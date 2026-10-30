"""Where the CLI talks to, and where it keeps its sign-in (FR-4, FR-32).

Everything is keyed by the API URL, so the local stack and production stay
signed in side by side. The CLI never stores a password: only the token pair
the authorization server issued.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError

DEFAULT_API_URL = "https://api.cloud.farapi.ai"
KEYRING_SERVICE = "farapi-cli"


def api_url(override: str | None = None) -> str:
    """--api-url, else FARAPI_API_URL, else production."""
    raw = override or os.environ.get("FARAPI_API_URL") or DEFAULT_API_URL
    return raw.strip().rstrip("/")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "farapi"


def cache_dir(url: str) -> Path:
    """Per-URL cache (the catalog), so two servers never share one."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "farapi" / hashlib.sha256(url.encode()).hexdigest()[:12]


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    # Seconds since the epoch, so the client can refresh before it lapses.
    access_expires_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Credentials | None:
        try:
            data = json.loads(raw)
            return cls(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                access_expires_at=float(data["access_expires_at"]),
            )
        except (ValueError, KeyError, TypeError):
            return None


def _keyring_usable(backend: Any) -> bool:
    """keyring falls back to a backend that refuses everything when the system
    has no keychain; treat that as no keychain at all."""
    if backend is not keyring:
        return True
    current = keyring.get_keyring()
    module = type(current).__module__
    return not module.startswith("keyring.backends.fail") and not module.endswith(".null")


class CredentialStore:
    """The OS keychain (service farapi-cli, user = the API URL), or, where none
    exists, a JSON file only the user can read."""

    def __init__(self, url: str, *, backend: Any = keyring, file_path: Path | None = None) -> None:
        self.url = url
        self.backend = backend
        self.file_path = file_path or config_dir() / "credentials.json"
        self._warned = False

    # The keychain ----------------------------------------------------------

    def _keyring_get(self) -> str | None:
        if not _keyring_usable(self.backend):
            raise KeyringError("no usable keychain")
        return self.backend.get_password(KEYRING_SERVICE, self.url)

    def _keyring_set(self, value: str) -> None:
        if not _keyring_usable(self.backend):
            raise KeyringError("no usable keychain")
        self.backend.set_password(KEYRING_SERVICE, self.url, value)

    def _keyring_delete(self) -> None:
        if not _keyring_usable(self.backend):
            raise KeyringError("no usable keychain")
        with contextlib.suppress(KeyringError):  # nothing stored is fine
            self.backend.delete_password(KEYRING_SERVICE, self.url)

    # The fallback file -----------------------------------------------------

    def _read_file(self) -> dict[str, str]:
        try:
            return json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_file(self, data: dict[str, str]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.file_path.parent, 0o700)
        fd = os.open(self.file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.chmod(self.file_path, 0o600)

    def _warn_fallback(self) -> None:
        if not self._warned:
            self._warned = True
            print(
                f"farapi: no system keychain; the sign-in is kept in {self.file_path} "
                "(readable only by you).",
                file=sys.stderr,
            )

    # The store -------------------------------------------------------------

    def load(self) -> Credentials | None:
        try:
            raw = self._keyring_get()
            if raw:
                return Credentials.from_json(raw)
        except KeyringError:
            pass
        raw = self._read_file().get(self.url)
        return Credentials.from_json(raw) if raw else None

    def save(self, credentials: Credentials) -> None:
        try:
            self._keyring_set(credentials.to_json())
            return
        except KeyringError:
            self._warn_fallback()
        data = self._read_file()
        data[self.url] = credentials.to_json()
        self._write_file(data)

    def delete(self) -> None:
        with contextlib.suppress(KeyringError):
            self._keyring_delete()
        data = self._read_file()
        if self.url in data:
            del data[self.url]
            self._write_file(data)
