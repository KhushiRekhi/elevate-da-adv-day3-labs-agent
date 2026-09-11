"""Point-of-Sale (POS) Troubleshooting RAG Tool: pos_troubleshooting_rag_tool.

Performs semantic vector similarity search and full-text keyword retrieval over
certified Cymbal POS hardware manuals and service runbooks in BigQuery.
Uses fine-grained sliding-window chunk embeddings and adjacent context stitching (N-1 to N+1).
"""

import os
import re
import time
import logging
from typing import Optional
from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPICallError

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_ID = "data-adv-sg"
SIMILARITY_THRESHOLD = 0.70


def pos_troubleshooting_rag_tool(query: str) -> str:
    """Retrieves certified POS hardware troubleshooting procedures, error code resolutions,
    and field service runbooks from the BigQuery knowledge store.

    Use this tool for:
    - POS hardware error codes (e.g. ERR-PAY-4001, ERR-DN-PRNT-24V, ERR-TGCS-COMM-02, ERR-UPOS-DEV-804).
    - EMV payment reader freezes, tokenization timeouts, and contactless transaction recovery.
    - Cash drawer solenoid failures, thermal receipt printer cutter jams, barcode scanner beam loss.
    - Step-by-step diagnostic and field recovery runbooks for store hardware:
      Toshiba TCx 810, Diebold Nixdorf BEETLE A1150, HP Engage One Pro, Clover Station Solo, NCR Voyix RealPOS XR7.
    - Safety protocols to ensure customers are not double-charged during transaction interruption.

    Args:
        query: The natural language description of the POS hardware fault or error code
               (e.g., 'ERR-PAY-4001 EMV reader freeze', 'thermal printer cutter lock').

    Returns:
        Certified procedural runbook instructions including document titles, hardware covered,
        clickable Google Cloud Storage PDF manual links, and stitched procedural steps.
        If out-of-scope or below confidence threshold, returns a certified warning message.
    """
    project_id = os.getenv("PROJECT_ID") or "data-adv-sg"
    location = os.getenv("LOCATION", "us-central1")
    client = bigquery.Client(project=project_id, location=location)

    max_retries = 3
    backoff_seconds = [1.0, 2.0, 4.0]

    for attempt in range(max_retries):
        try:
            return _execute_rag_search(client, project_id, query)
        except GoogleAPICallError as e:
            logger.warning(
                "BigQuery API error on attempt %d/%d: %s",
                attempt + 1,
                max_retries,
                str(e),
            )
            if attempt < max_retries - 1:
                time.sleep(backoff_seconds[attempt])
            else:
                return (
                    f"BigQuery RAG service temporarily unavailable after {max_retries} retries: {str(e)}"
                )
        except Exception as e:
            logger.error("Unexpected error in pos_troubleshooting_rag_tool: %s", str(e), exc_info=True)
            return f"Error executing POS troubleshooting retrieval: {str(e)}"


