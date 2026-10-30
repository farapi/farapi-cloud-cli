"""Every test runs with its own config and cache folders, an in-memory
keychain and the fake API, so nothing touches the real ones."""

from __future__ import annotations

import io
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fakes import API_URL, FakeApi, MemoryKeyring

from farapi_cli import auth
from farapi_cli.client import make_http
from farapi_cli.config import Credentials, CredentialStore, cache_dir
from farapi_cli.main import App, main


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("FARAPI_API_URL", raising=False)
    # Nobody at the keyboard unless a test says so.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    auth._metadata_cache.clear()


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def keychain() -> MemoryKeyring:
    return MemoryKeyring()


def make_app(api: FakeApi, keychain: MemoryKeyring) -> App:
    return App(
        API_URL,
        CredentialStore(API_URL, backend=keychain),
        make_http(httpx.MockTransport(api)),
        cache_dir(API_URL),
    )


@pytest.fixture
def signed_out(api, keychain) -> App:
    return make_app(api, keychain)


@pytest.fixture
def app(signed_out) -> App:
    signed_out.store.save(Credentials("access", "refresh", time.time() + 3600))
    return signed_out


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> Any:
        return json.loads(self.out)


@pytest.fixture
def run(capsys, monkeypatch):
    """main() as the shell would run it; tty=True is a terminal, else piped."""

    def invoke(app: App, *args: str, tty: bool = False) -> Result:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
        code = main(list(args), app=app)
        out, err = capsys.readouterr()
        return Result(code, out, err)

    return invoke
