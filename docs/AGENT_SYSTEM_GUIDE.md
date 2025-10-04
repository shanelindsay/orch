# Agent System Guide (GitHub‑Driven Orchestration)

This document explains how the agent system operates when **GitHub is the only control surface** and all execution happens **locally** (e.g., on an HPC login node inside `tmux`). It complements the Project Management Guide.

---

## High‑Level Architecture

**Control plane:** GitHub (labels, comments, PRs, Projects)  
**Brains:** Codex Orchestrator (a Codex conversation)  
**Workers:** Codex Sub‑agents (one conversation per Issue)  
**Runner:** Local Python daemon (`orchestrate_github.py`) + `codex app-server`  
**Repo isolation:** One **git worktree** per Issue (branch `ai/iss-<N>-<slug>`)

The daemon:
- Polls GitHub for Issues labelled `orchestrate` + `agent:queued`.
- Creates a worktree and branch for each picked Issue.
- Starts a sub‑agent conversation bound to that worktree.
- Mirrors **end‑of‑step reports** back to the Issue as comments.
- Manages labels: `agent:queued` → `agent:running` → (`agent:review`/`agent:done`) or `agent:stalled`.
- (Optional) On completion with `auto:pr-on-complete`, opens a PR and applies `agent:review`.

---

## Orchestrator & Sub‑agent Prompts (defaults)

**Orchestrator (system):**
> You are the ORCHESTRATOR. Plan work, coordinate named sub‑agents (each bound to a git worktree), and iterate until goals are met. Communicate clearly with short status notes for humans.  
> Emit control blocks when you want the hub to act:
> ```control
> {"spawn":{"name":"<agent>","task":"<task>","cwd":null}}
> ```
> ```control
> {"send":{"to":"<agent>","task":"<instruction>"}}
> ```
> ```control
> {"close":{"agent":"<agent>"}}
> ```
> Optional (when allowed):  
> ```control
> {"exec":{"cwd":"<path>","argv":["git","status"]}}
> ```
> Write normal prose updates for the human.

**Sub‑agent (system template):**
> You are a SUB‑AGENT named “{name}”. Work exclusively within this repo worktree. Provide succinct progress updates. When you finish a coherent step, write an **end‑of‑step report**: a short summary, what changed (files/commands), and suggested next actions.

**Initial message to a sub‑agent includes the Issue charter** (Goal, Acceptance, Scope, Validation) and its worktree path/branch.

---

## Control Blocks (JSON fenced as ```control)

- `spawn` — create/start a sub‑agent, optionally with `cwd` (worktree).
- `send` — send follow‑up instruction to an existing sub‑agent.
- `close` — stop/close a sub‑agent.
- `exec` *(optional)* — request a local command (allow‑listed) such as `git`/`gh`. Executed only when **autopilot** and **dangerous** are enabled.

The daemon executes these immediately and mirrors concise results to the Issue/PR as comments.

---

## Issue → Agent Lifecycle (state machine)

**Labels drive orchestration.**

1. **Queue**  
   Human sets: `orchestrate`, `agent:queued` (+ other dimension labels).  
   _Daemon picks it up on next poll._

2. **Start / Worktree**  
   Daemon creates branch `ai/iss-<N>-<slug>` and worktree `.worktrees/iss-<N>`, starts sub‑agent, comments “Agent started…”, applies `agent:running` (removes `agent:queued`).

3. **Working / Heartbeats**  
   Each sub‑agent message updates a local `last_activity` timestamp.  
   If no activity for *X minutes* (default 30), daemon applies `agent:stalled` and pings the orchestrator to triage.

4. **End‑of‑step report**  
   Sub‑agent writes a short structured report (summary, changed files/commands, next actions).  
   Daemon mirrors this as an Issue comment and keeps `agent:running`.

5. **Done / PR**  
   - If the issue has `auto:pr-on-complete`, daemon opens a PR from the branch and applies `agent:review`.  
   - Otherwise daemon applies `agent:done` and comments the final report.  
   Humans review/merge/close the Issue; or orchestrator continues with more steps.

---

## PR Flow (simplified)

