"""What a command prints (FR-27 to FR-30).

Piped, or with --json, a command prints exactly one JSON document on stdout:
the result, or {"error": {...}}. Claude and scripts parse that and branch on
the exit code. In a terminal it prints tables and sentences for a person, and
errors go to stderr.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

import click

from farapi_cli.errors import (
    API_ERROR,
    USAGE,
    ApiError,
    NotSignedIn,
    SignInFailed,
    Unreachable,
)

# One English sentence per code a tool or sign-in can end in (FR-29, D-9).
# {name} placeholders are filled from the error's params; unknown codes print
# as themselves.
ERRORS: dict[str, str] = {
    "not_found": "Not found in this organization, or you can't reach it.",
    "task_not_found": "No such task in this organization, or you can't reach it.",
    "workspace_not_found": "No such project in this organization, or you can't reach it.",
    "chat_not_found": "No such chat in this organization, or you're not in it.",
    "message_not_found": "No such message in this chat.",
    "notification_not_found": "No such notification in your inbox.",
    "member_not_found": "No such member in this organization.",
    "assignee_not_found": "{email} is not a member of this organization.",
    "assignee_no_access": "Someone you assigned can't reach this project.",
    "title_required": "A task needs a title.",
    "workspace_and_title_required": "A task needs a project and a title.",
    "message_empty": "A message can't be empty.",
    "organization_required": "Name the organization with --org.",
    "organization_not_found": "You're not a member of that organization.",
    "invalid_organization_header": "That isn't an organization slug or id.",
    "permission_denied": "You don't have permission to do that in this organization.",
    "tool_not_found": "This command isn't available to the CLI.",
    "validation_error": "Some input isn't valid.",
    "session_invalid": "Your sign-in has ended. Run farapi login.",
    "not_signed_in": "You're not signed in. Run farapi login.",
    "unreachable": "Can't reach the farapi server. Check your connection or --api-url.",
    "access_denied": "The sign-in was denied.",
    "expired_token": "The sign-in timed out. Run farapi login again.",
    "invalid_grant": "The sign-in didn't go through. Run farapi login again.",
    "invalid_client": "The server doesn't know this CLI. Is --api-url right?",
    "metadata_unavailable": "That server doesn't offer CLI sign-in. Is --api-url right?",
}

# The columns a table prefers, in order; anything else scalar comes after.
PREFERRED = (
    # A task's ID, RQW-01, leads its row (spec 20 FR-14).
    "key",
    "slug",
    "name",
    "title",
    "full_name",
    "email",
    "status",
    "priority",
    "due_date",
    "kind",
    "unread",
    "body",
    "read_at",
    "created_at",
    "id",
)
MAX_COLUMNS = 6
MAX_CELL = 48


def json_mode(flag: bool = False) -> bool:
    return flag or not sys.stdout.isatty()


def dumps(value: Any) -> str:
    # Arabic stays readable characters, never \u escapes (Languages & RTL).
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


# Errors ----------------------------------------------------------------------


def sentence(code: str, params: dict[str, Any] | None = None) -> str:
    template = ERRORS.get(code)
    if template is None:
        return code
    try:
        return template.format(**(params or {}))
    except (KeyError, IndexError):
        return template


def error_document(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ApiError):
        doc: dict[str, Any] = {"code": exc.code}
        if exc.params:
            doc["params"] = exc.params
        if exc.fields:
            doc["fields"] = exc.fields
        return {"error": doc}
    if isinstance(exc, (NotSignedIn, Unreachable, SignInFailed)):
        return {"error": {"code": exc.code}}
    if isinstance(exc, click.UsageError):
        params = getattr(exc, "params", None)
        doc = {
            "code": getattr(exc, "code", "usage_error"),
            "params": {"message": exc.format_message()},
        }
        if params:
            doc["params"].update(params)
        return {"error": doc}
    return {"error": {"code": "error", "params": {"message": str(exc)}}}


def error_text(exc: BaseException) -> str:
    if isinstance(exc, ApiError):
        lines = [sentence(exc.code, exc.params)]
        for name, codes in (exc.fields or {}).items():
            flag = "--" + str(name).replace("_", "-")
            listed = ", ".join(codes) if isinstance(codes, list) else str(codes)
            lines.append(f"  {flag}: {listed}")
        return "\n".join(lines)
    if isinstance(exc, (NotSignedIn, Unreachable, SignInFailed)):
        return sentence(exc.code)
    return str(exc)


def exit_code(exc: BaseException) -> int:
    if isinstance(exc, click.UsageError):
        return USAGE
    return getattr(exc, "exit_code", API_ERROR)


def report_error(exc: BaseException, as_json: bool) -> int:
    if as_json:
        click.echo(dumps(error_document(exc)))
    elif isinstance(exc, click.UsageError):
        exc.show()
    else:
        click.echo(f"Error: {error_text(exc)}", err=True)
    return exit_code(exc)


# Results ---------------------------------------------------------------------


def _local(value: str) -> str:
    """An ISO datetime from the API, in the reader's local time."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def cell(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        for key in ("name", "title", "full_name", "email", "id"):
            if key in value:
                return cell(value[key])
        return "…"
    if isinstance(value, list):
        return ", ".join(cell(v) for v in value) if value else "–"
    text = str(value)
    if isinstance(value, str) and len(text) >= 19 and text[4:5] == "-" and "T" in text:
        text = _local(text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def columns(items: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for item in items:
        for key in item:
            if key not in keys:
                keys.append(key)

    def shown(key: str) -> bool:
        sample = next((i[key] for i in items if i.get(key) is not None), None)
        if isinstance(sample, list):
            return key == "assignees"
        return True

    ordered = [k for k in PREFERRED if k in keys and shown(k)]
    ordered += [k for k in keys if k not in ordered and shown(k)]
    # id goes last but always fits: follow-up calls need it (FR-26).
    if "id" in ordered:
        ordered.remove("id")
        return [*ordered[: MAX_COLUMNS - 1], "id"]
    return ordered[:MAX_COLUMNS]


def table(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(none)"
    cols = columns(items)
    rows = [[cell(item.get(c)) for c in cols] for item in items]
    widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    lines = ["  ".join(c.upper().ljust(widths[i]) for i, c in enumerate(cols)).rstrip()]
    lines += ["  ".join(r[i].ljust(widths[i]) for i in range(len(cols))).rstrip() for r in rows]
    return "\n".join(lines)


def fields(obj: dict[str, Any]) -> str:
    scalars = {k: v for k, v in obj.items() if not isinstance(v, list)}
    width = max((len(k) for k in scalars), default=0)
    lines = [f"{k.ljust(width)}  {cell(v)}" for k, v in scalars.items()]
    for key, value in obj.items():
        if isinstance(value, list):
            lines.append("")
            lines.append(f"{key} ({len(value)})")
            if value and all(isinstance(v, dict) for v in value):
                lines.append(table(value))
            elif value:
                lines.append(cell(value))
    return "\n".join(lines)


def render(result: Any, *, offset: int | None = None) -> str:
    """A result for a person: a table for {items}, labelled fields otherwise."""
    if result is None:
        return "Done."
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        text = table(result["items"])
        if result.get("has_more"):
            nxt = (offset or 0) + len(result["items"])
            text += f"\nmore… (rerun with --offset {nxt})"
        return text
    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        text = table(result["messages"])
        if result.get("has_older"):
            text = "older messages… (rerun with --before <the first created_at>)\n" + text
        return text
    if isinstance(result, dict):
        return fields(result)
    if isinstance(result, list):
        return table(result) if all(isinstance(r, dict) for r in result) else cell(result)
    return cell(result)


def emit(result: Any, as_json: bool, *, offset: int | None = None) -> None:
    click.echo(dumps(result) if as_json else render(result, offset=offset))
