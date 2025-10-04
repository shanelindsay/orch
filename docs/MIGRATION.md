# Migration notes

- Create an annotated tag (e.g., `classic-orch-archive`) at the final classic commit for quick reference.
- `orch-classic` branch holds the original implementation.
- `main` uses the new GitHub-driven exec model.
- Issues/PRs are the single source of truth for plans and progress.
- Use `track:classic` or `track:exec` labels to disambiguate legacy vs new issues.
- Optional workflow helper (`relay-comment-on-checks.yml`) requires `workflows` permission; add it manually once permissions are in place.
