"""The tool catalog, cached per API URL (FR-14, NFR Speed).

Commands are built from the cache, so a normal run makes one request: the call
itself. Every API response carries X-Tools-Version; when it differs from the
cache, the catalog is fetched again for the next run. A command missing from
the cache triggers one fetch before it is reported unknown.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from farapi_cli.client import Client

FILENAME = "catalog.json"


@dataclass
class Catalog:
    version: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)

    def find(self, name: str) -> dict[str, Any] | None:
        return next((t for t in self.tools if t["name"] == name), None)

    def to_json(self) -> str:
        return json.dumps({"version": self.version, "tools": self.tools}, ensure_ascii=False)


def cache_path(directory: Path) -> Path:
    return directory / FILENAME


def cached(directory: Path) -> Catalog | None:
    try:
        data = json.loads(cache_path(directory).read_text(encoding="utf-8"))
        return Catalog(version=data.get("version", ""), tools=list(data.get("tools", [])))
    except (OSError, ValueError, AttributeError):
        return None


def save(directory: Path, catalog: Catalog) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    cache_path(directory).write_text(catalog.to_json(), encoding="utf-8")


def fetch(client: Client, directory: Path) -> Catalog:
    data = client.request("GET", "/api/v1/tools/") or {}
    catalog = Catalog(version=data.get("version", ""), tools=list(data.get("tools", [])))
    save(directory, catalog)
    return catalog


def load(client: Client, directory: Path, *, refresh: bool = False) -> Catalog:
    if not refresh:
        current = cached(directory)
        if current is not None:
            return current
    return fetch(client, directory)


def note_version(client: Client, directory: Path) -> None:
    """After a call: refetch for next time when the server's catalog moved on.
    Best effort; a failure here never fails the command that just succeeded."""
    seen = client.tools_version
    current = cached(directory)
    if not seen or (current is not None and current.version == seen):
        return
    with contextlib.suppress(Exception):  # the command already succeeded
        fetch(client, directory)
