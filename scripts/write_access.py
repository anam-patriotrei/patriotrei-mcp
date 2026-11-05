"""The buildium_write tool: create or change data in Buildium.

The server uses ONE Buildium API key for everyone; that key needs Edit
permissions in Buildium for whatever writers should be able to change.
Who may write (by signed-in email) is set in access_rules.py. Everyone else
is read-only. MCP_WRITES_ENABLED=false on Render stops ALL writes at once.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

import access_rules
from buildium_client import BuildiumClient

logger = logging.getLogger("buildium-mcp.write")

# Who may write lives in access_rules.py (WRITE_PERMISSIONS).

ALL_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PREVIEW_TTL = 15 * 60


class WriteNotAllowed(Exception):
    pass


def caller_name(ctx) -> str | None:
    """Name of the API key behind this request (set by XApiKeyAuthMiddleware).
    stdio has no keys: it is the local, single-user process -> "local"."""
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    if request is None:
        return "local"
    scope = getattr(request, "scope", None) or {}
    return (scope.get("state") or {}).get("auth_client_id")


def check_allowed(caller: str | None, method: str) -> None:
    if os.environ.get("MCP_WRITES_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        raise WriteNotAllowed("Changing data in Buildium is turned off on this server (MCP_WRITES_ENABLED=false).")
    allowed = access_rules.write_methods(caller)
    if not allowed:
        raise WriteNotAllowed("You have read-only access to Buildium. Ask Anam if you need to change data.")
    if method not in allowed:
        raise WriteNotAllowed(f"You aren't allowed to use {method}. Allowed: {', '.join(sorted(allowed))}.")


def clean_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    if "://" in path or ".." in path or "?" in path or "#" in path:
        raise ValueError("path must be a plain Buildium API path like /workorders (put query values in params).")
    return path


# --- One-time preview codes -------------------------------------------------
# The real write must send back the code from its own preview. The code is an
# HMAC over (caller, time, random part, exact request), so it can't be guessed
# or reused by someone else, stops matching if anything changes, works once,
# and expires after 15 minutes. The secret is new on every restart: after a
# redeploy or Render sleep, just preview again.
_SECRET = secrets.token_bytes(32)
_used: dict[str, float] = {}
_lock = threading.Lock()


def _sign(caller, issued, nonce, req) -> str:
    msg = json.dumps([caller, issued, nonce, req], sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_SECRET, msg, hashlib.sha256).hexdigest()[:32]


def make_code(caller: str, req: dict, now: float | None = None) -> str:
    issued, nonce = int(now if now is not None else time.time()), secrets.token_hex(4)
    return f"{issued}.{nonce}.{_sign(caller, issued, nonce, req)}"


def use_code(code: str | None, caller: str, req: dict, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    parts = (code or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        raise WriteNotAllowed("Missing preview_code. Call buildium_write with confirm=false first and show the user the preview.")
    issued_s, nonce, sig = parts
    if not hmac.compare_digest(sig, _sign(caller, int(issued_s), nonce, req)):
        raise WriteNotAllowed("The preview_code doesn't match this request. Something changed since the preview; preview again.")
    if now - int(issued_s) > PREVIEW_TTL:
        raise WriteNotAllowed("The preview expired. Preview again and ask the user to confirm.")
    with _lock:
        for c, t in list(_used.items()):
            if now - t > PREVIEW_TTL:
                del _used[c]
        if code in _used:
            raise WriteNotAllowed("This preview was already used, so the change was already made. Preview again to do it again.")
        _used[code] = now


def buildium_write(ctx, method: str, path: str, body: dict | list | None = None,
                   params: dict | None = None, confirm: bool = False, preview_code: str | None = None) -> dict:
    caller = caller_name(ctx)
    method = (method or "").strip().upper()
    try:
        if method not in ALL_WRITE_METHODS:
            raise ValueError(f"method must be one of {', '.join(sorted(ALL_WRITE_METHODS))} (use buildium_get to read).")
        path = clean_path(path)
        check_allowed(caller, method)
        req = {"method": method, "path": path, "body": body, "params": params or {}}
        if confirm:
            use_code(preview_code, caller, req)
    except (WriteNotAllowed, ValueError) as exc:
        logger.info("buildium_write refused caller=%s %s %s: %s", caller, method, path, exc)
        return {"status": "refused", "reason": str(exc)}

    if not confirm:
        return {
            "status": "preview",
            "message": (
                "Nothing has been changed yet. Show the user this request in plain words and ask them to confirm. "
                "Only if they clearly confirm, call buildium_write again with exactly the same method, path, body "
                "and params, plus confirm=true and this preview_code. It works once and expires in 15 minutes."
            ),
            "preview_code": make_code(caller, req),
            "will_send": req,
        }

    result = BuildiumClient().write(method, path, body=body, params=params)
    logger.info("WRITE caller=%s %s %s -> id=%s", caller, method, path,
                result.get("Id") if isinstance(result, dict) else None)
    return {"status": "done", "result": result}
