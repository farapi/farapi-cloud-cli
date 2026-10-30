"""Commands built from the tool catalog at run time (FR-11 to FR-13, FR-31, D-11).

A tool's dotted name is its command path, with underscores shown as hyphens:
``tasks.subtasks.add`` is ``farapi tasks subtasks add`` and ``inbox.read_all``
is ``farapi inbox read-all``. Each input property becomes one option, typed
from its JSON Schema, so a tool the backend offers on the CLI becomes a command
with no change here (D-12, FR-14).
"""

from __future__ import annotations

import json
import mimetypes
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import click

from farapi_cli import output

# Options every tool command has; a property with one of these names is
# offered as --input-<name> instead, so the two can never collide.
RESERVED = {"org", "json", "json_output", "api_url", "yes", "help"}
METAVARS = {"uuid": "UUID", "date": "YYYY-MM-DD", "date-time": "DATETIME", "email": "EMAIL"}
# A file on this machine: offered as a path, sent as what describes it, and
# its bytes go where the tool's result says (see _upload).
LOCAL_FILE = "local-file"


class Runner(Protocol):
    def call(self, name: str, data: dict[str, Any], org: str | None) -> Any: ...

    def organization_slugs(self) -> list[str]: ...

    def upload(self, target: dict[str, Any], path: Path) -> None: ...


class MissingOrganization(click.UsageError):
    """--org left out of an organization-scoped command (D-2, FR-31). It lists
    the user's organizations, so the caller can pick one and retry."""

    code = "organization_required"

    def __init__(self, slugs: list[str]) -> None:
        listed = ", ".join(slugs) if slugs else "none"
        super().__init__(f"--org is required. Your organizations: {listed}")
        self.params = {"organizations": slugs}


def command_path(name: str) -> list[str]:
    return [part.replace("_", "-") for part in name.split(".")]


def flag_for(prop: str) -> str:
    base = f"input_{prop}" if prop in RESERVED else prop
    return "--" + base.replace("_", "-")


def _types(spec: dict[str, Any]) -> tuple[set[str], bool]:
    raw = spec.get("type", "string")
    types = set(raw) if isinstance(raw, list) else {raw}
    return types - {"null"}, "null" in types


def _param_type(spec: dict[str, Any]) -> click.ParamType:
    if "enum" in spec:
        return click.Choice([str(v) for v in spec["enum"] if v is not None])
    types, _ = _types(spec)
    if "integer" in types:
        return click.IntRange(spec.get("minimum"), spec.get("maximum"))
    if "number" in types:
        return click.FLOAT
    return click.STRING


def _help(spec: dict[str, Any]) -> str:
    text = spec.get("description", "")
    if "default" in spec and spec["default"] is not None:
        default = spec["default"]
        if isinstance(default, bool):
            default = "yes" if default else "no"
        text = f"{text} (default: {default})".strip()
    _, nullable = _types(spec)
    if nullable:
        text = f"{text} Pass null to clear.".strip()
    return text


def build_option(prop: str, spec: dict[str, Any], required: bool) -> click.Option:
    types, nullable = _types(spec)
    flag = flag_for(prop)
    pyname = flag[2:].replace("-", "_")
    if spec.get("format") == LOCAL_FILE:
        return click.Option(
            [flag, pyname],
            required=required,
            type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
            metavar="PATH",
            help=_help(spec),
        )
    if "boolean" in types:
        off = "--no-" + flag[2:]
        return click.Option([f"{flag}/{off}", pyname], default=None, help=_help(spec))
    if "array" in types:
        items = spec.get("items", {})
        return click.Option(
            [flag, pyname],
            multiple=True,
            required=required,
            type=_param_type(items),
            metavar=METAVARS.get(items.get("format", "")),
            help=(_help(spec) + " Repeat for several.").strip(),
        )
    if "object" in types:
        return click.Option([flag, pyname], required=required, metavar="JSON", help=_help(spec))
    param_type = _param_type(spec)
    if nullable and isinstance(param_type, click.Choice):
        param_type = click.Choice([*param_type.choices, "null"])
    return click.Option(
        [flag, pyname],
        required=required,
        type=param_type,
        metavar=METAVARS.get(spec.get("format", "")),
        help=_help(spec),
    )


def _describe(path: Path) -> dict[str, Any]:
    content_type, _ = mimetypes.guess_type(path.name)
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "content_type": content_type or "application/octet-stream",
    }


