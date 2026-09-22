"""Static API-key authentication for the HTTP transport.

This server has a handful of known, trusted users (not the general public),
so a static per-user API key compared in constant time is enough -- no
external identity provider or OAuth authorization server is involved. Each
authorized user gets one long random token; requests must send it as
`x-api-key: <token>`.

Not `Authorization: Bearer <token>`: the MCP SDK's built-in bearer-auth
middleware reads that header, but `Authorization` is reserved by the client
platform once its own sign-in/OAuth is enabled -- it can't also carry this
server's static key. `XApiKeyAuthMiddleware` below checks `x-api-key`
instead, ahead of the SDK's request handling.

Tokens are only used over the streamable-http transport. The stdio transport
(local Claude Desktop / Claude Code usage) is unaffected -- stdio has no
network listener for an unauthenticated party to reach.
"""
import hmac
import json
import os

from mcp.server.auth.provider import AccessToken, TokenVerifier
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

MIN_TOKEN_LENGTH = 16


class ConfigError(Exception):
    """Raised when MCP_API_KEYS is missing or malformed. Fails startup, not a request."""


def parse_api_keys(raw: str) -> dict[str, str]:
    """Parse "name:token,name2:token2" (or bare "token,token2") into {name: token}."""
    keys: dict[str, str] = {}
    seen_tokens: set[str] = set()
    for i, entry in enumerate(part.strip() for part in raw.split(",")):
        if not entry:
            continue
        name, sep, token = entry.partition(":")
        if not sep:
            name, token = f"key-{i + 1}", name
        name, token = name.strip(), token.strip()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"MCP_API_KEYS entry {name!r} has no token, or the token is shorter than "
                f"{MIN_TOKEN_LENGTH} characters. Generate a strong one with:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if token in seen_tokens:
            raise ConfigError(f"MCP_API_KEYS: the token for {name!r} is a duplicate of another entry.")
        seen_tokens.add(token)
        keys[name] = token
    if not keys:
        raise ConfigError("MCP_API_KEYS is set but contains no usable entries.")
    return keys


class StaticApiKeyVerifier(TokenVerifier):
    """Validates an API key against a fixed set of keys."""

    def __init__(self, keys: dict[str, str]):
        self._keys = keys

    def __len__(self) -> int:
        return len(self._keys)

    @classmethod
    def from_env(cls, var_name: str = "MCP_API_KEYS") -> "StaticApiKeyVerifier":
        raw = os.environ.get(var_name, "")
        if not raw.strip():
            raise ConfigError(
                f"{var_name} is not set. The HTTP transport requires authentication -- set "
                f"{var_name} to one or more \"name:token\" pairs, comma-separated (see .env.example), "
                f"or set MCP_DISABLE_AUTH=true to explicitly run without auth for local testing only."
            )
        return cls(parse_api_keys(raw))

    async def verify_token(self, token: str) -> AccessToken | None:
        # Constant-time compare per candidate; timing across *which* key matched
        # isn't hidden, but that leaks nothing an attacker can use (key names
        # aren't secret) and matches the standard API-key pattern.
        for name, expected in self._keys.items():
            if hmac.compare_digest(token, expected):
                return AccessToken(token=token, client_id=name, scopes=["mcp"], subject=name)
        return None


class XApiKeyAuthMiddleware:
    """ASGI middleware requiring a valid key in the `x-api-key` header.

    Stands in for the MCP SDK's built-in `RequireAuthMiddleware`, which only
    checks `Authorization: Bearer <token>` -- see the module docstring for
    why that header isn't available here.
    """

    def __init__(self, app: ASGIApp, verifier: StaticApiKeyVerifier, exempt_paths: frozenset[str] = frozenset({"/health"})):
        self.app = app
        self.verifier = verifier
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        token = Headers(scope=scope).get("x-api-key")
        auth_info = await self.verifier.verify_token(token) if token else None

        if auth_info is None:
            await self._send_auth_error(send)
            return

        scope["state"] = {**scope.get("state", {}), "auth_client_id": auth_info.client_id}
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_auth_error(send: Send) -> None:
        body = json.dumps({"error": "invalid_token", "error_description": "Missing or invalid x-api-key header."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'ApiKey realm="mcp", header="x-api-key"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
