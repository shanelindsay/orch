# Hub Dashboard (Proof‑of‑Concept)

This folder contains a tiny, single‑file web dashboard (`codex_hub_web.py`) that runs a
non‑blocking hub in front of a Codex CLI "proto" process and lets you:

- send a message to the orchestrator from your browser
- see a live event stream (Server‑Sent Events) from the orchestrator/sub‑agents
- keep the UI responsive while the agent is thinking/working

It is intentionally small and pragmatic — a proof‑of‑concept you can run locally to
watch and steer an agent without a full TUI.

> Status: early PoC. It works for basic messaging and logging, but leaves lots of
> functionality for later (see “Roadmap”).

---

## Quick start

Prerequisites:
- Python 3.10+
- `pip install aiohttp`
- Codex CLI (`codex`) available on your PATH (or point to it with `--codex-path`)

Run:

```bash
python hub_dashboard/codex_hub_web.py --port 8777 --seed "Hello"
# then open http://127.0.0.1:8777/
```

Useful flags:
- `--port`       dashboard port (default 8777)
- `--codex-path` path to the `codex` binary (default `codex`)
- `--cwd`        working directory for the agent (default current repo)
- `--seed`       initial text message shown to the agent

If the page looks stale, use a hard refresh (Ctrl/Cmd+Shift+R) to avoid cached JS.

---

## What it does now

- Starts a Codex process in `proto` mode and bridges stdin/stdout to the browser.
- Adds a simple form so you can type a message and hit Enter/Send.
- Streams events over SSE and renders a minimal log (your message, agent replies,
  task starts, errors). It deliberately filters some noisy event types.
- Keeps the UI responsive; you can send another message while the model thinks.

What it does NOT do yet (by design in this PoC):
- No authentication, sessions, or persistence
- No agent sidebar, tailing stderr, or per‑agent controls
- No autopilot toggle in the UI (the PoC favors “manual” operation)

---

## Troubleshooting

- “Nothing happens when I press Enter” → hard refresh the page. Some browsers or
  content scripts cache older JS; the form submit wiring in this PoC is very small
  and should work once the latest bundle loads.
- “Initial red stderr line” → Codex can print a one‑off warning on first boot. It’s
  harmless; the PoC filters most stderr noise, but you may still see one line.
- Extensions injecting `content.js` can spam the console. If the page feels odd,
  try an incognito window with extensions disabled.

---

## Roadmap / ideas

These are scoped deliberately small so they’re easy to add incrementally:

- Agent panel
  - list active sub‑agents with color coding and state (working/idle/error)
  - per‑agent actions: Send, Close, Tail stderr
  - small floating tail console with a ring buffer (e.g., last 500 lines)
- Autopilot controls
  - explicit “Enable/Disable autopilot” switch that gates spawn/send/close control
    blocks and auto‑approvals
  - visual warning banner when autopilot is enabled
- Safety & policy
  - option to run Codex without dangerous mode by default
  - simple policy hook to approve/deny commands based on path/regex
- Quality of life
  - multi‑session selector and `/events?session=` stream
  - minimal auth (shared secret or localhost‑only lock)
  - JSONL transcript export and per‑run logs
- Packaging
  - `pipx`/container recipe, Makefile tasks, and a tiny smoke test

If you want, I can wire any of the above next — the code is intentionally small so
each feature is a focused patch, not a rewrite.

---

## Notes

This dashboard is meant for local iteration and demos. Treat it as untrusted input
from the agent — don’t expose it to the public Internet without adding auth and
tightening policy. The defaults lean toward “manual control,” i.e., the agent only
does what you explicitly ask.
