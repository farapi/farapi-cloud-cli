"""Commands built from the catalog (FR-11 to FR-14, FR-31, D-11)."""

from __future__ import annotations

import click
from fakes import CHATS_SEND, TASK_ID, TOOLS, seed_catalog

from farapi_cli import catalog
from farapi_cli.commands import build_command, build_tree, command_path, flag_for


def _tree():
    return build_tree(TOOLS, lambda: None)


def _option(command: click.Command, flag: str) -> click.Option:
    return next(p for p in command.params if isinstance(p, click.Option) and flag in p.opts)


def test_dotted_names_become_nested_commands_with_hyphens():
    tree = _tree()
    assert command_path("inbox.read_all") == ["inbox", "read-all"]
    assert set(tree) == {"organizations", "tasks", "inbox"}
    tasks = tree["tasks"]
    assert isinstance(tasks, click.Group)
    assert {"list", "create", "update", "delete", "subtasks"} <= set(tasks.commands)
    assert "add" in tasks.commands["subtasks"].commands
    assert "read-all" in tree["inbox"].commands


def test_options_are_typed_from_the_input_schema():
    listing = _tree()["tasks"].commands["list"]
    status = _option(listing, "--status")
    assert isinstance(status.type, click.Choice)
    assert list(status.type.choices) == ["todo", "in_progress", "in_review", "done"]
    limit = _option(listing, "--limit")
    assert isinstance(limit.type, click.IntRange)
    assert (limit.type.min, limit.type.max) == (1, 100)
    overdue = _option(listing, "--overdue")
    assert overdue.is_flag and overdue.secondary_opts == ["--no-overdue"]
    assert _option(listing, "--org")

    create = _tree()["tasks"].commands["create"]
    assert _option(create, "--assignees").multiple
    assert _option(create, "--title").required
    assert _option(_tree()["tasks"].commands["delete"], "--yes")
    # organizations list acts on no organization: no --org.
    assert not any("--org" in p.opts for p in _tree()["organizations"].commands["list"].params)


def test_a_property_named_like_a_built_in_option_gets_an_input_prefix():
    tool = {
        **TOOLS[1],
        "input_schema": {"type": "object", "properties": {"org": {"type": "string"}}},
    }
    command = build_command(tool, lambda: None)
    assert flag_for("org") == "--input-org"
    assert _option(command, "--input-org")
    assert _option(command, "--org").name == "org"


def test_help_shows_the_description_choices_required_flags_and_example(app, run):
    seed_catalog(app.cache, TOOLS)
    listing = run(app, "tasks", "list", "--help")
    assert listing.code == 0
    assert "Tasks in a project, newest first." in listing.out
    assert "Changes nothing." in listing.out
    assert "in_review" in listing.out
    assert "--org" in listing.out
    assert "Example:\n    farapi tasks list --org acme --project ops --assignee me" in listing.out

    add = run(app, "tasks", "subtasks", "add", "--help")
    assert "required" in add.out
    assert "Changes data." in add.out
    assert "can't be undone" not in add.out
    assert "Destroys data" in run(app, "tasks", "delete", "--help").out
    # An example without the program name gets it.
    assert "farapi tasks create --org acme" in run(app, "tasks", "create", "--help").out


def test_the_call_sends_typed_values_and_the_organization(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.list"] = (200, {"items": [], "has_more": False})
    result = run(
        app, "tasks", "list", "--org", "acme", "--status", "todo", "--limit", "5", "--no-overdue"
    )
    assert result.code == 0
    assert api.tool_calls("tasks.list") == [
        ("tasks.list", {"status": "todo", "limit": 5, "overdue": False}, "acme")
    ]

    run(
        app,
        "tasks",
        "create",
        "--org",
        "acme",
        "--project",
        "ops",
        "--title",
        "Ship",
        "--assignees",
        "a@acme.test",
        "--assignees",
        "b@acme.test",
    )
    assert api.tool_calls("tasks.create")[0][1] == {
        "project": "ops",
        "title": "Ship",
        "assignees": ["a@acme.test", "b@acme.test"],
    }

    # null clears a nullable field.
    run(app, "tasks", "update", "--org", "acme", "--task", TASK_ID, "--due-date", "null")
    assert api.tool_calls("tasks.update")[0][1] == {"task": TASK_ID, "due_date": None}

    # A tool outside any organization sends no X-Active-Org.
    run(app, "organizations", "list")
    assert api.tool_calls("organizations.list")[-1][2] is None


def test_a_missing_org_exits_2_and_lists_the_users_organizations(app, api, run):
    seed_catalog(app.cache, TOOLS)
    result = run(app, "tasks", "list", "--project", "ops")
    assert result.code == 2
    error = result.json["error"]
    assert error["code"] == "organization_required"
    assert error["params"]["organizations"] == ["acme", "beta"]
    assert api.tool_calls("tasks.list") == []


def test_a_destructive_tool_without_yes_exits_2_when_nobody_can_answer(app, api, run):
    seed_catalog(app.cache, TOOLS)
    refused = run(app, "tasks", "delete", "--org", "acme", "--task", TASK_ID)
    assert refused.code == 2
    assert "--yes" in refused.json["error"]["params"]["message"]
    assert api.tool_calls("tasks.delete") == []

    api.results["tasks.delete"] = (204, None)
    done = run(app, "tasks", "delete", "--org", "acme", "--task", TASK_ID, "--yes")
    assert done.code == 0
    assert api.tool_calls("tasks.delete") == [("tasks.delete", {"task": TASK_ID}, "acme")]


def test_an_unknown_command_refetches_the_catalog_once(app, api, run):
    seed_catalog(app.cache, TOOLS, version="v0")
    api.tools = [*TOOLS, CHATS_SEND]
    result = run(app, "chats", "send", "--org", "acme", "--chat", TASK_ID, "--body", "Deployed.")
    assert result.code == 0
    assert api.tool_calls("chats.send")[0][1] == {"chat": TASK_ID, "body": "Deployed."}
    assert catalog.cached(app.cache).version == "v1"


def test_a_new_catalog_version_is_cached_for_the_next_run(app, api, run):
    seed_catalog(app.cache, TOOLS, version="v0")
    api.results["tasks.list"] = (200, {"items": [], "has_more": False})
    run(app, "tasks", "list", "--org", "acme")
    assert catalog.cached(app.cache).version == "v1"


def test_root_help_lists_the_catalog_when_signed_in(app, run):
    seed_catalog(app.cache, TOOLS)
    result = run(app, "--help")
    assert result.code == 0
    for name in ("login", "logout", "whoami", "tools", "skill", "tasks", "inbox"):
        assert name in result.out
    assert "farapi tools" in result.out


def test_root_help_works_signed_out_with_no_catalog(signed_out, api, run):
    result = run(signed_out, "--help")
    assert result.code == 0
    assert "login" in result.out
    assert "Run `farapi login` to sign in." in result.out
    assert api.requests == []
    assert run(signed_out, "tools", "--help").code == 0


def test_a_catalog_command_while_signed_out_exits_3(signed_out, run):
    result = run(signed_out, "tasks", "list", "--org", "acme")
    assert result.code == 3
    assert result.json == {"error": {"code": "not_signed_in"}}


def test_tools_lists_every_command(app, run):
    piped = run(app, "tools")
    assert [t["name"] for t in piped.json["tools"]] == [t["name"] for t in TOOLS]
    terminal = run(app, "tools", tty=True)
    assert "farapi tasks subtasks add" in terminal.out
    assert "farapi inbox read-all" in terminal.out
