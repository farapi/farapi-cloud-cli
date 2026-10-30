"""A Claude Code skill rendered from the live catalog (FR-33).

It's generated from the same catalog the commands are built from, so it
can't drift from what the CLI offers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from farapi_cli.commands import command_path, flag_for
from farapi_cli.config import DEFAULT_API_URL

FRONTMATTER = """---
name: farapi
description: Read and change farapi-ops tasks, chats, projects and the inbox from the terminal with the farapi CLI. Use when asked to look at or update farapi tasks, post in a farapi chat, or work through the farapi inbox.
---
"""

RULES = """# farapi CLI

`farapi` acts as the signed-in user, with exactly their access.

- **Sign in once.** The user runs `farapi login` themselves (it opens their
  browser). If a command exits with code 3, ask them to run it again.
- **Always pass `--org <slug>`** on organization commands. Nothing is
  remembered between calls. `farapi organizations list --json` lists the slugs.
- **Always pass `--json`** and parse stdout: it is exactly one JSON document,
  the result or `{"error": {"code", "params", "fields"}}`.
- **Exit codes:** 0 done, 1 the server refused (read `error.code`), 2 bad usage,
  3 not signed in, 4 server unreachable.
- **Destructive commands** need `--yes`. Confirm with the user before you add it.
- People are named by email and projects by slug. Every result carries the
  ids a follow-up call needs.
- Lists return `{"items": [...], "has_more": bool}`; page with `--offset`.
"""


def _option_line(prop: str, spec: dict[str, Any], required: bool) -> str:
    raw = spec.get("type", "string")
    kind = "/".join(t for t in (raw if isinstance(raw, list) else [raw]) if t != "null")
    if "enum" in spec:
        kind = "|".join(str(v) for v in spec["enum"] if v is not None)
    elif spec.get("format"):
        kind = spec["format"]
    if kind == "array":
        items = spec.get("items", {})
        kind = f"{items.get('format') or items.get('type', 'string')}, repeatable"
    flag = flag_for(prop)
    note = "required" if required else "optional"
    desc = spec.get("description", "")
    return f"  - `{flag}` ({kind}, {note}){': ' + desc if desc else ''}"


def render(tools: list[dict[str, Any]], api_url: str = DEFAULT_API_URL) -> str:
    lines = [FRONTMATTER, RULES]
    if api_url != DEFAULT_API_URL:
        lines.append(f"This catalog came from `{api_url}`: pass `--api-url {api_url}` too.\n")
    lines.append("## Commands\n")
    for tool in sorted(tools, key=lambda t: t["name"]):
        command = "farapi " + " ".join(command_path(tool["name"]))
        lines.append(f"### `{command}`\n")
        lines.append(tool.get("description", "").strip())
        traits = []
        if tool.get("org_scoped"):
            traits.append("needs `--org`")
        traits.append("read-only" if tool.get("read_only") else "changes data")
        if tool.get("destructive"):
            traits.append("destructive: needs `--yes`")
        lines.append(f"\n_{', '.join(traits)}_\n")
        schema = tool.get("input_schema") or {}
        required = set(schema.get("required", []))
        props = schema.get("properties", {})
        if props:
            lines.append("Options:")
            lines += [_option_line(p, s, p in required) for p, s in props.items()]
            lines.append("")
        example = tool.get("example", "")
        if example:
            if not example.startswith("farapi "):
                example = f"farapi {example}"
            lines.append(f"Example: `{example}`\n")
    return "\n".join(lines).rstrip() + "\n"


def install_path() -> Path:
    return Path.home() / ".claude" / "skills" / "farapi" / "SKILL.md"


def install(content: str, path: Path | None = None) -> Path:
    target = path or install_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
