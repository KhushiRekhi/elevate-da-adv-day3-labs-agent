"""Unit test suite for Cymbal Operations Agent tools and workflows.

Covers:
- pos_troubleshooting_rag_tool (vector search, full-text fallback, decline boundary, retries)
- cymbal_analytics_tool (partition clarification guardrail, NL2SQL handling, decline strings, retries)
- audit_top_offender_cashier_workflow (temporal cache invalidation, sequential execution)
- Bigtable MCP and resolution wrappers
- Agent tool registrations
"""

from __future__ import annotations

import os
import unittest.mock as mock
import pytest
from google.api_core.exceptions import GoogleAPICallError

from app.tools.analytics_tool import (
    CLARIFICATION_REQUIRED_PARTITION_DATE,
    DECLINE_NO_PAYLOAD_RETURNED,
    ERROR_STORE_ANALYTICS_UNREACHABLE,
    audit_top_offender_cashier_workflow,
    check_partition_date_guardrail,
    cymbal_analytics_tool,
    get_current_project_id,
    get_data_agent_resource_name,
    invalidate_cashier_cache,
)
from app.tools.bigtable_tool import (
    get_bigtable_mcp_url,
    get_target_service_account,
)
from app.tools.rag_tool import (
    DECLINE_OUT_OF_SCOPE,
    ERROR_DATABASE_UNREACHABLE,
    SIMILARITY_THRESHOLD,
    WARNING_BELOW_THRESHOLD,
    pos_troubleshooting_rag_tool,
)


# =====================================================================
# 1. RAG Tool Unit Tests
# =====================================================================

def test_rag_vector_search_success():
    """Verify primary vector search returns formatted runbook when score >= 0.70."""
    mock_row = {
        "document_filename": "toshiba_tcx810_manual.pdf",
        "document_title": "Toshiba TCx 810 Hardware & Service Guide",
        "equipment_covered": "Toshiba TCx 810 All-in-One POS",
        "source_pdf_uri": "gs://cymbal-store-manuals/toshiba_tcx810_manual.pdf",
        "chunk_index": 12,
        "similarity_score": 0.88,
        "stitched_content": "ERR-PAY-4001: Soft-reboot the EMV pin pad and verify 0420 reversal.",
    }

    mock_client = mock.MagicMock()
    mock_query_job = mock.MagicMock()
    mock_query_job.result.return_value = [mock_row]
    mock_client.query.return_value = mock_query_job

    with mock.patch("app.tools.rag_tool.bigquery.Client", return_value=mock_client), \
         mock.patch("app.tools.rag_tool.get_current_project_id", return_value="data-adv-sg"):
        result = pos_troubleshooting_rag_tool("ERR-PAY-4001 EMV contactless payment freeze")

    assert "Certified POS Troubleshooting Runbook" in result
    assert "Toshiba TCx 810" in result
    assert "https://storage.cloud.google.com/cymbal-store-manuals/toshiba_tcx810_manual.pdf" in result
    assert "Vector Similarity Search" in result
    assert "ERR-PAY-4001: Soft-reboot" in result


def test_rag_fallback_search_success():
    """Verify token-based full-text Fallback SEARCH succeeds when vector similarity < 0.70."""
    # First query (vector search) returns low similarity score (< 0.70)
    low_score_row = {
        "document_filename": "toshiba_tcx810_manual.pdf",
        "document_title": "Toshiba TCx 810 Manual",
        "equipment_covered": "Toshiba POS",
        "source_pdf_uri": "gs://cymbal/doc.pdf",
        "chunk_index": 5,
        "similarity_score": 0.52,
        "stitched_content": "General introduction...",
    }
    # Second query (fallback full-text SEARCH) returns matched row
    fallback_row = {
        "document_filename": "toshiba_tcx810_manual.pdf",
        "document_title": "Toshiba TCx 810 Manual",
        "equipment_covered": "Toshiba TCx 810",
        "source_pdf_uri": "gs://cymbal/doc.pdf",
        "chunk_index": 12,
        "similarity_score": 0.85,
        "stitched_content": "ERR-PAY-4001 recovery: reboot terminal.",
    }

    mock_client = mock.MagicMock()
    job_vector = mock.MagicMock()
    job_vector.result.return_value = [low_score_row]
    job_fallback = mock.MagicMock()
    job_fallback.result.return_value = [fallback_row]

    mock_client.query.side_effect = [job_vector, job_fallback]

    with mock.patch("app.tools.rag_tool.bigquery.Client", return_value=mock_client), \
         mock.patch("app.tools.rag_tool.get_current_project_id", return_value="data-adv-sg"):
        result = pos_troubleshooting_rag_tool("ERR-PAY-4001 EMV freeze")

    assert "Certified POS Troubleshooting Runbook" in result
    assert "Full-Text SEARCH Fallback" in result
    assert "ERR-PAY-4001 recovery" in result


