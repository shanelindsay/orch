#!/usr/bin/env bash
set -euo pipefail

# Minimal local orchestrator that:
# - Finds Issues labelled ready:agent
# - Creates worktrees/branches
# - Runs codex exec (non-interactive) against the Issue text
# - Opens Draft PRs, flips labels, and watches checks for promotion/auto-merge

# Dependencies: gh, jq, git, codex
# Config
CONFIG_FILE="${CONFIG_FILE:-exec/config.yml}"
WORKTREES_ROOT="$(yq '.worktrees_root' "$CONFIG_FILE" 2>/dev/null || echo worktrees)"
BASE_BRANCH="$(yq '.base_branch' "$CONFIG_FILE" 2>/dev/null || echo main)"
POLL_INTERVAL="$(yq '.poll_interval_seconds' "$CONFIG_FILE" 2>/dev/null || echo 20)"

LBL_READY="$(yq '.labels.ready' "$CONFIG_FILE" 2>/dev/null || echo ready:agent)"
LBL_INPROG="$(yq '.labels.in_progress' "$CONFIG_FILE" 2>/dev/null || echo in-progress:agent)"
LBL_PRDRAFT="$(yq '.labels.pr_draft' "$CONFIG_FILE" 2>/dev/null || echo pr:draft)"
LBL_GREEN="$(yq '.labels.checks_green' "$CONFIG_FILE" 2>/dev/null || echo checks:green)"
LBL_READYH="$(yq '.labels.ready_human' "$CONFIG_FILE" 2>/dev/null || echo ready:human)"
LBL_SAFELANE="$(yq '.labels.safe_lane' "$CONFIG_FILE" 2>/dev/null || echo safe-lane)"

ALLOW_EDITS="$(yq '.codex.allow_edits' "$CONFIG_FILE" 2>/dev/null || echo true)"
ALLOW_NET="$(yq '.codex.allow_network' "$CONFIG_FILE" 2>/dev/null || echo false)"
MODEL="$(yq '.codex.model' "$CONFIG_FILE" 2>/dev/null || echo gpt-5-codex)"

mkdir -p exec/state "$WORKTREES_ROOT"

function log() { printf "[%s] %s\n" "$(date -Is)" "$*" >&2; }

function gh_json() { gh api -H "Accept: application/vnd.github+json" "$@"; }

function issues_ready() {
  gh issue list --label "$LBL_READY" --state open --json number,title,url,body,labels | jq -c '.[]'
}

function issue_branch() {
  local num="$1"
  echo "issue/${num}"
}

function create_worktree_and_branch() {
  local num="$1" branch="$2" dir="${WORKTREES_ROOT}/${branch}"
  if [ ! -d "$dir" ]; then
    git fetch origin "$BASE_BRANCH" --quiet
    git worktree add -b "$branch" "$dir" "origin/${BASE_BRANCH}"
    log "Worktree created at $dir for issue #$num branch $branch"
  fi
}

function issue_has_label() {
  local issue="$1" label="$2"
  jq -r --arg L "$label" '.labels[].name | select(.==$L)' <<<"$issue" >/dev/null 2>&1
}

function add_issue_labels() { gh issue edit "$1" --add-label "$2" >/dev/null; }
function remove_issue_labels() { gh issue edit "$1" --remove-label "$2" >/dev/null || true; }

function run_codex_turn() {
  local issue_num="$1" workdir="$2"
  pushd "$workdir" >/dev/null
  local body
  body="$(gh issue view "$issue_num" --json title,body,url --jq '.title + "\n\n" + .body + "\n\nIssue URL: " + .url')"

  local flags=()
  $ALLOW_EDITS && flags+=("--full-auto")
  $ALLOW_NET && flags+=("--sandbox" "danger-full-access")
  flags+=("--model" "$MODEL")
  flags+=("--json")

  log "Running codex exec for issue #$issue_num"
  # Stream JSON events to a log; final messages go to stderr by default
  codex exec "${body}" "${flags[@]}" \
    1> "codex_events_${issue_num}.jsonl" 2> "codex_stderr_${issue_num}.log" || true

  # After the turn, if there are file changes, commit them
  if ! git diff --quiet; then
    git add -A
    git commit -m "agent: apply changes for #${issue_num}"
  fi
  popd >/dev/null
}

function open_or_update_pr() {
  local branch="$1" issue_num="$2" workdir="${WORKTREES_ROOT}/${branch}"

  pushd "$workdir" >/dev/null
  if ! gh pr view --head "$branch" >/dev/null 2>&1; then
    log "Opening Draft PR for #$issue_num"
    gh pr create --draft --fill --head "$branch" --base "$BASE_BRANCH" \
      --title "Agent: ${branch} (closes #${issue_num})" \
      --body "Automated Draft PR for issue #${issue_num}." >/dev/null
  else
    log "PR already exists for branch $branch"
  fi
  popd >/dev/null
}

function set_labels_for_progress() {
  local issue_num="$1"
  add_issue_labels "$issue_num" "$LBL_INPROG"
  remove_issue_labels "$issue_num" "$LBL_READY"
  add_issue_labels "$issue_num" "$LBL_PRDRAFT"
}

function pr_number_for_branch() {
  local branch="$1"
  gh pr list --head "$branch" --json number --jq '.[0].number'
}

function all_checks_green() {
  local pr="$1"
  # Returns 0 if all success, 1 otherwise
  gh pr view "$pr" --json statusCheckRollup \
    --jq '[.statusCheckRollup[] | select(.conclusion!="SUCCESS")] | length==0'
}

function safe_lane_pr() {
  local pr="$1"
  # Consider it safe if the linked Issue has LBL_SAFELANE
  local issue_num
  issue_num="$(gh pr view "$pr" --json closingIssuesReferences --jq '.closingIssuesReferences[0].number')"
  [ -n "$issue_num" ] && gh issue view "$issue_num" --json labels --jq ".labels[].name | select(.==\"$LBL_SAFELANE\")" >/dev/null
}

function promote_or_merge() {
  local pr="$1" issue_num="$2"
  if all_checks_green "$pr" | grep -q true; then
    add_issue_labels "$issue_num" "$LBL_GREEN"
    if safe_lane_pr "$pr"; then
      log "Auto-merging safe-lane PR #$pr"
      gh pr merge "$pr" --squash --auto >/dev/null || true
    else
      log "Promoting PR #$pr to ready:human"
      add_issue_labels "$issue_num" "$LBL_READYH"
    fi
  fi
}

function loop_once() {
  # Start new ready issues
  issues_ready | while read -r issue; do
    num="$(jq -r '.number' <<<"$issue")"
    branch="$(issue_branch "$num")"
    create_worktree_and_branch "$num" "$branch"
    run_codex_turn "$num" "${WORKTREES_ROOT}/${branch}"
    open_or_update_pr "$branch" "$num"
    set_labels_for_progress "$num"
  done

  # Check PRs that are in progress/draft and promote when green
  gh pr list --state open --json number,headRefName | jq -c '.[]' | while read -r pr; do
    prn="$(jq -r '.number' <<<"$pr")"
    branch="$(jq -r '.headRefName' <<<"$pr")"
    # Try to infer the issue number from the branch name convention
    if [[ "$branch" =~ issue/([0-9]+) ]]; then
      issue_num="${BASH_REMATCH[1]}"
      promote_or_merge "$prn" "$issue_num"
    fi
  done
}

log "Starting orchestrator (poll ${POLL_INTERVAL}s)"
while true; do
  loop_once
  sleep "$POLL_INTERVAL"
done
