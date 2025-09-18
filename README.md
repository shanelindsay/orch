# orch

CLI-only orchestration hub for Codex agents. It reuses `codex proto` subprocesses for the orchestrator and sub-agents while providing an interactive REPL and batch mode—no web server required.

## Features

- Terminal REPL with colon-prefixed commands (`:help`, `:agents`, `:spawn`, `:tail`, etc.).
- Colourised event stream showing orchestrator ↔ agent traffic.
- Script mode for non-interactive runs (`--script session.txt`).
- Optional auto-approval pass-through via `--dangerous/--no-dangerous`.
- Zero third-party dependencies (Python 3.10+).

## Quick Start

```bash
python3 codex_hub_cli.py \
  --codex-path /path/to/codex \
  --seed "Plan work for project X"
```

Then type free-form messages for the orchestrator or commands such as `:spawn coder Build the thing`.

Batch execution:

```bash
python3 codex_hub_cli.py --codex-path /path/to/codex --script session.txt
```

## Repository Layout

- `codex_hub_core.py` – standalone orchestration core (no web dependencies).
- `codex_hub_cli.py` – interactive CLI frontend.

## License

MIT
