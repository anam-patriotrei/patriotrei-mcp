# Deploying the Buildium MCP server

Two ways to run this server:

- **stdio** (default) -- a local subprocess launched by Claude Desktop /
  Claude Code via `.mcp.json`. Single user, no network listener, no auth.
  This is unchanged from before.
- **streamable-http** -- a network-reachable server multiple authorized
  users/integrations can connect to remotely. This mode requires an API-key
  header (`x-api-key`, not `Authorization` -- see the note at the end of
  this section); the server refuses to start without it unless you
  explicitly opt out for local testing.

## 1. Generate an API key per authorized user

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run it once per person/integration that should have access. Put the results
in `MCP_API_KEYS` as `name:token` pairs, comma-separated:

```
MCP_API_KEYS=alice:9f2c...,bob:71ad...,cowork-bot:e610...
```

Treat each token like a password: send it only over HTTPS, store it in your
deploy platform's secret manager (not in git), and issue a fresh one (just
edit `MCP_API_KEYS` and restart) if it's ever exposed. There's no
expiry/revocation list beyond "remove it from `MCP_API_KEYS` and restart" --
fine for a small, known set of users; swap in a real OAuth provider
(`auth_server_provider=` in `scripts/mcp_server.py`) if you outgrow that.

## 2. Configure environment

Copy `.env.example` to `.env` (local) or set these as real environment
variables / secrets in your hosting platform:

| Variable | Required | Purpose |
|---|---|---|
| `BUILDIUM_CLIENT_ID`, `BUILDIUM_CLIENT_SECRET` | yes | Buildium Open API credentials |
| `MCP_TRANSPORT` | yes, set to `streamable-http` | Enables the network transport |
| `MCP_API_KEYS` | yes (unless `MCP_DISABLE_AUTH=true`) | Authorized caller tokens |
| `MCP_HOST` / `MCP_PORT` | no (default `0.0.0.0:8000` in Docker) | Bind address |
| `MCP_PUBLIC_URL` | recommended | Public HTTPS URL clients use to reach the server |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | optional | Lock the HTTP transport to your reverse-proxy hostname |
| `LOG_LEVEL` | no (default `INFO`) | |

The server validates `BUILDIUM_CLIENT_ID`/`SECRET` and (for the HTTP
transport) `MCP_API_KEYS` at startup and exits immediately with a clear error
if either is missing -- it will not silently come up half-configured.

## 3. Run it

### Docker (recommended)

```
docker build -t buildium-mcp .
docker run -d --name buildium-mcp \
  --env-file .env \
  -p 8000:8000 \
  buildium-mcp
```

