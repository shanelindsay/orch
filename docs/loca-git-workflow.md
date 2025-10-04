# Basic task workflow for agents

## Global rules

* Treat the linked **GitHub Issue as the brief**. Follow its Goal, Acceptance checklist, and Scope notes.
* Keep changes small and focused. Prefer multiple small PRs over one large PR.
* Commit frequently

## Decide the lane

* **Extra-small (XS) tasks**: a single file or trivial edit with no behavioural risk, no tests needed, and no CI impacts.
  Examples: tweak README, fix a comment, rename a label, update a link.
* **Everything else**: use a **worktree + Draft PR**.
---
## Lane A: Extra-small direct to main

Only if the repository allows it and the change is truly trivial.

1. Pull latest `main`/`master`.
2. Make the minimal change.
3. Run the quickest relevant check (for example a linter on the touched file).
4. Commit with a clear message:
5. Push to `main`.
6. Comment on Issue with a one-line summary and tick the acceptance item. If the repo requires PRs for all changes, **do not** use this lane.

---

## Lane B: Standard flow (worktree + Draft PR)

Use this for small to typical tasks.

1. **Create a worktree/branch**

   * Branch name: `issue/<number>-<slug>` (for example `issue/123-add-ci-badge`). If part of an EPIC, use epic slug i.e e1/issuenumber-slug
   * Work only in this worktree.
   * If needed, create simlinks for data storage (only for pipelines where that data will be used)

2. **Do the work**

   * Stay within the Issue’s declared scope.
   * Commit regularly in small, meaningful steps with messages like:
     
3. **Open a Draft PR early**

   * Title: concise and imperative (for example “Add CI badge to README”).
   * Good level of details in the PR comments

4. **Iterate until acceptance is satisfied**

   * Keep commits small; push frequently.
   * Update the PR body’s checklist as you complete each item.
   * If you exceed scope or size, split into follow-up PRs and note that in the Issue.

5. **Mark Ready for review**

   * When all acceptance items are ticked or the PR is clearly completed, change the PR from **Draft** to **Ready**.
   * Request review from the designated human or reviewing agent (as per repo practice).

6. **After merge**

   * The Issue should auto-close via `Fixes #123`. If not, comment a short completion note and close it.

---

## Good conventions (recommended)

* **Commit prefixes**: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`.
* **Branch protection**: do not self-merge unless explicitly allowed by label or instruction.

If you paste this into a CONTRIBUTING.md or an Issue/PR template, agents like Codex or Terragon will follow it reliably without extra orchestration.
