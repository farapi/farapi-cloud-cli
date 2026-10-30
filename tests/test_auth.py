"""Signing in and keeping the sign-in (FR-1 to FR-4, FR-7, FR-32)."""

from __future__ import annotations

import stat
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fakes import API_URL, TOKENS, BrokenKeyring, FakeApi, MemoryKeyring

from farapi_cli import auth
from farapi_cli.client import make_http
from farapi_cli.config import DEFAULT_API_URL, Credentials, CredentialStore, cache_dir, config_dir
from farapi_cli.errors import NotSignedIn, SignInFailed


def _http(api: FakeApi) -> httpx.Client:
    return make_http(httpx.MockTransport(api))


def test_the_pkce_challenge_is_the_s256_of_the_verifier():
    # RFC 7636 appendix B.
    assert auth.s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    verifier, challenge = auth.pkce_pair()
    assert len(verifier) == 64
    assert challenge == auth.s256(verifier)
    assert auth.pkce_pair()[0] != verifier


def test_the_loopback_server_rejects_a_wrong_state_and_keeps_waiting():
    server = auth.LoopbackServer("right", lambda result: f"http://web.test/done?result={result}")
    callback = server.redirect_uri
    results = []
    waiter = threading.Thread(target=lambda: results.append(server.wait(10)))
    waiter.start()
    with httpx.Client(trust_env=False) as http:
        elsewhere = http.get(callback.replace("/callback", "/favicon.ico"))
        wrong = http.get(callback, params={"code": "stolen", "state": "wrong"})
        right = http.get(callback, params={"code": "c1", "state": "right"})
    waiter.join(10)
    assert elsewhere.status_code == 404
    assert wrong.status_code == 400
    assert right.status_code == 302
    assert right.headers["location"] == "http://web.test/done?result=approved"
    assert results[0].code == "c1"


def test_a_denied_sign_in_sends_the_browser_to_the_denied_page():
    server = auth.LoopbackServer("s", lambda result: f"http://web.test/done?result={result}")
    callback = server.redirect_uri
    results = []
    waiter = threading.Thread(target=lambda: results.append(server.wait(10)))
    waiter.start()
    with httpx.Client(trust_env=False) as http:
        denied = http.get(callback, params={"error": "access_denied", "state": "s"})
    waiter.join(10)
    assert denied.headers["location"] == "http://web.test/done?result=denied"
    assert results[0].error == "access_denied"


def test_browser_sign_in_exchanges_the_code_with_its_verifier(api, keychain):
    store = CredentialStore(API_URL, backend=keychain)
    api.token = [(200, TOKENS)]
    opened: list[dict[str, list[str]]] = []

    def open_browser(url: str) -> bool:
        query = parse_qs(urlsplit(url).query)
        opened.append(query)
        callback = f"{query['redirect_uri'][0]}?code=c1&state={query['state'][0]}"
        threading.Thread(target=httpx.get, args=(callback,), kwargs={"trust_env": False}).start()
        return True

    auth.login_browser(
        _http(api), API_URL, store, open_browser=open_browser, echo=lambda _text: None, timeout=10
    )
    query = opened[0]
    assert query["client_id"] == ["farapi-cli"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"][0].startswith("http://127.0.0.1:")
    sent = parse_qs(api.requests[-1].content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["c1"]
    assert auth.s256(sent["code_verifier"][0]) == query["code_challenge"][0]
    assert store.load().access_token == "a1"


def test_device_polling_waits_the_interval_and_backs_off_on_slow_down(api, keychain):
    store = CredentialStore(API_URL, backend=keychain)
    api.token = [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, TOKENS),
    ]
    sleeps: list[float] = []
    printed: list[str] = []
    auth.login_device(_http(api), API_URL, store, echo=printed.append, sleep=sleeps.append)
    assert sleeps == [5, 5, 10]
    assert any("WDJB-MJHT" in line for line in printed)
    polls = [r for r in api.requests if r.url.path == "/api/v1/oauth/token/"]
    assert parse_qs(polls[0].content.decode())["device_code"] == ["dc1"]
    assert all("Authorization" not in r.headers for r in polls)
    assert store.load().refresh_token == "r1"


def test_a_denied_device_sign_in_fails_with_its_code(api, keychain):
    api.token = [(400, {"error": "access_denied"})]
    with pytest.raises(SignInFailed) as failed:
        auth.login_device(
            _http(api),
            API_URL,
            CredentialStore(API_URL, backend=keychain),
            echo=lambda _text: None,
            sleep=lambda _seconds: None,
        )
    assert failed.value.code == "access_denied"
    assert keychain.items == {}


def test_a_refused_refresh_forgets_the_sign_in(api, keychain):
    store = CredentialStore(API_URL, backend=keychain)
    store.save(Credentials("a0", "r0", 0))
    api.token = [(400, {"error": "invalid_grant"})]
    with pytest.raises(NotSignedIn):
        auth.refresh(_http(api), API_URL, store)
    assert store.load() is None


def test_credentials_fall_back_to_a_private_file_without_a_keychain(capsys):
    store = CredentialStore(API_URL, backend=BrokenKeyring())
    credentials = Credentials("a1", "r1", 1234.5)
    store.save(credentials)
    path = config_dir() / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "no system keychain" in capsys.readouterr().err
    assert CredentialStore(API_URL, backend=BrokenKeyring()).load() == credentials
    store.delete()
    assert store.load() is None


def test_credentials_for_two_api_urls_are_kept_apart():
    keychain = MemoryKeyring()
    local = CredentialStore("http://localhost:8030", backend=keychain)
    production = CredentialStore(DEFAULT_API_URL, backend=keychain)
    local.save(Credentials("local", "r-local", 1.0))
    production.save(Credentials("prod", "r-prod", 2.0))
    assert local.load().access_token == "local"
    assert production.load().access_token == "prod"
    local.delete()
    assert local.load() is None
    assert production.load().access_token == "prod"
    assert cache_dir("http://localhost:8030") != cache_dir(DEFAULT_API_URL)

    # The same holds in the fallback file.
    files = [
        CredentialStore(url, backend=BrokenKeyring()) for url in ("http://a.test", "http://b.test")
    ]
    files[0].save(Credentials("a", "ra", 1.0))
    files[1].save(Credentials("b", "rb", 1.0))
    files[0].delete()
    assert files[0].load() is None
    assert files[1].load().access_token == "b"
