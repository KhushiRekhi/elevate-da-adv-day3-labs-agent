# Cymbal Retail Analytics & Operations Agent (Elevate Day 3 Labs)

This repository contains the complete implementation, configuration assets, verification scripts, and operational runbooks for **Elevate Advanced Data Analytics Day 3 Labs**:

1. **Module 3-1: Building Semantic Layer & IDE-Native Data Exploration** (`01-module3-semantic-handson-instructions.md`)
2. **Module 3-2: Building & Configuring the BigQuery Conversational Data Agent** (`02-module3-bqca-handson-instructions.md`)
3. **Module 3-3: Building & Orchestrating the Multi-Tool ADK Agent** (`03-module3-adk-handson-instructions.md`)

---

## Repository Structure

- `01-module3-semantic-handson-instructions.md`: Module 3-1 hands-on lab instructions
- `02-module3-bqca-handson-instructions.md`: Module 3-2 hands-on lab instructions
- `03-module3-adk-handson-instructions.md`: Module 3-3 hands-on lab instructions
- `README.md`: Repository overview and navigation

### `docs/`
Architecture, implementation planning, and execution tracking:
- `docs/implementation_plan.md`: Multi-tool agent architectural blueprint and implementation plan
- `docs/task.md`: Step-by-step progress tracking and execution ledger
- `docs/test_status.md`: Comprehensive validation and test status report

### `scratch/`
Dataplex aspect templates, data quality specifications, and test scripts:
- `scratch/aspect_template.yaml`: Dataplex Aspect Type definition (YAML)
- `scratch/aspect_template.json`: Dataplex Aspect Type definition (JSON)
- `scratch/aspect_pos_transactions_gold.json`: Aspect binding for pos_transactions_gold
- `scratch/aspect_pos_anomaly_alerts.json`: Aspect binding for pos_anomaly_alerts
- `scratch/aspect_inventory_ledger.json`: Aspect binding for gold_inventory_reconciliation_ledger
- `scratch/aspect_historical_tx.json`: Aspect binding for historical_transactional_data
- `scratch/aspect_warranty_sections.json`: Aspect binding for warranty_generic_sections_extracted
- `scratch/dq_spec.yaml`: Dataplex Data Quality Scan rule definitions
- `scratch/test_rag.py`: Standalone BigQuery Vector Search and RAG verification script

### `labs/dev/cymbal-operations-agent/`
Module 3-3 ADK Multi-Tool Agent Application:
- `Makefile`: Automation tooling (install, lint, test, web, deploy)
- `pyproject.toml`: Dependencies and packaging specification
- `deployment_metadata.json`: Vertex AI Reasoning Engine deployment metadata
- `app/agent.py`: Root Coordinator Agent (GlobalGemini + 4 decoupled tools)
- `app/fast_api_app.py`: FastAPI and A2A serving entrypoint
- `app/tools/analytics_tool.py`: BQCA NL2SQL tool with partition clarification guardrail
- `app/tools/rag_tool.py`: Vector search with token full-text fallback and decline strings
- `app/tools/bigtable_tool.py`: Cloud Bigtable streaming metrics via MCP Streamable HTTP
- `tests/unit/`: Comprehensive unit tests covering tools, guardrails, and workflows
- `tests/integration/`: Integration tests for streaming and A2A endpoints
- `tests/eval/`: Response quality evaluation dataset and judge

---

## Module Highlights

### Module 3-1: Semantic Layer & Knowledge Catalog Governance
- **Data Quality Scan:** Implemented via Dataplex (`scratch/dq_spec.yaml`) covering identifier completeness, payment amount integrity, and cart quantity integrity.
- **Custom Aspect Types:** Defined `table-operational-spec` (`scratch/aspect_template.yaml`) and bound across retail gold tables (`scratch/aspect_*.json`).
- **Data Profiling:** Exported statistical profiles to BigQuery governance dataset.

### Module 3-2: BigQuery Conversational Data Agent (BQCA)
- **Agent Name:** `Cymbal Retail Analytics Data Agent` (scoped with `global` location to mitigate mTLS routing errors).
- **Scope:** 6 conformed and federated analytical tables spanning `cymbal_gold`, `module1_unstructureddata`, and cross-cloud AWS S3 (`cymbal-lakehouse.elevate_data.silver_pos_transactions`).
- **System Instructions & Verification:** Verified NL2SQL queries for store inventory cover hours, revenue KPIs, and multi-cloud transaction audit.

### Module 3-3: Multi-Tool ADK Coordinator Agent (`cymbal_operations_agent`)
- **Model:** `GlobalGemini` with `gemini-3.6-flash` (Vertex AI global endpoint).
- **Toolsets:**
  1. `cymbal_analytics_tool`: NL2SQL relational and federated analytics using official ADK `ask_data_agent` with partition clarification guardrail.
  2. `pos_troubleshooting_rag_tool`: Multi-stage RAG with BigQuery `VECTOR_SEARCH`, sliding-window chunk embeddings (N-1 to N+1 adjacent context stitching), token-based full-text `SEARCH()` fallback, and certified decline strings.
  3. `bigtable_mcp_toolset`: Real-time streaming intra-hour cashier alerts and rolling metrics from Cloud Bigtable over MCP Streamable HTTP with OIDC bearer authentication.
  4. `audit_top_offender_cashier_workflow`: Programmatic cashier top offender temporal audit with cache invalidation and sequential multi-turn dispatch.
