import re
from google.cloud import bigquery

client = bigquery.Client(project="data-adv-sg")

def test_vector_and_stitching(user_query: str):
    print(f"\n--- Testing Query: {user_query} ---")
    
    # 1. Vector search with adjacent chunk stitching
    vector_sql = r"""
    WITH matched_chunks AS (
      SELECT
        base.document_filename,
        base.document_title,
        base.equipment_covered,
        base.source_pdf_uri,
        base.chunk_index,
        ROUND(1 - distance, 4) AS similarity_score
      FROM VECTOR_SEARCH(
        TABLE `data-adv-sg.cymbal_gold.pos_manual_chunk_embeddings`,
        'embedding',
        (
          SELECT ml_generate_embedding_result AS embedding
          FROM ML.GENERATE_EMBEDDING(
            MODEL `data-adv-sg.cymbal_gold.pos_text_embedding_model`,
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
      STRING_AGG(c.chunk_content, '\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM matched_chunks m
    JOIN `data-adv-sg.cymbal_gold.pos_manual_chunk_embeddings` c
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
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("query", "STRING", user_query)]
    )
    results = list(client.query(vector_sql, job_config=job_config).result())
    
    best_score = results[0]["similarity_score"] if results else 0.0
    print(f"Top vector similarity score: {best_score}")
    
    if best_score >= 0.70:
        print("Score >= 0.70! Vector search accepted.")
        top_result = results[0]
        return format_result(top_result)
    
    print(f"Score {best_score} < 0.70. Triggering SEARCH() fallback...")
    
    # Extract error codes or alphanumeric terms
    error_codes = re.findall(r'[A-Z0-9]+-[A-Z0-9-]+', user_query)
    search_term = None
    if error_codes:
        # e.g. ERR-PAY-4001
        search_term = error_codes[0]
        print(f"Extracted error code: {search_term}")
    
    fallback_sql = r"""
    WITH matched_chunks AS (
      SELECT
        document_filename,
        document_title,
        equipment_covered,
        source_pdf_uri,
        chunk_index,
        0.85 AS similarity_score
      FROM `data-adv-sg.cymbal_gold.pos_manual_chunk_embeddings`
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
      STRING_AGG(c.chunk_content, '\n' ORDER BY c.chunk_index ASC) AS stitched_content
    FROM matched_chunks m
    JOIN `data-adv-sg.cymbal_gold.pos_manual_chunk_embeddings` c
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
    if search_term:
        fallback_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("search_term", "STRING", f'`{search_term}`')]
        )
        fb_results = list(client.query(fallback_sql, job_config=fallback_config).result())
        if fb_results:
            print(f"SEARCH() found matching chunk via code: {search_term}")
            return format_result(fb_results[0])

    # If full text search doesn't find it or query is completely out-of-scope (e.g. Ford F-150):
    print("Warning: No certified POS documentation met the confidence threshold.")
    return "WARNING: The query falls below the minimum certified similarity threshold (0.70) and no matching POS runbook documentation was found in certified store manuals."

def format_result(row):
    # Convert gs:// to https://storage.cloud.google.com/
    gcs_uri = row["source_pdf_uri"]
    https_url = gcs_uri.replace("gs://", "https://storage.cloud.google.com/")
    
    return f"""
### Certified POS Troubleshooting Runbook
- **Document Title:** {row['document_title']}
- **Hardware Covered:** {row['equipment_covered']}
- **Source Manual:** [{row['document_filename']}]({https_url})
- **Similarity Score:** {row['similarity_score']}
- **Matched Chunk:** #{row['chunk_index']}

#### Runbook & Procedural Instructions:
{row['stitched_content']}
"""

if __name__ == "__main__":
    q1 = "What is the immediate field recovery protocol when a cashier encounters an ERR-PAY-4001 EMV contactless payment freeze, and how do we ensure the customer is not double-charged?"
    res1 = test_vector_and_stitching(q1)
    print(res1[:500])
    
    q2 = "How do I replace the engine oil on a Ford F-150 truck?"
    res2 = test_vector_and_stitching(q2)
    print(res2)