The image runs as a non-root user and exposes `GET /health` (unauthenticated,
for your load balancer / orchestrator's health check). The server is
stateless -- every tool call fetches fresh data from Buildium and returns it
directly to the caller; nothing is written to disk, so no volume is needed
and the container can be freely restarted/scaled.

### Directly with Python

```
pip install -r requirements.txt
MCP_TRANSPORT=streamable-http MCP_API_KEYS="alice:<token>" python scripts/mcp_server.py
```

### Render

This repo includes a [`render.yaml`](render.yaml) Blueprint, so the fastest
path is:

1. Push this repo to GitHub/GitLab.
2. In the Render dashboard: **New +** -> **Blueprint** -> select this repo.
   Render reads `render.yaml` and creates a Docker-based web service named
   `buildium-mcp` from the existing `Dockerfile` automatically.
3. When prompted, fill in the env vars Render leaves blank (marked
   `sync: false` in the Blueprint):
   - `BUILDIUM_CLIENT_ID`, `BUILDIUM_CLIENT_SECRET` -- your Buildium Open API credentials
   - `MCP_API_KEYS` -- `name:token` pairs from step 1 above
   - `MCP_PUBLIC_URL` -- leave blank for the first deploy; after it's live, set it to the
     `https://<service>.onrender.com` URL (or your custom domain) and redeploy
4. Deploy. Render builds the image, starts the container, and polls
   `GET /health` (already set as `healthCheckPath`) before routing traffic to it.

No `PORT` env var is needed -- Render auto-detects the container's listening
port from the Dockerfile's `EXPOSE 8000`. TLS is automatic and free on both
the `onrender.com` subdomain and any custom domain you attach, so there's no
separate Caddy/nginx step on Render (see the general TLS note below for
other platforms).

If you'd rather configure it by hand instead of via the Blueprint: **New +**
-> **Web Service** -> connect the repo -> **Environment: Docker** ->
Dockerfile Path `./Dockerfile` -> **Health Check Path** `/health` -> set the
same env vars as above.

The Blueprint uses Render's **free** web service plan, so this costs nothing
to deploy. The tradeoff: free-tier services spin down after 15 minutes of
inactivity, so the next request after a quiet period pays a ~30-60s
cold-start (and drops any in-flight streamable-http connection). If that
matters for your usage, change `plan: free` to `plan: starter` in
`render.yaml` (or pick **Starter** in the dashboard) for an always-on
instance -- that's a paid plan.

### Put it behind TLS

The app itself speaks plain HTTP. In production, terminate TLS in front of
it (Caddy, nginx, or your platform's built-in HTTPS -- e.g. Render, Fly.io,
Railway, an ALB) and proxy to port 8000. API keys sent over plain HTTP are as
good as no auth, so don't expose port 8000 directly to the internet. Render
handles this automatically (see above); for a bare VPS, a minimal Caddy
example:

```
buildium-mcp.example.com {
    reverse_proxy localhost:8000
}
```

## 4. Point clients at it

Any MCP client that supports the streamable-http transport connects with the
URL and an `x-api-key` header. Example client-side `mcp.json` entry
(this replaces the local stdio entry in this repo's own `.mcp.json`, which is
for local development against this same server):

```json
{
  "mcpServers": {
    "buildium": {
      "url": "https://buildium-mcp.example.com/mcp",
      "headers": {
        "x-api-key": "<the user's token>"
      }
    }
  }
}
```

Each user gets their own token from step 1 -- don't share one token across
people.

**Why `x-api-key` and not `Authorization`:** on platforms where sign-in/OAuth
is enabled for the connection (e.g. a claude.ai custom connector), the
platform reserves the `Authorization` header for its own OAuth bearer token
-- this server's static API key can't also travel on that header. The server
checks `x-api-key` instead (`scripts/auth_tokens.py`), so it works
regardless of whether the platform's own sign-in is enabled.

## Sign in with Google + who can do what

People connect to the Claude connector by signing in with Google, so the server
knows their email. Integrations can still use an `x-api-key`.

**Who can do what** is set in `scripts/access_rules.py`:
- `READ_DOMAINS` / `READ_EMAILS`: who may sign in and read.
- `WRITE_PERMISSIONS`: emails that may also use `buildium_write` (today: Julie).
To change access, edit that file and merge to `main`. Removing someone takes
effect at their next request.

**Render env vars** (Google sign-in is off until `GOOGLE_CLIENT_ID` is set):
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`: from Google Cloud (OAuth client, Web application).
  Authorized redirect URI: `https://buildium-mcp.onrender.com/oauth/google/callback`.
- `MCP_OAUTH_SECRET`: 40+ random characters (signs sign-in tokens; changing it signs everyone out).
- `MCP_PUBLIC_URL`: `https://buildium-mcp.onrender.com`

**Claude connector** (org admin): Authentication "Sign in now" / OAuth client
"Register automatically", and no `x-api-key` header.

Every write previews first; the real call must send back the one-time preview code.
To stop all writes at once, set `MCP_WRITES_ENABLED=false` on Render.

## Notes on the current auth design

- It's per-token, not per-tool: any valid token can call every tool,
  including `buildium_get` (a read-only passthrough to arbitrary Buildium GET
  endpoints). There's no create/update/delete tool, so no token here can
  modify Buildium data, but any authorized user can read anything the
  Buildium API key can read.
- All authorized users share the same Buildium credentials -- this mirrors
  the existing single-portfolio design, not a multi-tenant setup.
- Buildium itself rate-limits to ~10 req/s per client id/secret pair
  (`scripts/buildium_client.py` already backs off on 429s); a large number of
  concurrent users hammering `buildium_get` will contend for that shared
  budget.
