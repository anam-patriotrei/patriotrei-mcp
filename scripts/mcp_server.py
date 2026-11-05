"""MCP server exposing the Buildium rental report tools to Claude Code / Claude
Desktop / any MCP client.

Read-only for everyone except the API-key names listed in
write_access.WRITE_PERMISSIONS (currently only "julie"), who can also use
buildium_write. Every other tool wraps a GET-only Buildium call (via
BuildiumClient) or one of the report builders also used by run_all.py.

Two transports, controlled by MCP_TRANSPORT:

- "stdio" (default): local, single-user, spoken over the process's own
  stdin/stdout (e.g. Claude Desktop or ../.mcp.json launching this as a
  subprocess). No network listener, so no bearer-token auth applies.
    python scripts/mcp_server.py

- "streamable-http": a network-reachable server for remote/shared
  deployments. Requires MCP_API_KEYS (see auth_tokens.py / .env.example)
  unless MCP_DISABLE_AUTH=true is set for local-only testing. Clients
  authenticate with an `x-api-key` header, not `Authorization` -- see
  auth_tokens.py for why.
    MCP_TRANSPORT=streamable-http MCP_API_KEYS="ops:<token>" python scripts/mcp_server.py

See DEPLOY.md for full deployment / auth-token setup instructions.
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import Context, MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

import detect_issues
import write_access
import fetch_balance_sheet
import fetch_gl_bank_activity
import fetch_rent_roll
from buildium_client import BuildiumClient

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
DISABLE_AUTH = os.environ.get("MCP_DISABLE_AUTH", "").strip().lower() in ("1", "true", "yes")

# Configured here (not left to MCPServer's own configure_logging, which
# doesn't run until the constructor below) so the startup messages just below
# -- in particular the "no authentication" warning -- are never silently
# dropped by an unconfigured root logger, on stdio or HTTP.
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
logger = logging.getLogger("buildium-mcp")

if not (os.environ.get("BUILDIUM_CLIENT_ID") and os.environ.get("BUILDIUM_CLIENT_SECRET")):
    raise SystemExit(
        "Missing Buildium credentials. Set BUILDIUM_CLIENT_ID and BUILDIUM_CLIENT_SECRET "
        "(e.g. in a .env file copied from .env.example) before starting the server."
    )

api_key_verifier = None

if TRANSPORT == "stdio":
    if os.environ.get("MCP_API_KEYS") or DISABLE_AUTH:
        logger.warning("MCP_API_KEYS/MCP_DISABLE_AUTH are ignored on the stdio transport.")
else:
    if DISABLE_AUTH:
        logger.warning(
            "MCP_DISABLE_AUTH=true: this %s server is running with NO authentication. "
            "Anyone who can reach %s:%s can call every tool. Local testing only.",
            TRANSPORT,
            HOST,
            PORT,
        )
    else:
        from auth_tokens import ConfigError, StaticApiKeyVerifier

        try:
            api_key_verifier = StaticApiKeyVerifier.from_env()
        except ConfigError as exc:
            raise SystemExit(str(exc)) from exc

        logger.info("x-api-key authentication enabled (%d key(s) configured).", len(api_key_verifier))

# No token_verifier/auth passed here: those wire up the MCP SDK's built-in
# bearer-auth middleware, which only checks the `Authorization` header. This
# server checks `x-api-key` instead (see auth_tokens.py), applied as an ASGI
# wrapper around the app in run_streamable_http() below.
mcp = MCPServer(
    "buildium-rental-reports",
    log_level=LOG_LEVEL,
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    """Unauthenticated liveness/readiness probe for load balancers / container orchestrators."""
    return JSONResponse({"status": "ok"})


@mcp.tool()
def balance_sheet() -> dict:
    """Fetch a Balance Sheet reconstructed from Buildium GL account balances,
    as of today, and return it directly. Nothing is saved locally."""
    return fetch_balance_sheet.fetch()


@mcp.tool()
def rent_roll() -> list:
    """Fetch the current Rent Roll (all units, occupancy, tenants, rent,
    security deposits) from Buildium and return it directly. Nothing is
    saved locally."""
    return fetch_rent_roll.fetch()


@mcp.tool()
def gl_bank_activity() -> dict:
    """Fetch GL transactions for the Operating and Security Deposit bank
    accounts for the current month-to-date and return them directly.
    Nothing is saved locally."""
    return fetch_gl_bank_activity.fetch()


@mcp.tool()
def detect_report_issues(balance_sheet: dict | None = None, rent_roll: list | None = None, gl_bank_activity: dict | None = None) -> dict:
    """Scan the given Balance Sheet, Rent Roll and/or GL report (as returned
    by this server's own balance_sheet/rent_roll/gl_bank_activity tools) for
    data-quality / bookkeeping issues: unbalanced balance sheet, vacant units
    still charging rent, occupied units with no rent recorded, expired
    leases still marked active, negative operating balance, Security Deposit
    trust-account balance not matching the SD liability, etc. Pass whichever
    reports you have; omit the rest."""
    return detect_issues.run(bs=balance_sheet, rr=rent_roll, gl=gl_bank_activity)


@mcp.tool()
def refresh_all_reports() -> dict:
    """Fetch Balance Sheet, Rent Roll and GL bank activity, run issue
    detection over the fresh data, and return everything directly. Nothing
    is saved locally. Equivalent to running run_all.py."""
    bs = fetch_balance_sheet.fetch()
    rr = fetch_rent_roll.fetch()
    gl = fetch_gl_bank_activity.fetch()
    issues = detect_issues.run(bs=bs, rr=rr, gl=gl)
    return {"balance_sheet": bs, "rent_roll": rr, "gl": gl, "issues": issues}


@mcp.tool()
def buildium_get(path: str, params: dict | None = None) -> dict:
    """Read-only passthrough to any GET endpoint on the Buildium Open API
    (e.g. "/rentals", "/leases", "/bankaccounts", "/rentals/owners"). Only
    GET is exposed here -- there is no create/update/delete tool, so this
    cannot modify data in Buildium.

    Args:
        path: API path starting with "/", relative to https://api.buildium.com/v1
        params: optional query parameters
    """
    if not path.startswith("/"):
        path = "/" + path
    client = BuildiumClient()
    return client.get(path, params=params or {})


@mcp.tool()
def buildium_write(
    ctx: Context,
    method: str,
    path: str,
    body: dict | list | None = None,
    params: dict | None = None,
    confirm: bool = False,
    preview_code: str | None = None,
) -> dict:
    """Create or change data in Buildium (POST/PUT/PATCH) -- e.g. create a
    work order (POST /workorders), a task (POST /tasks/todorequests), or a
    bill (POST /bills). Only approved users can use this; everyone else gets
    a "refused" answer and stays read-only.

    ALWAYS call it first with confirm=false: that returns a preview and a
    preview_code and changes nothing. Explain the preview to the user in
    plain words. Only after they clearly confirm, call again with exactly the
    same method, path, body and params, plus confirm=true and preview_code.
    The code works once and expires after 15 minutes.

    Look up IDs first with buildium_get (properties "/rentals", units
    "/rentals/units", vendors "/vendors", staff "/users"), and check
    Buildium's API reference for the body fields of each endpoint.

    Args:
        method: POST, PUT or PATCH
        path: API path relative to https://api.buildium.com/v1, e.g. "/workorders"
        body: JSON body to send
        params: optional query parameters
        confirm: false = preview only; true = actually send it
        preview_code: the code from the preview (required when confirm=true)
    """
    return write_access.buildium_write(ctx, method, path, body=body, params=params,
                                       confirm=confirm, preview_code=preview_code)


def _transport_security():
    """Optional Host/Origin allowlisting for the HTTP transports, from env.

    Unset by default: MCP clients are typically CLI/desktop apps, not
    browsers, so DNS-rebinding protection matters less here than for a
    cookie-authenticated web app -- but set MCP_ALLOWED_HOSTS /
    MCP_ALLOWED_ORIGINS (comma-separated) to lock it down behind a known
    reverse-proxy hostname in production.
    """
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    origins = [o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if not hosts and not origins:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts or ["*"],
        allowed_origins=origins or ["*"],
    )


def _run_streamable_http() -> None:
    """Equivalent to mcp.run("streamable-http", ...), except the app is
    wrapped with XApiKeyAuthMiddleware instead of the SDK's Authorization-
    header auth (which we never wire up above)."""
    import anyio
    import uvicorn

    app = mcp.streamable_http_app(host=HOST, transport_security=_transport_security())
    if api_key_verifier is not None:
        from auth_tokens import XApiKeyAuthMiddleware

        app = XApiKeyAuthMiddleware(app, api_key_verifier)

    config = uvicorn.Config(app, host=HOST, port=PORT, log_level=LOG_LEVEL.lower())
    anyio.run(uvicorn.Server(config).serve)


if __name__ == "__main__":
    if TRANSPORT == "stdio":
        mcp.run()
    else:
        _run_streamable_http()
