"""Point-of-Sale (POS) Troubleshooting RAG Tool: pos_troubleshooting_rag_tool.

Performs semantic vector similarity search and full-text keyword retrieval over
certified Cymbal POS hardware manuals and service runbooks in BigQuery.
Uses fine-grained sliding-window chunk embeddings and adjacent context stitching (N-1 to N+1).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import google.auth
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

logger = logging.getLogger(__name__)

# Exact String Error and Decline Constants mandated by SDD
DECLINE_OUT_OF_SCOPE = (
    "DECLINE: Query falls outside certified POS hardware troubleshooting runbooks."
)
MANDATORY_DECLINE_STRING = DECLINE_OUT_OF_SCOPE
WARNING_BELOW_THRESHOLD = DECLINE_OUT_OF_SCOPE
ERROR_DATABASE_UNREACHABLE = (
    "ERROR: POS runbook database is currently unreachable due to transient connectivity issues. Please retry shortly."
)

SIMILARITY_THRESHOLD = 0.70


def get_current_project_id() -> str:
    """Dynamically resolves the active Google Cloud project ID."""
    proj = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID")
    if not proj:
        try:
            _, proj = google.auth.default()
        except Exception:
            pass
    if not proj:
        raise ValueError(
            "Project ID could not be determined. Please set GOOGLE_CLOUD_PROJECT or PROJECT_ID environment variable."
        )
    return proj


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
    project_id = get_current_project_id()
    location = os.getenv("LOCATION", "us-central1")
    client = bigquery.Client(project=project_id, location=location)

    max_retries = 3
    backoff_seconds = [1.0, 2.0, 4.0]

    for attempt in range(max_retries):
        try:
            return _execute_rag_search(client, project_id, query)
        except GoogleAPICallError as e:
            logger.warning("BigQuery API attempt %d failed: %s", attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(backoff_seconds[attempt])
        except Exception as e:
            logger.error("Unexpected error in POS RAG tool attempt %d: %s", attempt + 1, e)
            if attempt < max_retries - 1:
                time.sleep(backoff_seconds[attempt])

    return ERROR_DATABASE_UNREACHABLE


def _execute_rag_search(client: bigquery.Client, project_id: str, query: str) -> str:
    """Performs unified vector similarity search with in-SQL error-boosting regex check
    and adjacent context window stitching in a single GoogleSQL statement.
    """
    unified_sql = f"""
    WITH vector_candidates AS (
      SELECT
        base.document_filename,
        base.document_title,
        base.equipment_covered,
        base.source_pdf_uri,
        base.chunk_index,
        base.chunk_content,
        ROUND(1 - distance, 4) AS cosine_similarity
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
        top_k => 15,
        distance_type => 'COSINE'
      )
    ),
    scored_candidates AS (
      SELECT
        v.*,
        CASE
          -- Error-boosting regex check: boost score when query contains a specific error code matching chunk_content
          WHEN REGEXP_CONTAINS(@query, r'(?i)\\bERR-[A-Z0-9-]+\\b')
               AND REGEXP_CONTAINS(v.chunk_content, REGEXP_EXTRACT(@query, r'(?i)\\b(ERR-[A-Z0-9-]+)\\b'))
            THEN 0.95
          WHEN REGEXP_CONTAINS(@query, r'(?i)ERR-[A-Z0-9-]+') AND REGEXP_CONTAINS(v.chunk_content, r'(?i)ERR-[A-Z0-9-]+')
            THEN GREATEST(v.cosine_similarity, 0.85)
          ELSE v.cosine_similarity
        END AS boosted_score
      FROM vector_candidates v
    ),
    top_match AS (
      SELECT *
      FROM scored_candidates
      ORDER BY boosted_score DESC, cosine_similarity DESC
      LIMIT 1
    )
    SELECT
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.chunk_index,
      m.boosted_score AS similarity_score,
      STRING_AGG(c.chunk_content, '\\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM top_match m
    JOIN `{project_id}.cymbal_gold.pos_manual_chunk_embeddings` c
      ON m.document_filename = c.document_filename
     AND c.chunk_index BETWEEN (m.chunk_index - 1) AND (m.chunk_index + 1)
    GROUP BY
      m.document_filename,
      m.document_title,
      m.equipment_covered,
      m.source_pdf_uri,
      m.chunk_index,
      m.boosted_score
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("query", "STRING", query)]
    )
    results = list(client.query(unified_sql, job_config=job_config).result())

    if results:
        top_row = results[0]
        score = top_row["similarity_score"]
        if score >= SIMILARITY_THRESHOLD:
            return _format_runbook_response(top_row, method="Unified Semantic Vector Search")

    # Mandatory standard decline response string under similarity threshold drop or out-of-scope
    return DECLINE_OUT_OF_SCOPE



def _format_runbook_response(row: bigquery.Row, method: str) -> str:
    """Formats retrieved runbook chunk into a structured response with clickable HTTPS links."""
    gcs_uri = row["source_pdf_uri"] or ""
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
