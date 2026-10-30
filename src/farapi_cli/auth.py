"""Signing in, refreshing and signing out (FR-1 to FR-5, FR-7).

The CLI is the public OAuth client ``farapi-cli``. It finds every endpoint in
the authorization server's metadata (RFC 8414), so the only address it knows is
the API URL: pointing it at the local API also points it at the local web app.

- In a browser: authorization code with PKCE over a loopback redirect
  (RFC 8252 §7.3).
- Without one: the device authorization grant (RFC 8628).

A token request never carries a Bearer header: a stale one would be refused
before the request reached the token endpoint.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from farapi_cli.config import Credentials, CredentialStore
from farapi_cli.errors import NotSignedIn, SignInFailed, Unreachable

CLIENT_ID = "farapi-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
LOGIN_TIMEOUT = 300
DEVICE_NAME_MAX = 120

_metadata_cache: dict[str, dict[str, Any]] = {}


def device_name() -> str:
    """What the approval page and Connected apps call this machine."""
    return (socket.gethostname() or "")[:DEVICE_NAME_MAX]


def discover(http: httpx.Client, api_url: str) -> dict[str, Any]:
    """The authorization server metadata, cached for the process."""
    if api_url not in _metadata_cache:
        try:
            response = http.get(f"{api_url}/.well-known/oauth-authorization-server")
        except httpx.TransportError as exc:
            raise Unreachable(str(exc)) from exc
        if response.status_code != 200:
            raise SignInFailed("metadata_unavailable")
        _metadata_cache[api_url] = response.json()
    return _metadata_cache[api_url]


# PKCE ------------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """A 64-character verifier and its S256 challenge (RFC 7636 §4.1–4.2)."""
    verifier = secrets.token_urlsafe(48)
    return verifier, s256(verifier)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# Tokens ----------------------------------------------------------------------


def _credentials(payload: dict[str, Any]) -> Credentials:
    return Credentials(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        access_expires_at=time.time() + float(payload.get("expires_in", 0)),
    )


def _token_request(http: httpx.Client, endpoint: str, data: dict[str, str]) -> httpx.Response:
    try:
        return http.post(endpoint, data=data)
    except httpx.TransportError as exc:
        raise Unreachable(str(exc)) from exc


def _oauth_error(response: httpx.Response) -> str:
    try:
        return response.json().get("error") or "invalid_request"
    except ValueError:
        return "invalid_request"


# The browser flow ------------------------------------------------------------


@dataclass
class CallbackResult:
    code: str | None = None
    error: str | None = None


class LoopbackServer:
    """A one-shot HTTP server on 127.0.0.1 that receives the redirect.

    A request with the wrong state gets 400 and is otherwise ignored, so a
    stray or forged request can't end the sign-in. The right one gets a 302 to
    the web app's done page, so the browser shows a styled page in the user's
    language rather than plain English text from the CLI.
    """

    def __init__(self, state: str, done_url: Callable[[str], str]) -> None:
        self.state = state
        self.done_url = done_url
        self.result: CallbackResult | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                if not secrets.compare_digest(query.get("state", ""), outer.state):
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"This sign-in response doesn't match. Run farapi login again."
                    )
                    return
                if "code" in query:
                    outer.result = CallbackResult(code=query["code"])
                    target = outer.done_url("approved")
                else:
                    outer.result = CallbackResult(error=query.get("error", "access_denied"))
                    target = outer.done_url("denied")
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                # Keep the terminal clean: the CLI reports the outcome itself.
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.5

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/callback"

    def wait(self, timeout: float = LOGIN_TIMEOUT) -> CallbackResult:
        deadline = time.monotonic() + timeout
        try:
            while self.result is None and time.monotonic() < deadline:
                self.server.handle_request()
        finally:
            self.server.server_close()
        if self.result is None:
            raise SignInFailed("expired_token")
        return self.result


def done_url_for(authorization_endpoint: str) -> Callable[[str], str]:
    parts = urlsplit(authorization_endpoint)
    origin = f"{parts.scheme}://{parts.netloc}"
    return lambda result: f"{origin}/dashboard/connect/done?result={result}"


def login_browser(
    http: httpx.Client,
    api_url: str,
    store: CredentialStore,
    *,
    open_browser: Callable[[str], bool],
    echo: Callable[[str], None],
    timeout: float = LOGIN_TIMEOUT,
) -> None:
    meta = discover(http, api_url)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    loopback = LoopbackServer(state, done_url_for(meta["authorization_endpoint"]))
    url = (
        meta["authorization_endpoint"]
        + "?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": loopback.redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "device_name": device_name(),
            }
        )
    )
    echo("Opening your browser to approve this sign-in. If it doesn't open, visit:")
    echo(f"  {url}")
    open_browser(url)
    result = loopback.wait(timeout)
    if result.error:
        raise SignInFailed(result.error)
    response = _token_request(
        http,
        meta["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": result.code or "",
            "code_verifier": verifier,
            "redirect_uri": loopback.redirect_uri,
        },
    )
    if response.status_code != 200:
        raise SignInFailed(_oauth_error(response))
    store.save(_credentials(response.json()))


# The device flow -------------------------------------------------------------


def login_device(
    http: httpx.Client,
    api_url: str,
    store: CredentialStore,
    *,
    echo: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    meta = discover(http, api_url)
    try:
        response = http.post(
            meta["device_authorization_endpoint"],
            data={"client_id": CLIENT_ID, "device_name": device_name()},
        )
    except httpx.TransportError as exc:
        raise Unreachable(str(exc)) from exc
    if response.status_code != 200:
        raise SignInFailed(_oauth_error(response))
    grant = response.json()
    echo(f"On any device where you're signed in, open {grant['verification_uri']}")
    echo(f"and enter the code {grant['user_code']}")
    if grant.get("verification_uri_complete"):
        echo(f"(or open {grant['verification_uri_complete']})")

    interval = float(grant.get("interval") or 5)
    deadline = time.monotonic() + float(grant.get("expires_in") or 600)
    while time.monotonic() < deadline:
        sleep(interval)
        polled = _token_request(
            http,
            meta["token_endpoint"],
            {
                "grant_type": DEVICE_GRANT,
                "client_id": CLIENT_ID,
                "device_code": grant["device_code"],
            },
        )
        if polled.status_code == 200:
            store.save(_credentials(polled.json()))
            return
        error = _oauth_error(polled)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # RFC 8628 §3.5: every slow_down adds five seconds for good.
            interval += 5
            continue
        raise SignInFailed(error)
    raise SignInFailed("expired_token")


# Refresh and sign-out --------------------------------------------------------

_refresh_lock = threading.Lock()


def refresh(http: httpx.Client, api_url: str, store: CredentialStore) -> Credentials:
    """A fresh access token for the stored sign-in. A refused refresh means the
    sign-in is over (revoked on the web, or logged out): forget it."""
    with _refresh_lock:
        current = store.load()
        if current is None:
            raise NotSignedIn()
        meta = discover(http, api_url)
        response = _token_request(
            http,
            meta["token_endpoint"],
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": current.refresh_token,
            },
        )
        if response.status_code == 200:
            fresh = _credentials(response.json())
            store.save(fresh)
            return fresh
        if response.status_code in (400, 401):
            store.delete()
            raise NotSignedIn()
        raise Unreachable(f"token endpoint answered {response.status_code}")


def logout(http: httpx.Client, api_url: str, store: CredentialStore) -> bool:
    """End the session on the server, then forget it here whatever the answer.
    Returns whether there was a sign-in to end."""
    current = store.load()
    if current is None:
        return False
    with contextlib.suppress(httpx.TransportError):  # forgotten here either way
        http.post(f"{api_url}/api/v1/auth/logout/", json={"refresh": current.refresh_token})
    store.delete()
    return True