def _execute_rag_search(client: bigquery.Client, project_id: str, query: str) -> str:
    """Executes vector similarity search with adjacent chunk stitching and SEARCH fallback."""

    # 1. Vector Search with adjacent context stitching (N-1 to N+1)
    vector_sql = f"""
    WITH matched_chunks AS (
      SELECT
        base.document_filename,
        base.document_title,
        base.equipment_covered,
        base.source_pdf_uri,
        base.chunk_index,
        ROUND(1 - distance, 4) AS similarity_score
      FROM VECTOR_SEARCH(
        TABLE `{project_id}.cymbal_gold.pos_manual_chunk_embeddings`,
        'embedding',
        (
          SELECT ml_generate_embedding_result AS embedding
          FROM ML.GENERATE_EMBEDDING(
            MODEL `{project_id}.cymbal_gold.pos_text_embedding_model`,
            (SELECT @query AS content),
            STRUCT('RETRIEVAL_QUERY' AS task_type)
          )
        ),
        top_k => 3,
        distance_type => 'COSINE'
      )
    )
    SELECT
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.chunk_index,
      m.similarity_score,
      STRING_AGG(c.chunk_content, '\\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM matched_chunks m
    JOIN `{project_id}.cymbal_gold.pos_manual_chunk_embeddings` c
      ON m.document_filename = c.document_filename
     AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
    GROUP BY
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.chunk_index,
      m.similarity_score
    ORDER BY m.similarity_score DESC
    LIMIT 1
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("query", "STRING", query)]
    )
    results = list(client.query(vector_sql, job_config=job_config).result())

    if results:
        top_row = results[0]
        score = top_row["similarity_score"]
        if score >= SIMILARITY_THRESHOLD:
            return _format_runbook_response(top_row, method="Vector Similarity Search")

    # 2. Fallback: Full-Text SEARCH() for exact error codes or domain keywords
    # Extract error codes like ERR-PAY-4001 or model identifiers
    error_codes = re.findall(r'[A-Z0-9]+-[A-Z0-9-]+', query)
    search_terms = []
    if error_codes:
        search_terms.extend(error_codes)
    
    # Also check for key diagnostic tokens
    for kw in ["EMV", "freeze", "cutter", "solenoid", "thermal", "beep", "offline", "reboot"]:
        if kw.lower() in query.lower() and kw not in search_terms:
            search_terms.append(kw)

    for term in search_terms:
        fallback_sql = f"""
        WITH matched_chunks AS (
          SELECT
            document_filename,
            document_title,
            equipment_covered,
            source_pdf_uri,
            chunk_index,
            0.85 AS similarity_score
          FROM `{project_id}.cymbal_gold.pos_manual_chunk_embeddings`
          WHERE SEARCH(chunk_content, @search_term)
          LIMIT 1
        )
        SELECT
          m.document_filename,
          m.document_title,
          m.equipment_covered,
          m.source_pdf_uri,
          m.chunk_index,
          m.similarity_score,
          STRING_AGG(c.chunk_content, '\\n' ORDER BY c.chunk_index ASC) AS stitched_content
        FROM matched_chunks m
        JOIN `{project_id}.cymbal_gold.pos_manual_chunk_embeddings` c
          ON m.document_filename = c.document_filename
         AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
        GROUP BY
          m.document_filename,
          m.document_title,
          m.equipment_covered,
          m.source_pdf_uri,
          m.chunk_index,
          m.similarity_score
        """
        # Wrap search token in backticks to safely handle hyphens in BigQuery SEARCH()
        formatted_term = f"`{term}`" if "-" in term else term
        fb_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("search_term", "STRING", formatted_term)
            ]
        )
        fb_results = list(client.query(fallback_sql, job_config=fb_config).result())
        if fb_results:
            return _format_runbook_response(fb_results[0], method=f"Full-Text SEARCH Fallback ('{term}')")

    # 3. Certified Safety Fallback Warning when out-of-scope or below threshold
    return (
        "WARNING: The query falls below the minimum certified similarity threshold (0.70) "
        "and no matching POS runbook documentation was found in certified store manuals."
    )


def _format_runbook_response(row: bigquery.Row, method: str) -> str:
    """Formats retrieved runbook chunk into a structured response with clickable HTTPS links."""
    gcs_uri = row["source_pdf_uri"] or ""
    # Convert gs://... to clickable HTTPS link https://storage.cloud.google.com/...
    https_url = gcs_uri.replace("gs://", "https://storage.cloud.google.com/")
    doc_name = row["document_filename"] or "POS Hardware Service Manual"
    doc_title = row["document_title"] or doc_name
    equipment = row["equipment_covered"] or "All compatible POS terminals"
    score = row["similarity_score"]
    chunk_id = row["chunk_index"]
    stitched_text = row["stitched_content"] or ""

    return f"""### Certified POS Troubleshooting Runbook
- **Document Title:** {doc_title}
- **Equipment Covered:** {equipment}
- **Certified Source Manual:** [{doc_name}]({https_url})
- **Retrieval Match:** {method} (Relevance Score: {score}, Matched Chunk #{chunk_id} with adjacent window stitching)

#### Runbook & Procedural Instructions:
{stitched_text}
"""