def test_rag_decline_out_of_scope():
    """Verify out-of-scope query returns the exact mandated decline string."""
    mock_client = mock.MagicMock()
    empty_job = mock.MagicMock()
    empty_job.result.return_value = []
    mock_client.query.return_value = empty_job

    with mock.patch("app.tools.rag_tool.bigquery.Client", return_value=mock_client), \
         mock.patch("app.tools.rag_tool.get_current_project_id", return_value="data-adv-sg"):
        result = pos_troubleshooting_rag_tool("How do I replace the engine oil on a Ford F-150 truck?")

    assert result == DECLINE_OUT_OF_SCOPE
    assert "falls below the minimum certified similarity threshold (0.70)" in result


def test_rag_retry_and_database_unreachable():
    """Verify exponential backoff retry logic and unreachable message on API failures."""
    mock_client = mock.MagicMock()
    mock_client.query.side_effect = GoogleAPICallError("503 Service Unavailable")

    with mock.patch("app.tools.rag_tool.bigquery.Client", return_value=mock_client), \
         mock.patch("app.tools.rag_tool.get_current_project_id", return_value="data-adv-sg"), \
         mock.patch("time.sleep") as mock_sleep:
        result = pos_troubleshooting_rag_tool("ERR-PAY-4001")

    assert result == ERROR_DATABASE_UNREACHABLE
    assert mock_sleep.call_count == 2


# =====================================================================
# 2. Analytics Tool Guardrails and NL2SQL Unit Tests
# =====================================================================

def test_partition_date_guardrail_triggers_without_date():
    """Verify guardrail halts and prompts for date clarification on partitioned lookups."""
    # Query referencing transactions without date bounds
    q1 = "Show transactions in pos_transactions_gold"
    assert check_partition_date_guardrail(q1) == CLARIFICATION_REQUIRED_PARTITION_DATE
    assert cymbal_analytics_tool(q1) == CLARIFICATION_REQUIRED_PARTITION_DATE

    # Query referencing anomaly alerts without date bounds
    q2 = "List active promo abuse alerts for cashiers"
    assert check_partition_date_guardrail(q2) == CLARIFICATION_REQUIRED_PARTITION_DATE
    assert cymbal_analytics_tool(q2) == CLARIFICATION_REQUIRED_PARTITION_DATE

    # Query referencing checkout logs without date bounds
    q3 = "Retrieve checkout logs from pos_transactions"
    assert check_partition_date_guardrail(q3) == CLARIFICATION_REQUIRED_PARTITION_DATE
    assert cymbal_analytics_tool(q3) == CLARIFICATION_REQUIRED_PARTITION_DATE


def test_partition_date_guardrail_passes_with_date():
    """Verify guardrail passes when a date or temporal range is present."""
    queries_with_date = [
        "Show cashiers with active cashier promo abuse alerts in the last 7 days",
        "Check transaction details for TXN-20260312-0015811",
        "What is Cashier CASH_1190's 7-day historical override baseline?",
        "Show transactions from pos_transactions_gold for 2026-09-10",
        "Retrieve checkout logs between 2026-03-01 and 2026-03-07",
    ]
    for q in queries_with_date:
        assert check_partition_date_guardrail(q) is None


def test_partition_date_guardrail_passes_for_non_partitioned_queries():
    """Verify queries targeting non-partitioned tables (inventory, warranty) bypass guardrail."""
    non_partitioned_queries = [
        "What is the estimated cover hours remaining for store inventory positions under 20h?",
        "Show the warranty coverage policy for Samsung Galaxy Watch4",
        "Show inventory positions for prod_4691 at Store 15",
    ]
    for q in non_partitioned_queries:
        assert check_partition_date_guardrail(q) is None


def test_analytics_tool_success_with_adk_wrapper():
    """Verify cymbal_analytics_tool processes ADK ask_data_agent SUCCESS response."""
    mock_response = {
        "status": "SUCCESS",
        "response": [
            {
                "data": {
                    "query": "SELECT * FROM cymbal_gold.gold_inventory_reconciliation_ledger LIMIT 1",
                    "result": [{"store_id": "STORE_015", "product_id": "prod_4691", "cover_hours": 4.5}],
                }
            },
            {
                "text": {
                    "textType": "FINAL_RESPONSE",
                    "parts": ["Inventory position for prod_4691 at STORE_015 has 4.5 hours cover remaining."],
                }
            },
        ],
    }

    with mock.patch("app.tools.analytics_tool.get_data_agent_resource_name", return_value="projects/test/locations/global/dataAgents/cymbal-agent"), \
         mock.patch("app.tools.analytics_tool.ask_data_agent", return_value=mock_response), \
         mock.patch("google.auth.default", return_value=(mock.MagicMock(), "data-adv-sg")):
        result = cymbal_analytics_tool("What is the estimated cover hours remaining for store inventory positions under 20h?")

    assert "4.5 hours cover remaining" in result
    assert "SELECT * FROM cymbal_gold.gold_inventory_reconciliation_ledger" in result
    assert "STORE_015" in result


