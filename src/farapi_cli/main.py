"""The `farapi` entry point: the built-ins plus the catalog's commands (FR-2,
FR-5, FR-6, FR-10, FR-30, FR-33).

``main`` returns the exit code instead of letting click exit, so every failure
ends in one of FR-30's codes: 0 done, 1 refused, 2 bad usage, 3 not signed
in, 4 unreachable.
"""

from __future__ import annotations

import contextlib
import sys
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import httpx

from farapi_cli import __version__, auth, catalog as catalogs, output, skill
from farapi_cli.client import Client, make_http, upload_file
from farapi_cli.commands import Runner, build_tree, command_path
from farapi_cli.config import CredentialStore, api_url as resolve_api_url, cache_dir
from farapi_cli.errors import ApiError, NotSignedIn, SignInFailed, Unreachable

BUILTINS = ("login", "logout", "whoami", "tools", "skill")


@dataclass
class App:
    """Everything a run needs, built once per process (tests build their own)."""

    api_url: str
    store: CredentialStore
    http: httpx.Client
    cache: Path
    _tree: dict[str, click.Command] | None = field(default=None, repr=False)
    _fetched: bool = field(default=False, repr=False)

    @classmethod
    def create(cls, url: str, transport: httpx.BaseTransport | None = None) -> App:
        return cls(url, CredentialStore(url), make_http(transport), cache_dir(url))

    def client(self, org: str | None = None) -> Client:
        return Client(self.api_url, self.store, self.http, org=org)

    def signed_in(self) -> bool:
        return self.store.load() is not None

    # Runner, for the catalog's commands ------------------------------------

    def call(self, name: str, data: dict[str, Any], org: str | None) -> Any:
        client = self.client(org)
        result = client.call_tool(name, data)
        catalogs.note_version(client, self.cache)
        return result

    def organization_slugs(self) -> list[str]:
        result = self.client().call_tool("organizations.list", {}) or {}
        return [o["slug"] for o in result.get("items", [])]

    def upload(self, target: dict[str, Any], path: Path) -> None:
        upload_file(self.http, target, path)

    # The catalog's command tree --------------------------------------------

    def tree(self, *, fetch: bool = False) -> dict[str, click.Command]:
        """Commands from the cached catalog; fetch=True goes to the server
        (once per run) when signed in."""
        if fetch and not self._fetched and self.signed_in():
            self._fetched = True
            current = catalogs.fetch(self.client(), self.cache)
            self._tree = build_tree(current.tools, self._runner)
        if self._tree is None:
            current = catalogs.cached(self.cache)
            if current is None and self.signed_in() and not self._fetched:
                self._fetched = True
                try:
                    current = catalogs.fetch(self.client(), self.cache)
                except (Unreachable, NotSignedIn, ApiError):
                    current = None
            self._tree = build_tree(current.tools if current else [], self._runner)
        return self._tree

    def _runner(self) -> Runner:
        return self


