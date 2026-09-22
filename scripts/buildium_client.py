"""Thin client for the Buildium Open API (https://developer.buildium.com).

Auth is two headers, x-buildium-client-id / x-buildium-client-secret
(no OAuth). Buildium enforces ~10 concurrent requests/sec; this client
calls sequentially and backs off on 429s.
"""
import os
import time

import requests


class BuildiumError(Exception):
    pass


class BuildiumClient:
    BASE_URL = "https://api.buildium.com/v1"

    def __init__(self, client_id=None, client_secret=None):
        client_id = client_id or os.environ.get("BUILDIUM_CLIENT_ID")
        client_secret = client_secret or os.environ.get("BUILDIUM_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise BuildiumError(
                "Missing Buildium credentials. Set BUILDIUM_CLIENT_ID and"
                "BUILDIUM_CLIENT_SECRET (e.g. in a .env file copied from .env.example)."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-buildium-client-id": client_id,
                "x-buildium-client-secret": client_secret,
                "Accept": "application/json",
            }
        )

    def _request(self, method, path, **kwargs):
        url = f"{self.BASE_URL}{path}"
        for attempt in range(5):
            resp = self.session.request(method, url, timeout=60, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise BuildiumError(f"{method} {path} -> {resp.status_code}: {resp.text[:800]}")
            return resp
        raise BuildiumError(f"{method} {path} -> repeatedly rate-limited (429)")

    def get(self, path, params=None):
        resp = self._request("GET", path, params=params or {})
        if not resp.content:
            return None
        return resp.json()

    def get_all(self, path, params=None, page_size=500):
        """Paginate a Buildium list endpoint using offset/limit."""
        params = dict(params or {})
        params["limit"] = page_size
        offset = 0
        results = []
        while True:
            params["offset"] = offset
            batch = self.get(path, params=params) or []
            results.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return results