def test_analytics_tool_decline_no_payload():
    """Verify cymbal_analytics_tool returns DECLINE_NO_PAYLOAD_RETURNED when payload is empty."""
    mock_response = {"status": "SUCCESS", "response": []}

    with mock.patch("app.tools.analytics_tool.get_data_agent_resource_name", return_value="projects/test/locations/global/dataAgents/cymbal-agent"), \
         mock.patch("app.tools.analytics_tool.ask_data_agent", return_value=mock_response), \
         mock.patch("google.auth.default", return_value=(mock.MagicMock(), "data-adv-sg")):
        result = cymbal_analytics_tool("Query inventory for store 10 in 2026-03-12")

    assert result == DECLINE_NO_PAYLOAD_RETURNED


def test_analytics_tool_unreachable_on_retries():
    """Verify cymbal_analytics_tool returns unreachable error when ADK calls fail."""
    with mock.patch("app.tools.analytics_tool.get_data_agent_resource_name", return_value="projects/test/locations/global/dataAgents/cymbal-agent"), \
         mock.patch("app.tools.analytics_tool.ask_data_agent", side_effect=Exception("Connection timed out")), \
         mock.patch("google.auth.default", return_value=(mock.MagicMock(), "data-adv-sg")), \
         mock.patch("time.sleep"):
        result = cymbal_analytics_tool("Query inventory for store 10 in 2026-03-12")

    assert result == ERROR_STORE_ANALYTICS_UNREACHABLE


# =====================================================================
# 3. Top Offender Temporal Workflow Unit Tests
# =====================================================================

def test_audit_top_offender_cashier_workflow_execution():
    """Verify programmatic temporal state invalidation and sequential re-execution."""
    turn1_mock = "Top offender ranking over the last 7 days: Cashier CASH_1063 at STORE_016 with 50 promo alerts."
    turn2_mock = "Checkout transaction logs for CASH_1063: 10 transactions with $1,250 in manual overrides on 2026-09-10."

    with mock.patch("app.tools.analytics_tool.cymbal_analytics_tool", side_effect=[turn1_mock, turn2_mock]):
        report = audit_top_offender_cashier_workflow(time_window_days=7, force_refresh=True)

    assert "Cross-Cloud Top Offender Forensic Audit Report" in report
    assert "CASH_1063" in report
    assert "Turn 1: Anomaly Alert Ranking" in report
    assert "Turn 2: Federated Checkout Logs" in report
    assert "Invalidated & Re-executed Live" in report


def test_cashier_cache_invalidation():
    """Verify cache invalidation logic clears cached state."""
    invalidate_cashier_cache()
    from app.tools.analytics_tool import _CASHIER_AUDIT_CACHE
    assert len(_CASHIER_AUDIT_CACHE) == 0


# =====================================================================
# 4. Bigtable Tool and Resolution Wrappers Unit Tests
# =====================================================================

def test_get_bigtable_mcp_url_env_override():
    """Verify BIGTABLE_MCP_URL environment variable takes precedence."""
    test_url = "https://custom-bigtable-mcp.run.app"
    with mock.patch.dict(os.environ, {"BIGTABLE_MCP_URL": test_url}):
        assert get_bigtable_mcp_url() == test_url


def test_get_bigtable_mcp_url_fallback():
    """Verify get_bigtable_mcp_url constructs standard Cloud Run URL as fallback."""
    with mock.patch.dict(os.environ, {"BIGTABLE_MCP_URL": "", "GOOGLE_CLOUD_PROJECT": "data-adv-sg", "LOCATION": "us-central1"}):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            url = get_bigtable_mcp_url()
            assert "data-adv-sg" in url
            assert "us-central1.run.app" in url


def test_get_current_project_id_from_env():
    """Verify get_current_project_id dynamically reads from environment."""
    with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-project-123"}):
        assert get_current_project_id() == "test-project-123"


def test_get_data_agent_resource_name():
    """Verify get_data_agent_resource_name handles explicit IDs and dynamic discovery."""
    with mock.patch.dict(os.environ, {"DATA_AGENT_ID": "projects/123/locations/global/dataAgents/agent_abc"}):
        assert get_data_agent_resource_name() == "projects/123/locations/global/dataAgents/agent_abc"


# =====================================================================
# 5. Agent Tools Registration Unit Test
# =====================================================================

def test_agent_tools_registration():
    """Verify all 4 specialized operational tools are registered on root agent."""
    from app.agent import cymbal_operations_agent, root_agent

    assert root_agent is cymbal_operations_agent
    tool_names = [t.__name__ if hasattr(t, "__name__") else t.__class__.__name__ for t in cymbal_operations_agent.tools]
    assert "cymbal_analytics_tool" in tool_names
    assert "pos_troubleshooting_rag_tool" in tool_names
    assert "McpToolset" in tool_names
    assert "audit_top_offender_cashier_workflow" in tool_names

