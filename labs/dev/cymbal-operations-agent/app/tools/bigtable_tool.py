"""Cloud Bigtable MCP Toolset: bigtable_mcp_toolset.

Connects to the deployed mcp-toolbox-bigtable Cloud Run microservice via Model Context Protocol (MCP)
using Streamable HTTP transport and GCP OIDC bearer token authentication.
"""

import os
import logging
import google.auth
import google.oauth2.id_token
from google.auth.transport.requests import Request
from google.auth import impersonated_credentials
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

logger = logging.getLogger(__name__)

DEFAULT_BIGTABLE_MCP_URL = "https://mcp-toolbox-bigtable-1055849777405.us-central1.run.app"
DEFAULT_TARGET_SA = "cymbal-sa-data@data-adv-sg.iam.gserviceaccount.com"


def get_oidc_headers(service_url: str) -> dict:
    """Generates an OIDC Authorization header for the target Cloud Run service audience."""
    target_sa = os.getenv("TARGET_SERVICE_ACCOUNT", DEFAULT_TARGET_SA)
    try:
        base_creds, _ = google.auth.default()
        target_creds = impersonated_credentials.Credentials(
            source_credentials=base_creds,
            target_principal=target_sa,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        id_creds = impersonated_credentials.IDTokenCredentials(
            target_credentials=target_creds,
            target_audience=service_url,
            include_email=True,
        )
        id_creds.refresh(Request())
        return {"Authorization": f"Bearer {id_creds.token}"}
    except Exception as e:
        logger.warning("Failed impersonation ID token generation: %s. Trying direct fetch.", str(e))
        auth_req = Request()
        token = google.oauth2.id_token.fetch_id_token(auth_req, service_url)
        return {"Authorization": f"Bearer {token}"}


service_url = os.getenv("BIGTABLE_MCP_URL", DEFAULT_BIGTABLE_MCP_URL)
mcp_endpoint = f"{service_url.rstrip('/')}/mcp"

bigtable_mcp_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=mcp_endpoint,
        headers=get_oidc_headers(service_url),
        timeout=30,
    ),
    header_provider=lambda ctx: get_oidc_headers(service_url),
)
