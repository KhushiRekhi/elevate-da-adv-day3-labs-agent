"""BigQuery Conversational Data Agent (BQCA) Tool: cymbal_analytics_tool.

Integrates with the published Cymbal Retail Analytics Data Agent in BigQuery Studio
for NL2SQL execution, schema discovery, and cross-cloud analytics.
"""

import os
import time
import json
import logging
import requests
import google.auth
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

DEFAULT_DATA_AGENT_ID = "projects/data-adv-sg/locations/global/dataAgents/agent_a5fd9440-6d65-4bd7-9335-d66b1fb79551"


def cymbal_analytics_tool(query: str) -> str:
    """Executes relational analytics and natural language inquiries over Cymbal retail gold tables
    and federated AWS S3 tables using the BigQuery Conversational Data Agent.

    Use this tool for:
    - Store inventory positions, shelf/backroom units, and stockout cover hours (<20h).
    - Real-time intraday POS checkout ledger and daily gross revenue KPIs.
    - Historical customer purchase lookups and warranty claim triage (flattening items array).
    - Historical cashier promo abuse alerts and multi-day offender rankings.
    - Cross-cloud AWS S3 transaction log retrieval via BigLake federation.

    Args:
        query: The natural language question to submit to the BigQuery Data Agent.
               Preserve enterprise business terms (e.g., Net Transaction Revenue,
               Total On-Hand Inventory, Estimated Inventory Cover Hours,
               Cashier Promo Override Rate) verbatim.

    Returns:
        A detailed response string containing the analytical summary,
        generated GoogleSQL query, and retrieved data rows.
    """
    data_agent_name = os.getenv("DATA_AGENT_ID", DEFAULT_DATA_AGENT_ID)
    parent = data_agent_name.rsplit("/", 2)[0]
    chat_url = f"https://geminidataanalytics.googleapis.com/v1beta/{parent}:chat"

    max_retries = 3
    backoff_seconds = [1.0, 2.0, 4.0]

    for attempt in range(max_retries):
        try:
            credentials, _ = google.auth.default()
            credentials.refresh(Request())

            headers = {
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            }
            payload = {
                "messages": [{"userMessage": {"text": query}}],
                "dataAgentContext": {"dataAgent": data_agent_name},
            }

            resp = requests.post(chat_url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                events = resp.json()
                generated_sql = None
                matched_query = None
                text_summaries = []
                data_results = None

                for item in events:
                    msg = item.get("systemMessage", {})
                    if "data" in msg:
                        data_obj = msg["data"]
                        if "query" in data_obj and data_obj["query"]:
                            q_val = data_obj["query"]
                            if isinstance(q_val, str):
                                generated_sql = q_val
                            elif isinstance(q_val, dict):
                                generated_sql = q_val.get("sqlQuery") or q_val.get("query")
                        elif "generatedSql" in data_obj and data_obj["generatedSql"]:
                            g_val = data_obj["generatedSql"]
                            generated_sql = g_val if isinstance(g_val, str) else g_val.get("sqlQuery") or g_val.get("query")
                        if "matchedQuery" in data_obj and data_obj["matchedQuery"]:
                            mq_val = data_obj["matchedQuery"]
                            if isinstance(mq_val, dict):
                                matched_query = mq_val.get("exampleQuery", {}).get("sqlQuery") or mq_val.get("sqlQuery") or mq_val.get("naturalLanguageQuestion")
                            else:
                                matched_query = str(mq_val)
                        if "result" in data_obj and data_obj["result"]:
                            data_results = data_obj["result"]
                    if "text" in msg:
                        ttype = msg["text"].get("textType")
                        parts = msg["text"].get("parts", [])
                        if ttype == "FINAL_RESPONSE" and parts:
                            text_summaries.append("\n".join(parts))

                output_parts = []
                if text_summaries:
                    output_parts.append("\n\n".join(text_summaries))
                if generated_sql and isinstance(generated_sql, str):
                    output_parts.append(f"\n```sql\n{generated_sql.strip()}\n```")
                elif matched_query and isinstance(matched_query, str):
                    output_parts.append(f"\n```sql\n{matched_query.strip()}\n```")
                if data_results:
                    output_parts.append(f"\nData Results:\n{json.dumps(data_results, indent=2)}")

                if output_parts:
                    return "\n".join(output_parts)
                return "Query executed successfully, but no response payload was returned by the Data Agent."

            logger.warning(
                "Data Agent attempt %d failed with HTTP %d: %s",
                attempt + 1,
                resp.status_code,
                resp.text[:200],
            )
        except Exception as e:
            logger.warning("Data Agent attempt %d encountered exception: %s", attempt + 1, e)

        if attempt < max_retries - 1:
            time.sleep(backoff_seconds[attempt])

    return "Error: Store analytics data is currently unreachable due to transient connectivity issues. Please retry shortly."