def _value(spec: dict[str, Any], value: Any) -> Any:
    types, nullable = _types(spec)
    if isinstance(value, Path):
        return _describe(value)
    if isinstance(value, tuple):
        return list(value)
    if nullable and value == "null":
        return None
    if "object" in types and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise click.UsageError(f"{value!r} is not valid JSON.") from exc
    return value


def _confirm(command: str) -> None:
    """D-11: a destructive tool asks a person; nobody to ask means --yes."""
    if not sys.stdin.isatty():
        raise click.UsageError(
            f"`farapi {command}` can't be undone. Add --yes to run it without a prompt."
        )
    click.confirm(f"`farapi {command}` can't be undone. Continue?", abort=True)


def _upload(runner: Runner, result: Any, path: Path, org: str | None) -> Any:
    """A tool with a file input answers where to send the bytes ("upload":
    {url, fields}) and the tool that confirms they arrived ("complete":
    {tool, input}). The confirmation's result is the command's."""
    if not isinstance(result, dict) or "upload" not in result:
        return result
    runner.upload(result["upload"], path)
    complete = result.get("complete")
    if not complete:
        return result
    return runner.call(complete["tool"], complete.get("input", {}), org)


def build_command(tool: dict[str, Any], runner_for: Callable[[], Runner]) -> click.Command:
    path = command_path(tool["name"])
    schema = tool.get("input_schema") or {}
    properties: dict[str, dict[str, Any]] = schema.get("properties", {})
    required = set(schema.get("required", []))
    params: list[click.Parameter] = []
    names: dict[str, str] = {}
    for prop, spec in properties.items():
        option = build_option(prop, spec, prop in required)
        names[option.name or prop] = prop
        params.append(option)

    org_scoped = bool(tool.get("org_scoped"))
    destructive = bool(tool.get("destructive"))
    if org_scoped:
        params.append(
            click.Option(["--org"], help="The organization this call acts in: its slug or id.")
        )
    params.append(
        click.Option(["--json", "json_output"], is_flag=True, help="Print the result as JSON.")
    )
    params.append(click.Option(["--api-url"], help="The farapi API to talk to.", hidden=True))
    if destructive:
        params.append(click.Option(["--yes"], is_flag=True, help="Don't ask before running."))

    def callback(**kwargs: Any) -> None:
        org = kwargs.pop("org", None)
        as_json = output.json_mode(kwargs.pop("json_output", False))
        yes = kwargs.pop("yes", False)
        kwargs.pop("api_url", None)  # read from argv before the catalog loads
        data: dict[str, Any] = {}
        files: list[Path] = []
        for pyname, value in kwargs.items():
            if value is None or value == ():
                continue
            prop = names[pyname]
            if isinstance(value, Path):
                files.append(value)
            data[prop] = _value(properties[prop], value)
        runner = runner_for()
        if org_scoped and not org:
            raise MissingOrganization(runner.organization_slugs())
        if destructive and not yes:
            _confirm(" ".join(path))
        result = runner.call(tool["name"], data, org if org_scoped else None)
        if files:
            result = _upload(runner, result, files[0], org if org_scoped else None)
        output.emit(result, as_json, offset=data.get("offset"))

    example = tool.get("example", "")
    if example and not example.startswith("farapi "):
        example = f"farapi {example}"
    kind = "Changes nothing." if tool.get("read_only") else "Changes data."
    if destructive:
        kind = "Destroys data: asks first, or needs --yes."
    help_text = f"{tool.get('description', '')}\n\n{kind}"
    epilog = f"\b\nExample:\n  {example}" if example else None
    return click.Command(
        path[-1],
        params=params,
        callback=callback,
        help=help_text,
        short_help=tool.get("description", "").split("\n")[0][:80],
        epilog=epilog,
    )


def build_tree(
    catalog_tools: list[dict[str, Any]], runner_for: Callable[[], Runner]
) -> dict[str, click.Command]:
    """The top-level commands for a catalog: groups for every dotted prefix."""
    top: dict[str, click.Command] = {}
    for tool in sorted(catalog_tools, key=lambda t: t["name"]):
        path = command_path(tool["name"])
        command = build_command(tool, runner_for)
        if len(path) == 1:
            top[path[0]] = command
            continue
        group = top.get(path[0])
        if not isinstance(group, click.Group):
            group = click.Group(path[0], help=f"{path[0].capitalize()} commands.")
            top[path[0]] = group
        for part in path[1:-1]:
            child = group.commands.get(part)
            if not isinstance(child, click.Group):
                child = click.Group(part, help=f"{part.capitalize()} commands.")
                group.add_command(child)
            group = child
        group.add_command(command)
    return top
