# orch

CLI-first orchestration hub for Codex agents. It reuses `codex proto` subprocesses for the orchestrator and any spawned sub-agents, providing an interactive REPL and a batch driver—no web UI or extra services required.

## Features

- Terminal-native REPL with colon-prefixed commands (`:help`, `:agents`, `:spawn`, `:tail`, etc.).
- Colourised, column-aligned event stream with grouped control payloads and optional state feed.
- Batch execution mode for scripted runs (`--script session.txt`).
- Optional auto-approval pass-through via `--dangerous` / `--no-dangerous`.
- Runtime autopilot toggle (`:autopilot on|off`) to decide whether orchestrator control blocks run automatically.
- GitHub helpers that treat Issues as charters (`:issue`, `:issue-prompt`, `:issue-list`, `:gh-issue`, `:gh-pr`).
- Zero third-party Python dependencies (Python 3.10+).

## Requirements

- Python 3.10 or newer.
- A local Codex checkout (point `--codex-path` at its root).

## Quick Start (Interactive)

```bash
python3 codex_hub_cli.py \
  --codex-path /path/to/codex \
  --seed "Plan work for project X"
```

Type free-form prompts for the orchestrator, or use colon commands to drive agents. For example:

```
:spawn coder Set up a pytest suite for the repo
:send coder Please generate a minimal test for foo.py
:agents
:stderr coder 50
:tail coder
:tail off
:statefeed off
:close coder
:quit
```

Use `:statefeed on|off` to control whether state change notifications appear in the event stream.

## GitHub Issue Helpers

The CLI can call the `gh` CLI to keep Issues and PRs in sync with hub activity.

- `:issue <number>` prints the Goal, Acceptance checklist, Scope notes, and Validation sections parsed from the Issue body.
- `:issue-prompt <number>` prints the same summary **and** sends it to the orchestrator as a fresh brief.
- `:issue-list` lists open issues labelled `orchestrate` so you can pick the next task.
- `:gh-issue <number> <comment...>` / `:gh-pr <number> <comment...>` add quick status updates without leaving the REPL.

The helpers expect `gh` to be authenticated for the repository; they respect `cwd` passed via `--cwd` (or fall back to the current directory).

## Batch Mode From a Script

Prepare a command/script file:

```bash
cat > session.txt <<'EOF_SCRIPT'
hello orchestrator
:spawn analyst Audit dependencies for security issues
:send analyst Start with top 10 packages
:agents
:quit
EOF_SCRIPT
```

Run the script non-interactively:

```bash
python3 codex_hub_cli.py --codex-path /path/to/codex --script session.txt
```

Each line is fed to the orchestrator exactly as if typed in the REPL, letting you automate routine playbooks.

## Repository Layout

- `codex_hub_core.py` – standalone orchestration core (no web dependencies).
- `codex_hub_cli.py` – interactive CLI frontend.

## License

MIT