- On completion of a step, either:
  - Orchestrator emits `exec` to open a PR (`gh pr create --fill --head <branch>`), or
  - Issue has `auto:pr-on-complete` and daemon opens it automatically.
- Daemon applies `agent:review` and posts the PR URL.
- Human review proceeds as normal. On merge, close the Issue (or the daemon/orchestrator can close it via `exec gh issue close` if desired).

---

## Heartbeats, Stalls, Recovery

- Each sub‑agent has a `last_activity` timestamp (persisted under `.orch/state/issue-<N>.json`).
- If silent beyond the threshold, daemon marks `agent:stalled` and posts a triage note.
- Recovery options:
  - Human adds a clarifying comment or removes `agent:stalled` after intervention.
  - Orchestrator emits `send` to re‑prompt the sub‑agent.
  - If necessary, `close` and re‑`spawn` (fresh state).

---

## Running the System (local/HPC)

Start the daemon in `tmux` from the repo root:

```bash
python3 orchestrate_github.py \
  --codex-path codex \
  --poll-secs 20 \
  --dangerous \
  --autopilot-on
```

* **Dangerous + Autopilot** on → `exec` control blocks are permitted (allow‑listed git/gh commands only).
* Without either flag, `exec` is denied; `spawn/send/close` still work.

---

## Human Interaction Model

* **To start work:** add labels `orchestrate`, `agent:queued`.
* **To request a PR on completion:** add `auto:pr-on-complete`.
* **To steer:** write a normal Issue comment; the orchestrator reads Issue context + last end‑of‑step report and can reply with `send`/`exec`.
* **To stop:** remove `agent:running` and/or add a note; orchestrator can `close` the agent.

Everything is visible in GitHub: comments, labels, PRs, and branch diffs. The CLI remains available for debugging, but is not required.

---

## Defaults & Conventions

* **Worktree layout**: `.worktrees/iss-<N>` bound to branch `ai/iss-<N>-<slug>`.
* **Commit messages**: `iss-<N>: <summary>`; include `Refs #<N>`.
* **End‑of‑step report** (sub‑agent template):

  ```
  ### Summary
  <2–6 lines>

  ### Changes
  - files: <paths>
  - commands: <key commands run>

  ### Next Actions
  1) …
  2) …
  ```
* **Artifacts**: small images/log snippets inline; larger outputs stored under `outputs/` and referenced by path.

---

## Safety & Permissions

* You indicated **privacy is not a concern** and agents have **high permissions** locally.
* We still gate `exec` by:

  * **Allow‑list** of subcommands (`git`, `gh`) in the runner.
  * **Autopilot + Dangerous** must both be enabled to execute.
  * All `exec` results are mirrored back as plain text (command, cwd, code, stdout/stderr).

---

## Context Management (brief)

* Orchestrator and sub‑agents get concise **charter context** (Goal, Acceptance, Scope, Validation) and latest end‑of‑step comments.
* Full history lives in GitHub. Agents summarize within their context windows and can request refresh via a short “recap” from the Issue body/comments if needed.

---

## Label ↔ Project Field Sync (optional)

If you want Project fields to mirror labels, add a small workflow to map label → field value. Labels remain the canonical state; Projects are a synchronized view.

---

## Glossary (quick)

* **Orchestrator**: Codex conversation that plans and coordinates agents via control blocks.
* **Sub‑agent**: Codex conversation working a single Issue, constrained to its worktree.
* **Daemon**: Local Python process that polls GitHub, manages worktrees, forwards messages, executes allowed `exec`, and maintains heartbeats.
* **Autopilot**: Hub acts on control blocks automatically.
* **Dangerous**: Approves `exec` (within allow‑list).

---

## Why this structure works

- **Human mental load stays on GitHub.** You see Issues, labels, PRs, and concise end‑of‑step reports—nothing else is required.
- **Orchestrator is still an agent.** It decides what to do next, coordinates parallel sub‑agents, and uses control blocks; the daemon executes those quickly and safely.
- **Stall protection and recovery** are “just labels” plus a heartbeat—visible and fixable from GitHub.
- **Reproducibility is preserved** through worktrees, branch naming, and env wrappers; artifacts are versioned.
