#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from codex_hub_core import Hub, install_signal_handlers
import github_sync as ghx

LABEL_ORCHESTRATE = "orchestrate"
LABEL_Q = "agent:queued"
LABEL_RUN = "agent:running"
LABEL_REVIEW = "agent:review"
LABEL_DONE = "agent:done"
LABEL_STALLED = "agent:stalled"
LABEL_PR_ON_COMPLETE = "auto:pr-on-complete"

STATE_DIR = ".orch/state"


def repo_root(cwd: str) -> str:
    return ghx.git_root(cwd)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "task"


def worktree_paths(root: str, issue_no: int, title: str) -> Tuple[str, str]:
    branch = f"ai/iss-{issue_no}-{slugify(title)}"
    wt_dir = os.path.join(root, ".worktrees", f"iss-{issue_no}")
    return branch, wt_dir


def state_path(root: str, issue_no: int) -> str:
    path = Path(root) / STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return str(path / f"issue-{issue_no}.json")


def load_state(root: str, issue_no: int) -> dict:
    path = state_path(root, issue_no)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_state(root: str, issue_no: int, data: dict) -> None:
    path = state_path(root, issue_no)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def now_ts() -> float:
    return time.time()


async def start_for_issue(hub: Hub, repo: str, root: str, issue: ghx.IssueDetails) -> None:
    charter = ghx.parse_issue_body(issue.body)
    prompt = ghx.format_issue_prompt(issue, charter)
    branch, wt_dir = worktree_paths(root, issue.number, issue.title)
    ghx.ensure_worktree(root, branch, wt_dir)
    agent_name = f"iss{issue.number}"
    initial = (
        f"{prompt}\n\nWork in this repo worktree only:\n"
        f"- branch: {branch}\n- worktree: {wt_dir}\n"
        "When you finish a coherent step, write an end-of-step report."
    )
    await hub.spawn_sub(agent_name, initial, wt_dir)
    ghx.replace_labels(repo, issue.number, add=[LABEL_RUN], remove=[LABEL_Q, LABEL_STALLED])
    try:
        ghx.comment_issue(repo, issue.number, f"🧑‍💻 Agent **{agent_name}** started on worktree `{branch}` (`{wt_dir}`).")
    except Exception:
        pass
    save_state(
        root,
        issue.number,
        {
            "agent": agent_name,
            "branch": branch,
            "worktree": wt_dir,
            "status": "running",
            "last_activity": now_ts(),
        },
    )


def open_pr_if_needed(root: str, repo: str, issue: ghx.IssueDetails, state: dict) -> Optional[str]:
    branch = state.get("branch") or ""
    title = f"Issue #{issue.number}: {issue.title}"
    pr_url = ghx.ensure_pr(root, issue.number, branch, title)
    if pr_url:
        ghx.replace_labels(repo, issue.number, add=[LABEL_REVIEW], remove=[LABEL_RUN, LABEL_STALLED, LABEL_Q])
        try:
            ghx.comment_issue(repo, issue.number, f"📬 Opened PR: {pr_url}")
        except Exception:
            pass
    return pr_url


def stale_since(timestamp: float, minutes: int) -> bool:
    return (now_ts() - timestamp) > (minutes * 60)


