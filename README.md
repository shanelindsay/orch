# orch

CLI-first orchestration hub for Codex agents. It can run interactively (REPL) **or** in
**GitHub-driven mode** where GitHub Issues/PRs + labels are the only control surface.
Under the hood we drive a single `codex app-server` process and open one conversation
for the **orchestrator** and one per **sub-agent** (per Issue).

## Features

- Terminal-native REPL with colon-prefixed commands (`:help`, `:agents`, `:spawn`, `:tail`, etc.).
- Colourised, column-aligned event stream with grouped control payloads and optional state feed.
- Batch execution mode for scripted runs (`--script session.txt`).
- Orchestrator decision loop with debounced digests and a simple watchdog.
- New controls: `status` (post a human-readable update) and `fetch` (pull artifacts/diffs/logs on demand).
- Tiny append-only artifact store so sub-agent updates can be fetched without flooding context.
- Optional auto-approval pass-through via `--dangerous` / `--no-dangerous`.
- Runtime autopilot toggle (`:autopilot on|off`) to decide whether orchestrator control blocks run automatically.
- GitHub helpers that treat Issues as charters (`:issue`, `:issue-prompt`, `:issue-list`, `:gh-issue`, `:gh-pr`).
- Scheduler/watchdog enforces WIP limits, check-ins, nudges, and time budgets without blocking the REPL.
- GitHub poller fills capacity from `orchestrate` issues, understands simple blockers, and posts one status comment.
- Optional OTEL heartbeats by tailing a local JSONL log for per-conversation liveness.
- Core has zero third-party Python dependencies (Python 3.10+). The optional web dashboard under `hub_dashboard/` uses `aiohttp`.

## Requirements

- Python 3.10 or newer.
- Codex CLI with `codex app-server` available on PATH (or point `--codex-path`).
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

## GitHub-only control flow (local; ideal for tmux on HPC)

This mode polls GitHub locally and uses labels/comments as the interface. Agents run
in local git **worktrees** and report back to Issues/PRs.

### Labels (default set, configurable)

* `orchestrate` — pick up this Issue
* `agent:queued` — daemon should start/assign an agent
* `agent:running` — agent working (set by daemon)
* `agent:review` — PR opened and awaiting review
* `agent:done` — work finished (Issue can be closed)
* `agent:stalled` — no progress heartbeat within threshold
* `auto:pr-on-complete` — open a PR automatically when task completes

### Start the daemon

```bash
# inside the repo you want to orchestrate
python3 orchestrate_github.py \
  --codex-path codex \
  --poll-secs 25 \
  --dangerous \
  --autopilot-on
```

It will:
1) find Issues labelled `orchestrate`+`agent:queued`,
2) create a `git worktree` and branch `ai/iss-<N>-<slug>`,
3) spin up a sub-agent bound to that worktree,
4) post a comment + flip labels to `agent:running`,
5) mirror agent end-of-step reports back to the Issue, and
6) on completion optionally open a PR and relabel to `agent:review`.

> Project fields: if you want Projects to mirror labels, add a small workflow that maps
> label → field. Create `.github/workflows/labels-to-project.yml` (or similar) and paste the
> starter snippet below, editing the project/field IDs for your org.

```yaml
name: Sync labels → Project status
on:
  issues:
    types: [opened, labeled, unlabeled]
jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      issues: write
      contents: read
      projects: write
    steps:
      - uses: actions/github-script@v7
        with:
          script: |
            // TODO: set your project/field IDs; this is a stub showing where to map
            core.info('Map labels to a Project status here (implementation left to repo owner).')
```

### Orchestrator & sub-agent prompts

*Orchestrator.* Default system prompt: plan, spawn named sub-agents, coordinate by emitting
`control` blocks (`spawn`, `send`, `close`; optional `exec`). It writes short human updates
that the daemon mirrors into GitHub comments.

*Sub-agents.* Default system prompt: "You are a sub-agent named X; work in this worktree and
give succinct progress updates; end with a short summary and next actions." The daemon adds
the Issue charter text to the initial message so each agent can work from the Issue's Goal,
Acceptance, Scope and Validation.

### Safety/approvals

Safety model:

- `--dangerous` controls the sandbox power for conversations (danger-full-access vs. workspace-write).
- Approvals are always requested from the app-server; the hub auto-approves only when both `--dangerous` and autopilot are enabled.
- Use `:autopilot on|off` at runtime to toggle automatic execution of CONTROL blocks and approvals.

## Orchestrator controls the hub (new)

The orchestrator replies to DRDs with normal prose plus fenced blocks labelled ```control``` containing JSON:

```
{"spawn":{"name":"iss-128","task":"Investigate failing tests","cwd":null}}
{"send":{"to":"iss-128","task":"Open a Draft PR referencing #128 with repro"}}
{"close":{"agent":"iss-128"}}
{"status":{"issue":128,"text":"Draft PR requested; implementing lock next."}}
{"fetch":{"artifact":"a1b2c3","max_chars":4000}}
```

The hub executes these immediately when autopilot is enabled. Any `fetch` payload is attached to the next digest,
letting the orchestrator pull detail on demand without flooding its context window.

## Operator commands (new)

- `:decide`  flush the digest debounce and send a Decision-Ready Digest now
- `:wip`     show active agents with last check-ins and budgets
- `:recent`  show the decision log (last 20 decisions)

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

Artifacts: the hub stores text artifacts under `.orch/artifacts/` and writes a rolling event log to `.orch/state.jsonl`. The `.orch/` directory is ignored by git.
