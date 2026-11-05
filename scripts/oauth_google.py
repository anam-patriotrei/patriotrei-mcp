"""Sign in with Google for the Claude connector (OAuth), so the server knows
each person's real email.

Flow: Claude -> /authorize (this server) -> Google sign-in -> /oauth/google/callback
(this server checks the email is allowed to read) -> back to Claude with a code ->
Claude swaps it at /token for an access token that carries the email.

Stateless on purpose (Render's free plan restarts and has no disk): codes and
tokens are signed with MCP_OAUTH_SECRET instead of being stored, so people stay
signed in across restarts. Removing someone from access_rules.py cuts them off
at their next request, because every token is re-checked against the rules.

Env:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  Google Cloud OAuth client (web app)
  MCP_OAUTH_SECRET                        long random string used to sign tokens
  MCP_PUBLIC_URL                          e.g. https://buildium-mcp.onrender.com
  MCP_OAUTH_REDIRECTS (optional)          allowed client callback URLs, comma-separated
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlencode

import requests
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import access_rules

logger = logging.getLogger("buildium-mcp.oauth")

# Overridable only so tests can point at a fake Google; never set these on Render.
GOOGLE_AUTH_URL = os.environ.get("TEST_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URL = os.environ.get("TEST_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
GOOGLE_USERINFO_URL = os.environ.get("TEST_GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo")
CALLBACK_PATH = "/oauth/google/callback"

ACCESS_TTL = 60 * 60            # 1 hour
REFRESH_TTL = 30 * 24 * 3600    # 30 days, then sign in again
CODE_TTL = 5 * 60
LOGIN_TTL = 10 * 60
SCOPE = "buildium"
DEFAULT_REDIRECTS = "https://claude.ai/api/mcp/auth_callback,https://claude.com/api/mcp/auth_callback"


class ConfigError(Exception):
    pass


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID"))


# --- signed, self-contained tokens --------------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Signer:
    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ConfigError("MCP_OAUTH_SECRET must be at least 32 characters.")
        self._key = secret.encode()

    def sign(self, kind: str, data: dict, ttl: int) -> str:
        body = _b64(json.dumps({**data, "k": kind, "exp": int(time.time()) + ttl, "n": secrets.token_hex(6)},
                               separators=(",", ":")).encode())
        mac = _b64(hmac.new(self._key, f"{kind}.{body}".encode(), hashlib.sha256).digest())
        return f"{kind}.{body}.{mac}"

    def open(self, kind: str, token: str) -> dict | None:
        try:
            k, body, mac = (token or "").split(".")
        except ValueError:
            return None
        good = _b64(hmac.new(self._key, f"{k}.{body}".encode(), hashlib.sha256).digest())
        if k != kind or not hmac.compare_digest(mac, good):
            return None
        data = json.loads(_unb64(body))
        if data.get("exp", 0) < time.time():
            return None
        return data


# --- the OAuth provider the MCP SDK calls ---------------------------------------
class GoogleOAuthProvider:
    def __init__(self, public_url: str, google_id: str, google_secret: str, signing_secret: str,
                 allowed_redirects: list[str]):
        self.public_url = public_url.rstrip("/")
        self.google_id, self.google_secret = google_id, google_secret
        self.signer = Signer(signing_secret)
        self.allowed_redirects = allowed_redirects
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._used_codes: dict[str, float] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "GoogleOAuthProvider":
        missing = [v for v in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MCP_OAUTH_SECRET", "MCP_PUBLIC_URL")
                   if not os.environ.get(v)]
        if missing:
            raise ConfigError(f"Google sign-in is on but these are missing: {', '.join(missing)}")
        redirects = [r.strip() for r in os.environ.get("MCP_OAUTH_REDIRECTS", DEFAULT_REDIRECTS).split(",") if r.strip()]
        return cls(os.environ["MCP_PUBLIC_URL"], os.environ["GOOGLE_CLIENT_ID"], os.environ["GOOGLE_CLIENT_SECRET"],
                   os.environ["MCP_OAUTH_SECRET"], redirects)

    # Clients (Claude registers itself). Kept in memory; after a restart an unknown
    # client_id still works as a public client limited to the allowed callback URLs.
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id) or OAuthClientInformationFull(
            client_id=client_id, redirect_uris=[AnyUrl(u) for u in self.allowed_redirects],
            token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or []) if str(u) not in self.allowed_redirects]
        if bad:
            raise RegistrationError("invalid_redirect_uri", f"Callback not allowed: {', '.join(bad)}")
        self._clients[client_info.client_id] = client_info

    # Step 1: send the person to Google, remembering Claude's request in a signed state.
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.signer.sign("login", {
            "cid": client.client_id, "ru": str(params.redirect_uri), "rue": params.redirect_uri_provided_explicitly,
            "cc": params.code_challenge, "st": params.state, "res": params.resource,
        }, LOGIN_TTL)
        return f"{GOOGLE_AUTH_URL}?" + urlencode({
            "client_id": self.google_id, "redirect_uri": self.public_url + CALLBACK_PATH,
            "response_type": "code", "scope": "openid email", "state": state, "prompt": "select_account",
        })

    # Step 2: Google sends the person back here.
    async def google_callback(self, request: Request):
        login = self.signer.open("login", request.query_params.get("state", ""))
        if not login:
            return HTMLResponse("This sign-in link expired. Go back to Claude and try connecting again.", 400)
        if request.query_params.get("error"):
            return RedirectResponse(construct_redirect_uri(login["ru"], error="access_denied", state=login["st"]), 302)
        email = self._google_email(request.query_params.get("code", ""))
        if not email:
            return HTMLResponse("Google sign-in failed. Go back to Claude and try again.", 400)
        if not access_rules.can_read(email):
            logger.info("sign-in refused for %s (not on the access list)", email)
            return HTMLResponse(f"{email} doesn't have access to the Buildium connector. Ask Anam to add you.", 403)
        code = self.signer.sign("code", {**{k: login[k] for k in ("cid", "ru", "rue", "cc", "res")}, "email": email},
                                CODE_TTL)
        logger.info("sign-in ok for %s", email)
        return RedirectResponse(construct_redirect_uri(login["ru"], code=code, state=login["st"]), 302)

    def _google_email(self, google_code: str) -> str | None:
        try:
            tok = requests.post(GOOGLE_TOKEN_URL, timeout=20, data={
                "code": google_code, "client_id": self.google_id, "client_secret": self.google_secret,
                "redirect_uri": self.public_url + CALLBACK_PATH, "grant_type": "authorization_code",
            }).json()
            info = requests.get(GOOGLE_USERINFO_URL, timeout=20,
                                headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        except Exception as exc:  # network / bad code
            logger.warning("google exchange failed: %s", type(exc).__name__)
            return None
        if not info.get("email") or not info.get("email_verified"):
            return None
        return info["email"].strip().lower()

    # Step 3: Claude swaps the code for tokens.
    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        d = self.signer.open("code", authorization_code)
        if not d or d["cid"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, scopes=[SCOPE], expires_at=d["exp"], client_id=d["cid"],
                                 code_challenge=d["cc"], redirect_uri=AnyUrl(d["ru"]),
                                 redirect_uri_provided_explicitly=d["rue"], resource=d.get("res"), subject=d["email"])

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        with self._lock:
            now = time.time()
            for c, t in list(self._used_codes.items()):
                if t < now:
                    del self._used_codes[c]
            if authorization_code.code in self._used_codes:
                raise TokenError("invalid_grant", "This sign-in code was already used.")
            self._used_codes[authorization_code.code] = authorization_code.expires_at
        return self._issue(client.client_id, authorization_code.subject, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        d = self.signer.open("refresh", refresh_token)
        if not d or d["cid"] != client.client_id or not access_rules.can_read(d["email"]):
            return None
        return RefreshToken(token=refresh_token, client_id=d["cid"], scopes=[SCOPE], expires_at=d["exp"],
                            resource=d.get("res"), subject=d["email"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        return self._issue(client.client_id, refresh_token.subject, refresh_token.resource)

    def _issue(self, client_id: str, email: str, resource: str | None) -> OAuthToken:
        base = {"cid": client_id, "email": email, "res": resource}
        return OAuthToken(access_token=self.signer.sign("access", base, ACCESS_TTL), expires_in=ACCESS_TTL,
                          scope=SCOPE, refresh_token=self.signer.sign("refresh", base, REFRESH_TTL))

    # Every request: who is this?
    async def load_access_token(self, token: str) -> AccessToken | None:
        d = self.signer.open("access", token)
        if not d or not access_rules.can_read(d["email"]):
            return None
        return AccessToken(token=token, client_id=d["cid"], scopes=[SCOPE], expires_at=d["exp"],
                           resource=d.get("res"), subject=d["email"])

    async def revoke_token(self, token) -> None:  # stateless: removing the email from access_rules revokes
        return None
