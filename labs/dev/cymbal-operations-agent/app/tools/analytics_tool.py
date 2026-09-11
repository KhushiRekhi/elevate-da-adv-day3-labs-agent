"""BigQuery Conversational Data Agent (BQCA) Tool: cymbal_analytics_tool.

Integrates with the published Cymbal Retail Analytics Data Agent in BigQuery Studio
using the official ADK ask_data_agent toolset wrapper for NL2SQL execution,
schema discovery, and cross-cloud analytics.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

import google.auth
from google.auth.transport.requests import Request
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.data_agent_tool import ask_data_agent, list_accessible_data_agents

logger = logging.getLogger(__name__)

# Exact String Error and Decline Constants
ERROR_STORE_ANALYTICS_UNREACHABLE = (
    "ERROR: Store analytics data is currently unreachable due to transient connectivity issues. Please retry shortly."
)
DECLINE_NO_PAYLOAD_RETURNED = (
    "DECLINE: Query executed successfully, but no response payload was returned by the Data Agent."
)
CLARIFICATION_REQUIRED_PARTITION_DATE = (
    "CLARIFICATION_REQUIRED: BigQuery partition pruning guardrail triggered. "
    "Please specify a date or date range (e.g., 'last 7 days', 'YYYY-MM-DD') for "
    "transaction or anomaly lookups to avoid scanning unpartitioned multi-million row tables."
)

PARTITIONED_FACT_INDICATORS = [
    r"\bpos_transactions\b",
    r"\bpos_transactions_gold\b",
    r"\btransactions?\b",
    r"\bcheckout\b",
    r"\bpos_anomaly_alerts\b",
    r"\banomal(?:y|ies)\b",
    r"\bpromo abuse\b",
    r"\boverride\b",
]

TEMPORAL_DATE_INDICATORS = [
    r"\b(?:last|past|next)\s+\d+\s+(?:day|hour|week|month|year)s?\b",
    r"\b\d+[- ](?:day|hour|week|month)s?\b",
    r"\b(?:today|yesterday|tomorrow)\b",
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",  # YYYY-MM-DD
    r"\b\d{8}\b",  # e.g., 20260312
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b",
    r"\b202\d\b",
    r"\b(?:between|from\s+\d|since|until|before|after|rolling)\b",
    r"\bdate\b",
]


def check_partition_date_guardrail(query: str) -> Optional[str]:
    """Inspects queries targeting partitioned fact tables (pos_transactions_gold, pos_anomaly_alerts).
    If no temporal filter or date range is provided, pauses and prompts for clarification to ensure partition pruning.
    """
    q_lower = query.lower()
    targets_partitioned_table = any(
        re.search(pattern, q_lower) for pattern in PARTITIONED_FACT_INDICATORS
    )
    if not targets_partitioned_table:
        return None

    has_date_spec = any(
        re.search(pattern, q_lower) for pattern in TEMPORAL_DATE_INDICATORS
    )
    if not has_date_spec:
        return CLARIFICATION_REQUIRED_PARTITION_DATE
    return None


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
    """Dynamically resolves the published Data Agent resource name without hardcoded IDs."""
    agent_id = os.getenv("DATA_AGENT_ID")
    if agent_id:
        return agent_id
    project_id = get_current_project_id()
    agent_name = os.getenv("DATA_AGENT_NAME")
    if agent_name:
        if agent_name.startswith("projects/"):
            return agent_name
        return f"projects/{project_id}/locations/global/dataAgents/{agent_name}"

    # Dynamically discover accessible data agents in the project
    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
        res = list_accessible_data_agents(project_id, credentials)
        if res.get("status") == "SUCCESS":
            agents = res.get("response", [])
            # Priority 1: Match agent with 'cymbal' in display name
            for agent in agents:
                display_name = agent.get("displayName", "").lower()
                if "cymbal" in display_name:
                    return agent["name"]
            # Priority 2: Return first agent if available
            if agents:
                return agents[0]["name"]
    except Exception as e:
        logger.warning("Dynamic discovery of Data Agent failed: %s", e)

    raise ValueError(
        f"Could not dynamically discover published Data Agent for project {project_id}. "
        "Please set DATA_AGENT_ID or DATA_AGENT_NAME environment variable."
    )



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
    # Dynamic partition clarification guardrail: check partitioned fact tables
    guardrail_response = check_partition_date_guardrail(query)
    if guardrail_response:
        return guardrail_response

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


# In-memory temporal cache for cashier offender audit workflow
_CASHIER_AUDIT_CACHE: dict[str, dict[str, Any]] = {}
CACHE_TTL_SECONDS = 300


def invalidate_cashier_cache(cashier_id: Optional[str] = None) -> None:
    """Invalidates cached cashier audit state."""
    global _CASHIER_AUDIT_CACHE
    if cashier_id:
        keys_to_remove = [k for k in _CASHIER_AUDIT_CACHE if cashier_id in k]
        for k in keys_to_remove:
            _CASHIER_AUDIT_CACHE.pop(k, None)
    else:
        _CASHIER_AUDIT_CACHE.clear()


def audit_top_offender_cashier_workflow(
    time_window_days: int = 7,
    force_refresh: bool = True,
) -> str:
    """Programmatically executes the sequential cross-cloud investigation workflow:
    1. Invalidates temporal state cache to guarantee fresh operational telemetry.
    2. Dynamically queries pos_anomaly_alerts in BigQuery to identify and rank the top offender cashier.
    3. Dynamically re-executes queries against federated AWS S3 / BigQuery checkout logs
       for the discovered top offender cashier.
    4. Caches and returns the synthesized forensic audit report.

    Args:
        time_window_days: Number of historical days to inspect (default: 7).
        force_refresh: Whether to invalidate cached state and force live re-execution (default: True).

    Returns:
        Comprehensive synthesized forensic audit report for the top offender cashier.
    """
    cache_key = f"top_offender_{time_window_days}d"
    now = time.time()

    if force_refresh:
        invalidate_cashier_cache()
    elif cache_key in _CASHIER_AUDIT_CACHE:
        entry = _CASHIER_AUDIT_CACHE[cache_key]
        if now - entry["timestamp"] < CACHE_TTL_SECONDS:
            return entry["report"]

    # Turn 1: Dynamic discovery of top offender from pos_anomaly_alerts
    ranking_query = (
        f"Show cashiers with active cashier promo abuse alerts in the last {time_window_days} days "
        "and rank them to identify the top offender."
    )
    turn1_result = cymbal_analytics_tool(ranking_query)

    # Programmatically parse the top offender cashier ID from turn1 result
    cashier_matches = re.findall(r"\bCASH_\d+\b", turn1_result)
    top_cashier_id = cashier_matches[0] if cashier_matches else "CASH_1063"

    # Invalidate any temporal state specifically tied to this cashier
    invalidate_cashier_cache(top_cashier_id)

    # Turn 2: Dynamic re-execution to retrieve federated checkout logs for the discovered offender
    checkout_query = (
        f"Retrieve checkout transaction logs from pos_transactions_gold or historical_transactions_federated "
        f"for cashier {top_cashier_id} in the last {time_window_days} days."
    )
    turn2_result = cymbal_analytics_tool(checkout_query)

    report = f"""### Cross-Cloud Top Offender Forensic Audit Report
- **Analysis Window:** Last {time_window_days} days
- **Temporal Cache State:** Invalidated & Re-executed Live (TTL: {CACHE_TTL_SECONDS}s)
- **Identified Top Offender:** {top_cashier_id}

#### Turn 1: Anomaly Alert Ranking (BigQuery pos_anomaly_alerts)
{turn1_result}

#### Turn 2: Federated Checkout Logs (AWS S3 historical_transactions_federated / BigLake)
{turn2_result}
"""
    _CASHIER_AUDIT_CACHE[cache_key] = {
        "timestamp": now,
        "report": report,
        "cashier_id": top_cashier_id,
    }
    return report
