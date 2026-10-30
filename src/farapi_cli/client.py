"""The API client every command goes through (FR-7, FR-30, FR-31).

It refreshes the access token shortly before it lapses (the web proxy's
30-second margin), retries once after a 401, and sends the organization a call
names as the X-Active-Org header the web and mobile apps already use (D-2).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from farapi_cli import __version__, auth
from farapi_cli.config import Credentials, CredentialStore
from farapi_cli.errors import ApiError, NotSignedIn, Unreachable

REFRESH_MARGIN = 30
TIMEOUT = 20.0


def make_http(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        transport=transport,
        headers={"User-Agent": f"farapi-cli/{__version__}"},
    )


class Client:
    def __init__(
        self,
        api_url: str,
        store: CredentialStore,
        http: httpx.Client,
        *,
        org: str | None = None,
    ) -> None:
        self.api_url = api_url
        self.store = store
        self.http = http
        self.org = org
        # The catalog version the server last reported (FR-14).
        self.tools_version: str | None = None

    def _credentials(self) -> Credentials:
        current = self.store.load()
        if current is None:
            raise NotSignedIn()
        if current.access_expires_at - time.time() < REFRESH_MARGIN:
            current = auth.refresh(self.http, self.api_url, self.store)
        return current

    def _send(self, method: str, path: str, token: str, body: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if self.org:
            headers["X-Active-Org"] = self.org
        try:
            return self.http.request(method, f"{self.api_url}{path}", headers=headers, json=body)
        except httpx.TransportError as exc:
            raise Unreachable(str(exc)) from exc

    def request(self, method: str, path: str, *, json: Any = None) -> Any:
        response = self._send(method, path, self._credentials().access_token, json)
        if response.status_code == 401:
            # The access token died early (a revoked or restarted session):
            # one refresh, one retry. A refused refresh raises NotSignedIn.
            fresh = auth.refresh(self.http, self.api_url, self.store)
            response = self._send(method, path, fresh.access_token, json)
            if response.status_code == 401:
                self.store.delete()
                raise NotSignedIn()
        if "X-Tools-Version" in response.headers:
            self.tools_version = response.headers["X-Tools-Version"]
        if response.status_code == 204:
            return None
        if response.is_success:
            return response.json() if response.content else None
        raise _api_error(response)

    def call_tool(self, name: str, data: dict[str, Any]) -> Any:
        return self.request("POST", f"/api/v1/tools/{name}/", json=data)


def _api_error(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return ApiError(
        response.status_code,
        body.get("code") or f"http_{response.status_code}",
        body.get("params"),
        body.get("fields"),
    )
