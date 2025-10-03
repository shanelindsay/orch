# orch

GitHub-driven orchestration harness for Codex agents. The `main` branch now centers on short-lived
`codex exec` runs that sync with GitHub Issues and PRs, while the original REPL/app-server flow
is preserved on the `orch-classic` branch (see `docs/RUNBOOK-classic.md`).

## Why switch?
- **Single source of truth** – Issues carry objectives, scope, and acceptance; PRs track execution and checks.
- **Repeatable turns** – Each agent turn is a single `codex exec` invocation, resumable with `codex exec resume`.
- **Git-native parallelism** – Worktrees per Issue keep branches isolated and easy to inspect.
- **Automation ready** – Labels and required checks drive the state machine, including optional auto-merge for safe lanes.

## Prerequisites
- `codex` CLI (authenticated)
- GitHub CLI `gh` (authenticated with repo access)
- `jq` and `yq` for JSON/YAML parsing
- `git`
- Optional: `tmux` for the dashboard helper

## One-time setup
1. Copy the sample config: `cp exec/config.example.yml exec/config.yml` and adjust as needed.
2. Create the labels listed in `docs/MIGRATION.md` (scripted examples in the same file).
3. Ensure branch protections on `main` require the checks you care about.
4. (Optional) Prepare two worktrees if you need to compare with the archived implementation:
   ```bash
   git worktree add ../orch-classic-wt orch-classic
   git worktree add ../orch-exec-wt main
   ```

## Everyday flow
1. Draft or refine an Issue with an acceptance checklist and scope guardrails. Apply the `ready:agent` label (add `safe-lane` if it really is docs/tests/tooling only).
2. Run the orchestrator locally:
   ```bash
   ./exec/orchestrator.sh
   ```
3. The loop will:
   - Create or reuse a worktree/branch named `issue/<number>`.
   - Call `codex exec` with `--full-auto` (and `--sandbox danger-full-access` if allowed in config).
   - Commit agent changes and open a Draft PR tied to the Issue.
   - Flip labels from `ready:agent` → `in-progress:agent` → `pr:draft`.
   - Promote to `checks:green` and `ready:human` (or auto-merge) once required checks succeed.
4. Review PRs labelled `ready:human`. Safe-lane PRs auto-merge when checks stay green.

## Safe-lane automation
Files matching the globs in `exec/config.yml` are safe for the auto-merge lane. Add the `safe-lane` label to an Issue when it only touches docs/tests/tooling. The orchestrator will configure `gh pr merge --auto` for those PRs once checks are green.

## Useful helpers
- `exec/tmux_dashboard.sh` – optional tmux layout following active worktrees.
- `docs/ARCHITECTURE.md` – side-by-side comparison of the classic vs exec models.
- `docs/RUNBOOK-exec.md` – detailed operating guide.
- `docs/MIGRATION.md` – migration checklist, including label creation commands.

## Classic stack archived
- Branch: `orch-classic`
- Tag: create an annotated tag such as `classic-orch-archive` on the last classic commit for quick reference (see `docs/MIGRATION.md`).
- Runbook: `docs/RUNBOOK-classic.md`

## License
MIT
