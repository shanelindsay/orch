# Project Management Guide (Scientific Repos)

This document describes a lightweight, reusable approach to managing work in scientific codebases that use GitHub. It assumes GitHub is the single control plane and that agents run locally (HPC, workstation) while reporting back to Issues/PRs.

---

## Principles

- **Single source of truth.** GitHub Issues drive all work. PRs link back to their Issue; discussion stays in‑issue.
- **Small, verifiable steps.** Prefer small PRs with clear acceptance criteria and rendered outputs for inspection.
- **Reproducibility first.** Commands run via project env wrappers (e.g., `dev/run-in-env.sh`). Outputs render to versioned folders (e.g., `outputs/`), with Quarto/targets configured for deterministic builds.
- **Separation of concerns.** Code computes; QMDs report; manuscript consumes prepared figures/tables.
- **Agent + human collaboration.** Agents execute deterministic work; humans provide judgment and final voice. Labels make this explicit.

---

## Canonical Labels (orthogonal dimensions)

These labels are intentionally compact and orthogonal so you can filter/compose views easily.

**Type**
- `type:pm`, `type:docs`, `type:workflow`, `type:analysis`, `type:infra`, `type:writing`, `type:decision`

**Agentability / Role**
- `role:agent` — agent executes
- `role:agent+qc` — agent executes, human reviews/signs off
- `role:human` — human‑only task

**Run Context**
- `run:flexible` — local/cloud ok (HPC possible)
- `run:hpc-only` — requires HPC (e.g., Slurm)

**Size (effort/coordination; not wall‑clock)**
- `size:xs`, `size:s`, `size:m`, `size:l`, `size:xl` (epics)

**Priority**
- `priority:P0`, `priority:P1`, `priority:P2`

**Status (human workflow status)**
- `status:ready`, `status:in-progress`, `status:blocked`, `status:done`

**Agent Lifecycle (automation control)**
- `orchestrate` — eligible for orchestration
- `agent:queued` — daemon should start/assign an agent
- `agent:running` — agent working (set by daemon)
- `agent:review` — PR opened and awaiting review
- `agent:done` — work finished (Issue can be closed)
- `agent:stalled` — no progress heartbeat within threshold
- `auto:pr-on-complete` — open a PR automatically upon step/issue completion

> Keep these as the *consolidated* label set. Prefer reusing and pruning over adding new variants.

---

## Size Calibration

Size describes effort/coordination complexity, not runtime. A long HPC job can still be **S** if setup is trivial.

- **XS** — one‑touch change (trivial tweak, single file, no design).
- **S** — straightforward change (few files; clear acceptance; no cross‑domain coordination). Typically single agent end‑to‑end.
- **M** — composed change (multiple files or light design; may add a Make/targets rule; new rendered outputs). Human QC recommended.
- **L** — coordinated change (cross‑domain work; sequencing matters; introduces new module/figure family). Usually split into 2–5 S/M children; parent tracks coordination only.
- **XL** — **epic umbrella**; never implement directly. Tracks a theme or milestone across many issues.

**Working agreement**
- Default new issues to `size:s`. If it grows, split rather than letting scope creep.
- Use a parent L/XL only for coordination/roadmap; do not attach code changes directly to the parent.

---

## Split vs. Checklist

Split into sub‑issues when any are true:
- Multiple assignees or handoffs (agent → human QC).
- Cross‑domain coordination (e.g., Makefile + targets + QMD + manuscript).
- Independent scheduling (some parts can land earlier).

Keep a single issue with a checklist when items are small verifications under one assignee and one PR.

---

## Projects (Boards)

Recommended **Project fields**: `Status`, `Priority`, `Size`, `RunContext`, `Type`, `Agentability`, `Epic`.

- Default board columns: Now / Next / Later / Blocked / Done.
- Saved views: by assignee; by run context (flexible/HPC); by epic; by area (Type).
- A small GitHub Action can sync labels ↔ project fields (optional). Labels remain the source of truth.

---

## Roles: Agents and Humans

- **`role:agent`** — deterministic coding, refactors, converting exploratory notes to QMDs, rendering reports, label/Project hygiene, data plumbing, reproducible runs.
- **`role:agent+qc`** — publication‑facing outputs; agent produces; human reviews/signs off.
- **`role:human`** — framing/interpretation, key trade‑offs, journal selection, final manuscript voice, external communications.

---

## Examples

- **Choose journal**  
  Labels: `type:decision`, `decision:journal`, `decision:open`, `role:human`, `size:s`, `priority:P1`.  
  Deliverable: short decision note; then flip to `decision:made` and spawn submission tasks.

- **Add a QC Make target and Project view**  
  Labels: `type:workflow`, `role:agent+qc`, `run:flexible`, `size:m`.  
  Acceptance: new Make target; Project saved view by run context; README snippet.

- **Robustness checks for Exp2**  
  Parent: `size:l`, `type:analysis` (coordination only).  
  Children: each diagnostic `size:s|m`, `run:hpc-only`, `role:agent+qc`.

---

## Working Agreements (summary)

- Labels are canonical. Use the set above; avoid near‑duplicates.
- Epics are coordination only. Code lands in S/M children.
- Keep PRs small and verifiable; link back to the driving Issue.
- Post rendered artifacts (or links) in the Issue for review.
- Prefer splitting to keep scope and context manageable.

