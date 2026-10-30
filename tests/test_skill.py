"""The Claude Code skill rendered from the catalog (FR-33)."""

from __future__ import annotations

from pathlib import Path

from fakes import TOOLS

from farapi_cli import skill
from farapi_cli.commands import command_path
from farapi_cli.config import DEFAULT_API_URL


def test_the_skill_names_every_tool_with_its_command_and_example():
    content = skill.render(TOOLS, DEFAULT_API_URL)
    assert content.startswith("---\nname: farapi\n")
    for tool in TOOLS:
        assert f"### `farapi {' '.join(command_path(tool['name']))}`" in content
        example = tool["example"]
        if not example.startswith("farapi "):
            example = f"farapi {example}"
        assert f"Example: `{example}`" in content
    assert "`--task` (uuid, required)" in content
    assert "`--status` (todo|in_progress|in_review|done, optional)" in content
    assert "`--assignees` (email, repeatable, optional)" in content
    assert "destructive: needs `--yes`" in content
    assert "--api-url" not in content


def test_a_skill_from_another_server_says_which(app, run):
    content = skill.render(TOOLS, "http://localhost:8030")
    assert "pass `--api-url http://localhost:8030` too" in content


def test_skill_install_writes_it_under_claude_skills(app, run):
    result = run(app, "skill", "--install")
    assert result.code == 0
    path = Path.home() / ".claude" / "skills" / "farapi" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == skill.render(TOOLS, app.api_url)
    assert str(path) in result.err
    assert run(app, "skill").out == skill.render(TOOLS, app.api_url)
