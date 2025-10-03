# orch

CLI-first orchestration hub for Codex agents. **Now uses `codex app-server`** over STDIO,
so one long-lived server powers many conversations ("sub-agents"). Same REPL and batch driver,
no extra services required. Adds a scheduler/watchdog, GitHub poller, and optional OTEL heartbeats.

## Features

- Terminal-native REPL with colon-prefixed commands (`:help`, `:agents`, `:spawn`, `:tail`, etc.).
- Colourised, column-aligned event stream with grouped control payloads and optional state feed.
- Batch execution mode for scripted runs (`--script session.txt`).
- Optional auto-approval pass-through via `--dangerous` / `--no-dangerous`.
- Runtime autopilot toggle (`:autopilot on|off`) to decide whether orchestrator control blocks run automatically.
- GitHub helpers that treat Issues as charters (`:issue`, `:issue-prompt`, `:issue-list`, `:gh-issue`, `:gh-pr`).
- Scheduler/watchdog enforces WIP limits, check-ins, nudges, and time budgets without blocking the REPL.
- GitHub poller fills capacity from `orchestrate` issues, understands simple blockers, and posts one status comment.
- Optional OTEL heartbeats by tailing a local JSONL log for per-conversation liveness.
- Zero third-party Python dependencies (Python 3.10+).

## Requirements

- Python 3.10 or newer.
- Codex CLI with `codex app-server` (with `--stdio`) available on PATH (point `--codex-path` if needed).
- GitHub CLI (`gh`) authenticated for your repository when you use GitHub helpers.

## Quick Start (Interactive)

```bash
python3 codex_hub_cli.py \
  --codex-path /path/to/codex \
  --seed "Plan work for project X" \
  --wip 3 --checkin 10m --budget 45m \
  --otel-log /tmp/codex-otel.jsonl
```

Type free-form prompts for the orchestrator, or use colon commands to drive agents. For example:

```
:spawn coder Set up a pytest suite for the repo
:send coder Please generate a minimal test for foo.py
:agents
:wip
:plan
:stderr coder 50
:tail coder
:tail off
:statefeed off
:summary 123
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

The helpers expect `gh` to be authenticated for the repository; they respect `--cwd` (or default to the current directory).

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
  - `app_server_client.py` – lightweight JSON-RPC client for `codex app-server`.
  - `github_sync.py` – helpers for issues/PRs plus a status comment thread.
  - `otel_tailer.py` – optional JSONL tailer to ingest OTEL logs as heartbeats.

## License

MIT

---

## How the orchestrator and sub-agents behave (default prompts)

**Orchestrator (system prompt summary)**

- Treat GitHub Issues as charters: respect Goal, Acceptance, Scope, Validation.
- Use control blocks to `spawn`, `send`, and `close` sub-agents when autopilot is enabled.
- Keep messages concise; prefer small steps; ask for summaries and check-offs.
- Honour WIP limits; parallelise when blockers are cleared; sequence otherwise.

**Sub-agents (system prompt summary)**

- Work in the given workspace; create a branch/worktree as needed.
- Make minimal, testable changes; run tests; open a PR referencing the Issue.
- Report succinct progress; every check-in includes the next small step.
- On completion, map outcomes to the Issue acceptance checklist.

You can customise these in `codex_hub_core.py` (`ORCHESTRATOR_SYSTEM`, `SUBAGENT_SYSTEM_TEMPLATE`).

---

## Optional: OTEL collector (for heartbeats)

Enable OTEL in Codex and ship logs to a local JSONL file:

```toml
# ~/.config/codex/config.toml
[otel]
environment = "dev"
exporter    = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs" } }
log_user_prompt = true
```

Minimal collector example (sends logs to `/tmp/codex-otel.jsonl`):

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318

exporters:
  file:
    path: /tmp/codex-otel.jsonl

service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [file]
```

Run the hub with `--otel-log /tmp/codex-otel.jsonl` to enable heartbeats from OTEL.
