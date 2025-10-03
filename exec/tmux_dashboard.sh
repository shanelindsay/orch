#!/usr/bin/env bash
# Simple tmux layout: left pane orchestrator logs, right panes per active worktree
session="orch"
tmux new-session -d -s "$session" "tail -f exec/state/* 2>/dev/null || watch -n 5 'gh pr list && gh issue list'"
i=1
for d in worktrees/issue*; do
  [ -d "$d" ] || continue
  tmux split-window -h -t "$session" "bash -lc 'cd $d && git status; bash'"
  tmux select-layout tiled
  i=$((i+1))
done
tmux attach -t "$session"
