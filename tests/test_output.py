"""What a command prints, and its exit code (FR-27 to FR-30)."""

from __future__ import annotations

import json

import httpx
from fakes import ME, TASK_ID, TOKENS, TOOLS, seed_catalog

from farapi_cli.config import Credentials

TASKS = {
    "items": [
        {"id": "t1", "title": "Ship the CLI", "status": "in_review", "assignees": []},
        {"id": "t2", "title": "Write the skill", "status": "todo", "assignees": []},
    ],
    "has_more": True,
}


def test_piped_output_is_exactly_one_json_document(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.list"] = (200, TASKS)
    result = run(app, "tasks", "list", "--org", "acme")
    assert result.code == 0
    assert json.loads(result.out) == TASKS
    assert result.err == ""


def test_json_flag_wins_in_a_terminal(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.list"] = (200, TASKS)
    assert run(app, "tasks", "list", "--org", "acme", "--json", tty=True).json == TASKS


def test_arabic_is_printed_as_characters(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.create"] = (201, {"id": "t3", "title": "مراجعة العقد"})
    result = run(
        app, "tasks", "create", "--org", "acme", "--project", "ops", "--title", "مراجعة العقد"
    )
    assert "مراجعة العقد" in result.out
    assert "\\u" not in result.out


def test_an_api_error_exits_1_with_its_envelope(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.update"] = (
        400,
        {"code": "validation_error", "params": {}, "fields": {"status": ["invalid_choice"]}},
    )
    result = run(app, "tasks", "update", "--org", "acme", "--task", TASK_ID, "--status", "done")
    assert result.code == 1
    assert result.json == {
        "error": {"code": "validation_error", "fields": {"status": ["invalid_choice"]}}
    }


def test_a_usage_error_exits_2(app, run):
    seed_catalog(app.cache, TOOLS)
    missing = run(app, "tasks", "subtasks", "add", "--org", "acme", "--title", "x")
    assert missing.code == 2
    assert missing.json["error"]["code"] == "usage_error"
    assert "--task" in missing.json["error"]["params"]["message"]
    assert run(app, "tasks", "list", "--org", "acme", "--limit", "0").code == 2
    assert run(app, "tasks", "list", "--org", "acme", "--status", "lost").code == 2


def test_a_refused_refresh_exits_3_and_forgets_the_sign_in(signed_out, api, keychain, run):
    signed_out.store.save(Credentials("old", "r0", 0))
    seed_catalog(signed_out.cache, TOOLS)
    api.token = [(400, {"error": "invalid_grant"})]
    result = run(signed_out, "tasks", "list", "--org", "acme")
    assert result.code == 3
    assert result.json == {"error": {"code": "not_signed_in"}}
    assert keychain.items == {}


def test_an_early_401_refreshes_once_and_retries(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.token = [(200, TOKENS)]
    api.results["tasks.list"] = [(401, {"code": "session_invalid"}), (200, TASKS)]
    result = run(app, "tasks", "list", "--org", "acme")
    assert result.code == 0
    calls = [r for r in api.requests if r.url.path == "/api/v1/tools/tasks.list/"]
    assert [r.headers["Authorization"] for r in calls] == ["Bearer access", "Bearer a1"]
    # The token request itself carries no Bearer header.
    token = next(r for r in api.requests if r.url.path == "/api/v1/oauth/token/")
    assert "Authorization" not in token.headers


def test_an_unreachable_server_exits_4(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.list"] = httpx.ConnectError("refused")
    result = run(app, "tasks", "list", "--org", "acme")
    assert result.code == 4
    assert result.json == {"error": {"code": "unreachable"}}


def test_a_terminal_shows_a_list_as_a_table_with_a_more_line(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.list"] = (200, TASKS)
    result = run(app, "tasks", "list", "--org", "acme", "--offset", "2", tty=True)
    lines = result.out.splitlines()
    assert lines[0].split() == ["TITLE", "STATUS", "ASSIGNEES", "ID"]
    assert "Ship the CLI" in lines[1] and lines[1].endswith("t1")
    assert lines[-1] == "more… (rerun with --offset 4)"


def test_a_terminal_shows_an_error_as_an_english_sentence(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.update"] = (404, {"code": "task_not_found"})
    result = run(app, "tasks", "update", "--org", "acme", "--task", TASK_ID, tty=True)
    assert result.code == 1
    assert result.out == ""
    assert result.err.strip() == "Error: No such task in this organization, or you can't reach it."


def test_a_terminal_names_the_fields_an_error_is_about(app, api, run):
    seed_catalog(app.cache, TOOLS)
    api.results["tasks.create"] = (
        400,
        {
            "code": "assignee_not_found",
            "params": {"email": "x@acme.test"},
            "fields": {"assignees": ["assignee_not_found"]},
        },
    )
    result = run(
        app, "tasks", "create", "--org", "acme", "--project", "ops", "--title", "x", tty=True
    )
    assert "x@acme.test is not a member of this organization." in result.err
    assert "--assignees: assignee_not_found" in result.err


def test_whoami_and_logout(app, api, keychain, run):
    info = run(app, "whoami").json
    assert info["user"]["email"] == ME["email"]
    assert [o["slug"] for o in info["organizations"]] == ["acme", "beta"]
    terminal = run(app, "whoami", tty=True).out
    assert "Signed in as Sara Adel <sara@acme.test>" in terminal
    assert "Acme (--org acme)" in terminal

    assert run(app, "logout").json == {"signed_out": True}
    logout = next(r for r in api.requests if r.url.path == "/api/v1/auth/logout/")
    assert json.loads(logout.content) == {"refresh": "refresh"}
    assert keychain.items == {}
    assert run(app, "logout").json == {"signed_out": False}
