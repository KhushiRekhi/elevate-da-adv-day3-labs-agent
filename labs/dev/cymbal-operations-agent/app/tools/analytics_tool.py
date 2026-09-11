"""BigQuery Conversational Data Agent (BQCA) Tool: cymbal_analytics_tool.

Integrates with the published Cymbal Retail Analytics Data Agent in BigQuery Studio
using the official ADK ask_data_agent toolset wrapper for NL2SQL execution,
schema discovery, and cross-cloud analytics.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import google.auth
from google.auth.transport.requests import Request
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.data_agent_tool import ask_data_agent
from google.adk.tools.data_agent.data_agent_toolset import DataAgentToolset

logger = logging.getLogger(__name__)

# Exact String Error and Decline Constants
ERROR_STORE_ANALYTICS_UNREACHABLE = (
    "ERROR: Store analytics data is currently unreachable due to transient connectivity issues. Please retry shortly."
)
DECLINE_NO_PAYLOAD_RETURNED = (
    "DECLINE: Query executed successfully, but no response payload was returned by the Data Agent."
)


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


def get_data_agent_resource_name() -> str:
    """Dynamically resolves the published Data Agent resource name."""
    agent_id = os.getenv("DATA_AGENT_ID")
    if agent_id:
        return agent_id
    project_id = get_current_project_id()
    agent_name = os.getenv("DATA_AGENT_NAME", "agent_a5fd9440-6d65-4bd7-9335-d66b1fb79551")
    return f"projects/{project_id}/locations/global/dataAgents/{agent_name}"


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
    data_agent_name = get_data_agent_resource_name()
    config = DataAgentToolConfig(max_query_result_rows=100)

    max_retries = 3
    backoff_seconds = [1.0, 2.0, 4.0]

    for attempt in range(max_retries):
        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(Request())

            # Utilize the official ADK ask_data_agent toolset wrapper
            result = ask_data_agent(
                data_agent_name=data_agent_name,
                query=query,
                credentials=credentials,
                settings=config,
                tool_context=None,
            )

            if result.get("status") == "SUCCESS":
                events = result.get("response", [])
                generated_sql = None
                matched_query = None
                text_summaries = []
                data_results = None

                for item in events:
                    # Parse steps from ask_data_agent response
                    if "data" in item:
                        data_obj = item["data"]
                        if "query" in data_obj and data_obj["query"]:
                            q_val = data_obj["query"]
                            if isinstance(q_val, str):
                                generated_sql = q_val
                            elif isinstance(q_val, dict):
                                generated_sql = q_val.get("sqlQuery") or q_val.get("query")
                        elif "generatedSql" in data_obj and data_obj["generatedSql"]:
                            g_val = data_obj["generatedSql"]
                            generated_sql = (
                                g_val if isinstance(g_val, str) else g_val.get("sqlQuery") or g_val.get("query")
                            )
                        if "matchedQuery" in data_obj and data_obj["matchedQuery"]:
                            mq_val = data_obj["matchedQuery"]
                            if isinstance(mq_val, dict):
                                matched_query = (
                                    mq_val.get("exampleQuery", {}).get("sqlQuery")
                                    or mq_val.get("sqlQuery")
                                    or mq_val.get("naturalLanguageQuestion")
                                )
                            else:
                                matched_query = str(mq_val)
                        if "result" in data_obj and data_obj["result"]:
                            data_results = data_obj["result"]

                    if "Data Retrieved" in item:
                        data_results = item["Data Retrieved"]

                    if "text" in item:
                        ttype = item["text"].get("textType")
                        parts = item["text"].get("parts", [])
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
                return DECLINE_NO_PAYLOAD_RETURNED

            error_details = result.get("error_details", "")
            logger.warning(
                "ADK ask_data_agent attempt %d returned error: %s",
                attempt + 1,
                error_details[:200],
            )
        except Exception as e:
            logger.warning(
                "ADK ask_data_agent attempt %d encountered exception: %s",
                attempt + 1,
                e,
            )

        if attempt < max_retries - 1:
            time.sleep(backoff_seconds[attempt])

    return ERROR_STORE_ANALYTICS_UNREACHABLE
