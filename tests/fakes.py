"""A fake farapi API and keychain the tests run the CLI against.

The API is an httpx.MockTransport handler, so nothing leaves the process
except the loopback sign-in tests, which talk to the CLI's own server.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from keyring.errors import KeyringError, PasswordDeleteError

from farapi_cli import catalog

API_URL = "http://api.test"
TASK_ID = "6f1c2a3e-0000-4000-8000-000000000001"

METADATA = {
    "issuer": API_URL,
    "authorization_endpoint": "http://web.test/dashboard/connect/authorize",
    "token_endpoint": f"{API_URL}/api/v1/oauth/token/",
    "device_authorization_endpoint": f"{API_URL}/api/v1/oauth/device-authorization/",
}
TOKENS = {"access_token": "a1", "refresh_token": "r1", "token_type": "Bearer", "expires_in": 900}
DEVICE = {
    "device_code": "dc1",
    "user_code": "WDJB-MJHT",
    "verification_uri": "http://web.test/dashboard/connect/device",
    "verification_uri_complete": "http://web.test/dashboard/connect/device?code=WDJB-MJHT",
    "expires_in": 600,
    "interval": 5,
}
ME = {"id": "u1", "email": "sara@acme.test", "full_name": "Sara Adel"}
ORGANIZATIONS = {
    "items": [
        {"slug": "acme", "name": "Acme", "permissions": ["tasks.view"]},
        {"slug": "beta", "name": "Beta", "permissions": []},
    ],
    "has_more": False,
}


def _tool(name: str, description: str, example: str, properties: dict[str, Any], **traits: Any):
    return {
        "name": name,
        "description": description,
        "example": example,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": traits.pop("required", []),
        },
        "read_only": traits.get("read_only", False),
        "destructive": traits.get("destructive", False),
        "org_scoped": traits.get("org_scoped", True),
    }


TOOLS = [
    _tool(
        "organizations.list",
        "The organizations you belong to.",
        "farapi organizations list",
        {},
        read_only=True,
        org_scoped=False,
    ),
    _tool(
        "tasks.list",
        "Tasks in a project, newest first.",
        "farapi tasks list --org acme --project ops --assignee me",
        {
            "project": {"type": "string", "description": "The project's slug."},
            "status": {"type": "string", "enum": ["todo", "in_progress", "in_review", "done"]},
            "assignee": {"type": "string", "description": "An email, or me."},
            "overdue": {"type": "boolean", "description": "Only tasks past their due date."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            "offset": {"type": "integer", "minimum": 0},
        },
        read_only=True,
    ),
    _tool(
        "tasks.create",
        "Create a task.",
        "tasks create --org acme --project ops --title 'Ship it'",
        {
            "project": {"type": "string"},
            "title": {"type": "string"},
            "assignees": {"type": "array", "items": {"type": "string", "format": "email"}},
        },
        required=["project", "title"],
    ),
    _tool(
        "tasks.update",
        "Change a task.",
        f"farapi tasks update --org acme --task {TASK_ID} --status done",
        {
            "task": {"type": "string", "format": "uuid"},
            "status": {"type": "string", "enum": ["todo", "in_progress", "in_review", "done"]},
            "due_date": {"type": ["string", "null"], "format": "date"},
        },
        required=["task"],
    ),
    _tool(
        "tasks.delete",
        "Delete a task and its subtasks.",
        f"farapi tasks delete --org acme --task {TASK_ID} --yes",
        {"task": {"type": "string", "format": "uuid"}},
        required=["task"],
        destructive=True,
    ),
    _tool(
        "tasks.subtasks.add",
        "Add a subtask to a task.",
        f"farapi tasks subtasks add --org acme --task {TASK_ID} --title 'Write tests'",
        {"task": {"type": "string", "format": "uuid"}, "title": {"type": "string"}},
        required=["task", "title"],
    ),
    _tool(
        "inbox.read_all",
        "Mark every notification read.",
        "farapi inbox read-all --org acme",
        {},
    ),
]

CHATS_SEND = _tool(
    "chats.send",
    "Send a message to a chat.",
    "farapi chats send --org acme --chat <id> --body 'Deployed.'",
    {"chat": {"type": "string", "format": "uuid"}, "body": {"type": "string"}},
    required=["chat", "body"],
)


class FakeApi:
    """Answers the endpoints the CLI calls; records every request.

    ``results`` maps a tool name to (status, body), a list of those (one per
    call), or an exception to raise. ``token`` holds the token endpoint's
    answers in order.
    """

    def __init__(self, tools: list[dict[str, Any]] | None = None, version: str = "v1") -> None:
        self.tools = list(TOOLS if tools is None else tools)
        self.version = version
        self.results: dict[str, Any] = {"organizations.list": (200, ORGANIZATIONS)}
        self.token: list[tuple[int, dict[str, Any]]] = []
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        headers = {"X-Tools-Version": self.version}
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=METADATA)
        if path == "/api/v1/oauth/token/":
            status, body = self.token.pop(0)
            return httpx.Response(status, json=body)
        if path == "/api/v1/oauth/device-authorization/":
            return httpx.Response(200, json=DEVICE)
        if path == "/api/v1/auth/logout/":
            return httpx.Response(204)
        if path == "/api/v1/auth/me/":
            return httpx.Response(200, json=ME, headers=headers)
        if path == "/api/v1/tools/":
            body = {"version": self.version, "tools": self.tools}
            return httpx.Response(200, json=body, headers=headers)
        if path.startswith("/api/v1/tools/"):
            name = path.split("/")[4]
            self.calls.append(
                (name, json.loads(request.content or b"{}"), request.headers.get("X-Active-Org"))
            )
            result = self.results.get(name, (200, {"ok": True}))
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, Exception):
                raise result
            status, body = result
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(404, json={"code": "not_found"})

    def tool_calls(self, name: str) -> list[tuple[str, dict[str, Any], str | None]]:
        return [c for c in self.calls if c[0] == name]


class MemoryKeyring:
    """A keychain that lives in a dict."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.items:
            raise PasswordDeleteError("nothing stored")
        del self.items[(service, username)]


class BrokenKeyring:
    """A keychain that refuses everything, like a locked or missing one."""

    def get_password(self, *_args: str) -> str | None:
        raise KeyringError("locked")

    def set_password(self, *_args: str) -> None:
        raise KeyringError("locked")

    def delete_password(self, *_args: str) -> None:
        raise KeyringError("locked")


def seed_catalog(directory: Any, tools: list[dict[str, Any]], version: str = "v1") -> None:
    """A catalog cached by an earlier run."""
    catalog.save(directory, catalog.Catalog(version=version, tools=tools))