class FarapiGroup(click.Group):
    """The root: the built-ins first, then the catalog's groups."""

    def __init__(self, app: App, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.app = app

    def list_commands(self, ctx: click.Context) -> list[str]:
        return [*BUILTINS, *sorted(n for n in self.app.tree() if n not in BUILTINS)]

    def get_command(self, ctx: click.Context, name: str) -> click.Command | None:
        if name in self.commands:
            return self.commands[name]
        command = self.app.tree().get(name)
        if command is None:
            if not self.app.signed_in():
                raise NotSignedIn()
            # FR-14: maybe the server offers it and the cache is old.
            command = self.app.tree(fetch=True).get(name)
        return command

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if self.app.tree():
            text = "Run `farapi <command> --help` for its options. `farapi tools` lists every tool."
        else:
            text = "Run `farapi login` to sign in. Your commands appear here after that."
        with formatter.section("Next"):
            formatter.write_text(text)


def _signed_in_as(app: App) -> dict[str, Any]:
    client = app.client()
    me = client.request("GET", "/api/v1/auth/me/") or {}
    orgs = client.call_tool("organizations.list", {}) or {}
    return {
        "api_url": app.api_url,
        "user": {k: me.get(k) for k in ("id", "email", "full_name")},
        "organizations": orgs.get("items", []),
    }


def _whoami_text(info: dict[str, Any]) -> str:
    user = info["user"]
    lines = [
        f"Signed in as {user.get('full_name') or ''} <{user.get('email')}> on {info['api_url']}"
    ]
    orgs = info["organizations"]
    lines.append("")
    lines.append("Organizations:" if orgs else "You don't belong to any organization yet.")
    for org in orgs:
        lines.append(f"  {org.get('name')} (--org {org.get('slug')})")
        codes = org.get("permissions") or []
        lines.append("    " + (", ".join(codes) if codes else "no permission codes"))
    return "\n".join(lines)


def build_cli(app: App) -> FarapiGroup:
    root = FarapiGroup(
        app,
        name="farapi",
        help="farapi-ops from the terminal: tasks, chats, projects and the inbox.",
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    root.params.append(
        click.Option(["--api-url"], help="The farapi API to talk to (or FARAPI_API_URL).")
    )
    root.params.append(
        click.Option(
            ["--version"],
            is_flag=True,
            expose_value=False,
            is_eager=True,
            callback=_print_version,
            help="Show the version.",
        )
    )

    @root.command()
    @click.option("--device", is_flag=True, help="Sign in with a code on another device.")
    @click.option("--json", "json_output", is_flag=True, help="Print the result as JSON.")
    def login(device: bool, json_output: bool) -> None:
        """Sign in through the browser (or a device code). Never asks for a password."""
        if device or not _has_browser():
            auth.login_device(app.http, app.api_url, app.store, echo=_say)
        else:
            auth.login_browser(
                app.http, app.api_url, app.store, open_browser=webbrowser.open, echo=_say
            )
        with contextlib.suppress(Unreachable, ApiError):  # the commands can wait a run
            app.tree(fetch=True)
        info = _signed_in_as(app)
        as_json = output.json_mode(json_output)
        click.echo(output.dumps(info) if as_json else _whoami_text(info))

    @root.command()
    @click.option("--json", "json_output", is_flag=True, help="Print the result as JSON.")
    def logout(json_output: bool) -> None:
        """Sign out: ends the session on the server and forgets it here."""
        ended = auth.logout(app.http, app.api_url, app.store)
        if output.json_mode(json_output):
            click.echo(output.dumps({"signed_out": ended}))
        else:
            click.echo("Signed out." if ended else "You weren't signed in.")

    @root.command()
    @click.option("--json", "json_output", is_flag=True, help="Print the result as JSON.")
    def whoami(json_output: bool) -> None:
        """Who is signed in, and your organizations with your permission codes."""
        info = _signed_in_as(app)
        click.echo(output.dumps(info) if output.json_mode(json_output) else _whoami_text(info))

    @root.command()
    @click.option("--json", "json_output", is_flag=True, help="Print the full catalog as JSON.")
    def tools(json_output: bool) -> None:
        """Every tool the CLI offers you, from the server's catalog."""
        current = catalogs.load(app.client(), app.cache, refresh=True)
        if output.json_mode(json_output):
            click.echo(output.dumps({"version": current.version, "tools": current.tools}))
            return
        rows = [
            ("farapi " + " ".join(command_path(t["name"])), t.get("description", ""))
            for t in current.tools
        ]
        width = max((len(r[0]) for r in rows), default=0)
        for name, description in rows:
            click.echo(f"{name.ljust(width)}  {description.splitlines()[0] if description else ''}")

    @root.command(name="skill")
    @click.option("--install", is_flag=True, help="Write it to ~/.claude/skills/farapi/SKILL.md.")
    def skill_command(install: bool) -> None:
        """A Claude Code skill for this CLI, generated from the live catalog."""
        current = catalogs.load(app.client(), app.cache, refresh=True)
        content = skill.render(current.tools, app.api_url)
        if install:
            path = skill.install(content)
            click.echo(f"Wrote {path}", err=True)
        else:
            click.echo(content, nl=False)

    return root


def _say(text: str) -> None:
    """Sign-in instructions go to stderr, keeping stdout for the result."""
    click.echo(text, err=True)


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if value:
        click.echo(__version__)
        ctx.exit()


def _has_browser() -> bool:
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def _scan_api_url(args: list[str]) -> str | None:
    """--api-url anywhere on the line, read before click parses it: the
    catalog for that server is needed to know what the commands are."""
    for index, arg in enumerate(args):
        if arg == "--api-url" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--api-url="):
            return arg.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None, app: App | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    app = app or App.create(resolve_api_url(_scan_api_url(args)))
    as_json = "--json" in args or not sys.stdout.isatty()
    cli = build_cli(app)
    try:
        result = cli.main(args=args, prog_name="farapi", standalone_mode=False)
    except click.exceptions.NoArgsIsHelpError as exc:
        click.echo(exc.ctx.get_help() if exc.ctx else cli.get_help(click.Context(cli)))
        return 0
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 1
    except (click.UsageError, ApiError, NotSignedIn, Unreachable, SignInFailed) as exc:
        return output.report_error(exc, as_json)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
