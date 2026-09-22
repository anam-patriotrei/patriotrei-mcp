# Patriot REI Buildium MCP Server

An MCP (Model Context Protocol) server that exposes read-only Buildium rental
reports as tools for Claude Code / Claude Desktop / any MCP-compatible
client. Built for a single-portfolio setup ("Patriot REI") where all active
rental owners/properties share one Buildium Open API credential.

Every tool wraps a **GET-only** Buildium call -- there is no create/update/
delete tool, so nothing here can modify data in Buildium.

The server is **stateless**: every tool call fetches fresh data from
Buildium and returns it directly to the caller in the tool response. Nothing
is written to disk, so there's no local report/issue storage to maintain and
no volume needed when deploying.

## What it does

| Tool | Description |
|---|---|
| `balance_sheet` | Balance Sheet reconstructed from Buildium GL account balances, as of today. |
| `rent_roll` | Current Rent Roll: all units, occupancy, tenants, rent, security deposits. |
| `gl_bank_activity` | GL transactions for the Operating & Security Deposit bank accounts, month-to-date. |
| `detect_report_issues` | Scans balance_sheet/rent_roll/gl_bank_activity results (passed in by the caller) for data-quality issues (unbalanced balance sheet, vacant units still charging rent, expired leases marked active, SD trust-account mismatches, etc). |
| `refresh_all_reports` | Fetches all three reports, runs issue detection over them, and returns everything in one call. |
| `buildium_get` | Read-only passthrough to any GET endpoint on the Buildium Open API. |

Note: Buildium's API has no endpoint for owner statements/distributions, so
that report is not available.

## Project layout

```
scripts/
  mcp_server.py          MCP server entry point (stdio + streamable-http)
  buildium_client.py     Buildium Open API client (auth, pagination, 429 backoff)
  config.py              Env-driven config: accounting basis, account-name matching
  fetch_balance_sheet.py
  fetch_rent_roll.py
  fetch_gl_bank_activity.py
  detect_issues.py
  auth_tokens.py         x-api-key auth for the streamable-http transport
  run_all.py             CLI equivalent of refresh_all_reports; prints JSON to stdout
Dockerfile
DEPLOY.md                Full deployment / auth-token setup guide
```

## Requirements

- Python 3.12
- A Buildium Open API Client ID/Secret (Buildium admin settings)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# then fill in BUILDIUM_CLIENT_ID / BUILDIUM_CLIENT_SECRET in .env
```

## Running

**Local (stdio)** -- default, single-user, launched as a subprocess by
Claude Desktop/Code via `.mcp.json`. No network listener, no auth needed:

```bash
python scripts/mcp_server.py
```

**Network (streamable-http)** -- for remote/shared access. Requires
`MCP_API_KEYS`:

```bash
MCP_TRANSPORT=streamable-http MCP_API_KEYS="alice:<token>" python scripts/mcp_server.py
```

Generate a token per user with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run all reports once from the CLI instead of via MCP:

```bash
python scripts/run_all.py
```

## Deployment

Deploys to [Render](https://render.com) via the included [`render.yaml`](render.yaml)
Blueprint (Docker-based web service, health check on `/health`, no persistent
disk needed since the server is stateless). See [DEPLOY.md](DEPLOY.md) for
the full guide, including Render setup, Docker elsewhere, running directly
with Python, putting the server behind TLS, and pointing clients at a
deployed instance.

## Configuration reference

See [.env.example](.env.example) for all supported environment variables,
including Buildium credentials, accounting-basis / account-name overrides,
and the streamable-http transport/auth settings.
