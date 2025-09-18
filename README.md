# orch

CLI-first orchestration hub for Codex agents. It reuses `codex proto` subprocesses for the orchestrator and any spawned sub-agents, providing an interactive REPL and a batch driver—no web UI or extra services required.

## Features

- Terminal-native REPL with colon-prefixed commands (`:help`, `:agents`, `:spawn`, `:tail`, etc.).
- Colourised, column-aligned event stream with grouped control payloads and optional state feed.
- Batch execution mode for scripted runs (`--script session.txt`).
- Optional auto-approval pass-through via `--dangerous` / `--no-dangerous`.
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