async def mirror_events_to_github(hub: Hub, root: str, repo: str) -> None:
    queue = hub.subscribe()
    try:
        while True:
            event = await queue.get()
            kind = str(event.get("type") or "")
            who = str(event.get("who") or "")
            payload = event.get("payload") or {}
            match = re.match(r"^iss(\d+)$", who)
            issue_no = int(match.group(1)) if match else None

            if kind == "agent_to_orch" and issue_no:
                text = (payload.get("text") or "").strip()
                if text:
                    try:
                        ghx.comment_issue(repo, issue_no, text)
                    except Exception:
                        pass
                state = load_state(root, issue_no)
                state["last_activity"] = now_ts()
                state.pop("stalled_at", None)
                state.setdefault("status", "running")
                save_state(root, issue_no, state)
                try:
                    ghx.replace_labels(repo, issue_no, add=[], remove=[LABEL_STALLED])
                except Exception:
                    pass

            elif kind == "agent_removed" and issue_no:
                state = load_state(root, issue_no)
                state["status"] = "complete"
                state["completed_at"] = now_ts()
                save_state(root, issue_no, state)
                try:
                    issue = ghx.fetch_issue(repo, issue_no)
                except Exception:
                    issue = None
                labels = set(issue.labels or []) if issue else set()
                pr_url = None
                if issue and LABEL_PR_ON_COMPLETE in labels:
                    pr_url = open_pr_if_needed(root, repo, issue, state)
                    if pr_url:
                        state["pr_url"] = pr_url
                        save_state(root, issue_no, state)
                removes = [LABEL_Q, LABEL_RUN, LABEL_STALLED]
                try:
                    if pr_url:
                        ghx.replace_labels(repo, issue_no, add=[], remove=removes)
                    else:
                        ghx.replace_labels(repo, issue_no, add=[LABEL_DONE], remove=removes)
                        ghx.comment_issue(repo, issue_no, "✅ Agent finished; label set to agent:done.")
                except Exception:
                    pass

            elif kind == "orch_to_user":
                continue
    except asyncio.CancelledError:
        pass
    finally:
        hub.unsubscribe(queue)


async def daemon(args: argparse.Namespace) -> None:
    root = repo_root(args.cwd or os.getcwd())
    repo = root
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    install_signal_handlers(loop, stop_event)

    hub = Hub(
        codex_path=args.codex_path,
        dangerous=args.dangerous,
        default_cwd=root,
        model=args.model,
        github_poll=False,
    )
    await hub.start("GitHub-driven mode. I will coordinate Issues and PRs using control blocks.")
    if args.autopilot_on:
        await hub.set_autopilot(True)
    mirror_task = asyncio.create_task(mirror_events_to_github(hub, root, repo), name="mirror-events")

    try:
        while not stop_event.is_set():
            issues = ghx.list_orchestrate_issues(repo, limit=200)
            for issue in issues:
                labels = set(issue.labels or [])
                state = load_state(root, issue.number)
                agent_name = state.get("agent")
                agent_active = bool(agent_name and agent_name in hub.subs)

                if LABEL_Q in labels and state.get("status") != "running":
                    await start_for_issue(hub, repo, root, issue)
                    continue

                if LABEL_RUN in labels and not agent_active:
                    await start_for_issue(hub, repo, root, issue)
                    continue

                if LABEL_RUN in labels:
                    last = state.get("last_activity") or 0
                    if last and stale_since(last, args.stale_minutes):
                        if not state.get("stalled_at"):
                            ghx.replace_labels(repo, issue.number, add=[LABEL_STALLED], remove=[])
                            try:
                                ghx.comment_issue(repo, issue.number, "⏳ Agent appears stalled; orchestrator will triage.")
                            except Exception:
                                pass
                            state["stalled_at"] = now_ts()
                            save_state(root, issue.number, state)
                elif state.get("stalled_at"):
                    state.pop("stalled_at", None)
                    save_state(root, issue.number, state)

                if LABEL_PR_ON_COMPLETE in labels and state.get("status") == "complete" and not state.get("pr_url"):
                    pr_url = open_pr_if_needed(root, repo, issue, state)
                    if pr_url:
                        state["pr_url"] = pr_url
                        save_state(root, issue.number, state)

            await asyncio.sleep(args.poll_secs)
    finally:
        mirror_task.cancel()
        try:
            await mirror_task
        except asyncio.CancelledError:
            pass
        await hub.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local GitHub-driven orchestrator daemon")
    parser.add_argument("--cwd", default=None, help="Path to local git repo (default: cwd)")
    parser.add_argument("--codex-path", default="codex", help="Path to Codex binary")
    parser.add_argument("--model", default=None, help="Optional model override")
    parser.add_argument("--poll-secs", type=int, default=25, help="Polling interval seconds")
    parser.add_argument("--stale-minutes", type=int, default=30, help="Mark agent stalled after X minutes of silence")
    parser.add_argument("--dangerous", action=argparse.BooleanOptionalAction, default=True, help="Allow local exec when autopilot enabled")
    parser.add_argument("--autopilot-on", action="store_true", help="Start with autopilot enabled")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(daemon(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
