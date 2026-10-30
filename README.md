# farapi CLI

`farapi` is farapi-ops from the terminal: tasks, chats, projects and the inbox,
for people and for Claude. It acts as you, with exactly the access you have in
the web app.

Its commands come from the tool catalog the API serves. When the backend offers
another tool on the CLI, it shows up on the next run, without reinstalling.

## Install

Requirements: [uv](https://docs.astral.sh/uv/) and macOS or Linux.

```bash
uv tool install 'git+https://github.com/farapi/farapi-cloud-cli.git'
uv tool upgrade farapi-cli                                    # later, for a newer version
```

## Sign in

```bash
farapi login            # opens your browser; approve the sign-in at ops.farapi.ai
farapi login --device   # no browser here: prints a link and a code for any signed-in device
farapi whoami           # who you are, your organizations and your permission codes there
farapi logout
```

The CLI never asks for your password. The sign-in lives in your system keychain
(or, where there is none, a file only you can read in `~/.config/farapi/`), and
you can revoke it any time under Settings → Connected apps.

## Use

Every organization command needs `--org` (the organization's slug or id).
Nothing is remembered between calls.

```bash
farapi tools                                          # every command, one line each
farapi tasks list --org acme --project ops --assignee me
farapi tasks update --org acme --task <id> --status in_review
farapi tasks create --org acme --project ops --title "Ship the CLI" --assignees sara@acme.test
farapi chats send --org acme --chat <id> --body "Deployed."
farapi inbox list --org acme
farapi tasks delete --org acme --task <id> --yes      # destructive: asks first, or --yes
```

Piped, or with `--json`, a command prints exactly one JSON document: the result,
or `{"error": {"code", "params", "fields"}}`. In a terminal, it prints tables.

| Exit code | Meaning |
|---|---|
| 0 | Done |
| 1 | The server refused (see `error.code`) |
| 2 | Bad usage: a missing flag or a bad value |
| 3 | Not signed in: run `farapi login` |
| 4 | Can't reach the server |

## Claude

```bash
farapi skill --install   # writes ~/.claude/skills/farapi/SKILL.md from the live catalog
```

## Other servers

`--api-url` (or `FARAPI_API_URL`) points the CLI at another API, for example the
local stack at `http://localhost:8030`. Each server keeps its own sign-in.

## Develop

```bash
uv sync
uv run farapi --help
uv run ruff check .
uv run pytest
```

## License

MIT
