# Orchestration architecture (classic vs exec)

| Aspect | orch-classic (archived) | orch-exec (current, main) |
|---|---|---|
| Control surface | REPL/app-server, optional web UI | CLI-only (`codex exec`), GitHub Issues/PRs as source of truth |
| Triggers | Human commands in REPL | GitHub labels & PR/Checks state (polled via `gh`) |
| Agent turns | Interactive, long-lived sessions | One CLI invocation per turn; `exec resume` for continuity |
| State | In-process session logs | Durable in Issues/PRs; minimal local state under `exec/state/` |
| Parallelism | Manual; tmux panes | Worktrees per Issue; loop schedules by labels |
| Quality gates | Human review | Required checks + small auto-merge lane + human review on demand |
| SSH ergonomics | Good with tmux | Good with tmux (optional) |
