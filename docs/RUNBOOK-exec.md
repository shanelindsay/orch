# Runbook: exec-based orchestration

## One-time setup
- Install `gh`, `jq`, `codex`, and `yq`.
- Create labels (see repository README or migration notes).
- Ensure required checks are configured in branch protections for `main`.

## Everyday flow
1. **Write or refine an Issue** with acceptance checklist and scope notes.
2. Add label **`ready:agent`** (and optional **`safe-lane`** for docs/tests/tooling).
3. Run the orchestrator locally:

   ```bash
   ./exec/orchestrator.sh
   ```

4. Watch it:
   - Creates a worktree and branch for the Issue.
   - Runs `codex exec` to do the work.
   - Opens a Draft PR and flips labels to `pr:draft`, then `checks:green` when passing.
   - If `safe-lane`, the orchestrator enables auto-merge; otherwise it labels `ready:human` for review.

## Human review gates
- For normal code paths: review PRs labelled `ready:human`.
- For `safe-lane`: PR merges automatically once checks pass.

## Resuming context
- The orchestrator uses `codex exec resume --last` for follow-up turns on the same Issue/branch.
