#!/usr/bin/env python3
"""Interactive CLI for the Codex hub without the web dashboard."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import threading
from typing import Optional

from codex_hub_core import Hub, install_signal_handlers

try:
    import github_sync
except ImportError:  # pragma: no cover - optional dependency
    github_sync = None


class Palette:
    """Simple colour palette manager with optional ANSI output."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.colors = {
            "you": "\x1b[38;5;171m",
            "orch": "\x1b[38;5;39m",
            "agent": "\x1b[38;5;208m",
            "work": "\x1b[38;5;111m",
            "ok": "\x1b[38;5;71m",
            "warn": "\x1b[38;5;178m",
            "err": "\x1b[38;5;203m",
            "muted": "\x1b[38;5;244m",
        }
        self.reset = "\x1b[0m"

    def c(self, key: str) -> str:
        if not self.enabled:
            return ""
        return self.colors.get(key, "")

    def r(self) -> str:
        return self.reset if self.enabled else ""


class StdinBridge:
    """Bridge blocking stdin reads into the asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            line = sys.stdin.readline()
            if not line:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, ":quit")
                break
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line.rstrip("\n"))


HELP = """Commands (prefix : ; free text goes to orchestrator)\n""" "\n" \
    "  :help                  Show this help\n" \
    "  :agents                List agents and states\n" \
    "  :state                 Show raw state map\n" \
    "  :say <text>            Send text to orchestrator\n" \
    "  :spawn <name> <task>   Spawn a sub-agent\n" \
    "  :send <name> <task>    Send text to a sub-agent\n" \
    "  :close <name>          Close a sub-agent\n" \
    "  :stderr <name> [N]     Show last N stderr lines (default 100)\n" \
    "  :tail <name|off>       Follow stderr until :tail off or Ctrl+C\n" \
    "  :autopilot on|off      Toggle acting on control blocks / approvals\n" \
    "  :issue <num>           Show Goal/Acceptance/Scope/Validation for issue\n" \
    "  :issue-prompt <num>    Send issue summary to orchestrator\n" \
    "  :issue-list            List open issues labelled 'orchestrate'\n" \
    "  :gh-issue <num> <text> Comment on GitHub issue via gh CLI\n" \
    "  :gh-pr <num> <text>    Comment on GitHub PR via gh CLI\n" \
    "  :statefeed on|off      Toggle live state change events\n" \
    "  :quit | :exit          Quit\n" \
    "Examples:\n" \
    "  hello world            (to orchestrator)\n" \
    "  :spawn coder Build the thing\n" \
    "  :send coder Run tests\n"


def _git_cmd(path: str, args: list[str]) -> str:
    """Run a git command within ``path`` and return stripped stdout."""
    cmd = ["git", "-C", path, *args]
    return subprocess.run(cmd, capture_output=True, check=True, text=True).stdout.strip()


def detect_git_context(path: str) -> Optional[dict[str, Optional[str]]]:
    """Return git repo metadata if ``path`` is inside a repository."""
    try:
        root = _git_cmd(path, ["rev-parse", "--show-toplevel"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    info: dict[str, Optional[str]] = {
        "root": root,
        "name": os.path.basename(root) or root,
    }

    try:
        branch = _git_cmd(path, ["rev-parse", "--abbrev-ref", "HEAD"])
        info["branch"] = branch if branch != "HEAD" else None
    except subprocess.CalledProcessError:
        info["branch"] = None

    try:
        info["commit"] = _git_cmd(path, ["rev-parse", "--short", "HEAD"])
    except subprocess.CalledProcessError:
        info["commit"] = None

    return info


def print_startup_context() -> None:
    """Print the current working directory and git info if available."""
    cwd = os.getcwd()
    print(f"Current directory: {cwd}")

    git_info = detect_git_context(cwd)
    if not git_info:
        return

    parts: list[str] = []
    branch = git_info.get("branch")
    commit = git_info.get("commit")

    if branch:
        parts.append(f"branch {branch}")
    else:
        parts.append("detached HEAD")

    if commit:
        parts.append(f"commit {commit}")

    details = ", ".join(parts)
    print(f"Git repo: {git_info['name']} ({details})")


class Printer:
    """Pretty-printer for hub events."""

    PREFIX_LABEL_WIDTH = 18

    def __init__(self, palette: Palette) -> None:
        self.p = palette
        self.tail_agent: Optional[str] = None
        self.show_state_events = True
        self._last_states: dict[str, Optional[str]] = {}

    def _format_lines(self, text: str) -> list[str]:
        if not text:
            return [""]
        stripped = text.lstrip()
        if stripped.startswith("```control"):
            return self._format_control_block(text)
        return text.splitlines()

    @staticmethod
    def _format_control_block(text: str) -> list[str]:
        lines = text.splitlines() or [""]
        formatted = ["control payload:"]
        formatted.extend(f"    {line}" for line in lines)
        return formatted

    def line(self, seq: int, label: str, text: str, colour: str, system: bool = False) -> None:
        colour_key = "muted" if system else colour
        label = label.strip()
        label = label[: self.PREFIX_LABEL_WIDTH]
        seq_str = f"{self.p.c('muted')}[{seq:03d}]{self.p.r()}"
        label_str = f"{self.p.c(colour_key)}{label:<{self.PREFIX_LABEL_WIDTH}}{self.p.r()}"
        prefix = f"{seq_str} {label_str}"
        indent = " " * len(prefix)

        for idx, body in enumerate(self._format_lines(text)):
            prefix_out = prefix if idx == 0 else indent
            body_str = f"{self.p.c(colour_key)}{body}{self.p.r()}" if body else ""
            sys.stdout.write(f"{prefix_out} {body_str}\n")
        sys.stdout.flush()

    def event(self, ev: dict) -> None:
        payload = ev.get("payload") or {}
        seq = ev.get("seq")
        who = ev.get("who")
        etype = ev.get("type")

        if seq is None:
            return

        if etype == "user_to_orch":
            self.line(seq, "You→ORCH", payload.get("text", ""), "you")
        elif etype == "orch_to_user":
            self.line(seq, "ORCH→You", payload.get("text", ""), "orch")
        elif etype == "orch_to_agent":
            agent = payload.get("agent", "?")
            action = (payload.get("action") or "").upper()
            headline = f"ORCH→{agent}"[: self.PREFIX_LABEL_WIDTH]
            text = payload.get("text", "")
            if action:
                text = f"[{action}] {text}" if text else f"[{action}]"
            self.line(seq, headline, text, "agent")
        elif etype == "agent_to_orch":
            self.line(seq, f"{who}→ORCH", payload.get("text", ""), "agent")
        elif etype == "task_started":
            msg = payload.get("text") or "Working"
            subject = who or "task"
            self.line(seq, "status", f"{subject}: {msg}", "work", system=True)
        elif etype == "error":
            msg = payload.get("message") or "Unknown error"
            self.line(seq, f"{who} error", msg, "err")
        elif etype == "agent_state":
            if not self.show_state_events:
                return
            agent = payload.get("agent", "?")
            state = payload.get("state", "unknown")
            previous = self._last_states.get(agent)
            if previous is None:
                indicator = "•"
            elif previous == state:
                indicator = "="
            else:
                indicator = "→"
            self._last_states[agent] = state
            body = f"{agent} {indicator} {state}"
            self.line(seq, "state", body, "muted", system=True)
        elif etype == "agent_added":
            agent = payload.get("agent", "")
            self.line(seq, "agents", f"added {agent}", "ok", system=True)
        elif etype == "agent_removed":
            agent = payload.get("agent", "")
            self.line(seq, "agents", f"removed {agent}", "warn", system=True)
        elif etype == "autopilot_state":
            enabled = bool(payload.get("enabled"))
            status = "ENABLED" if enabled else "DISABLED"
            colour = "ok" if enabled else "warn"
            self.line(seq, "autopilot", status, colour, system=True)
        elif etype == "autopilot_suppressed":
            summary = payload.get("summary", "control")
            self.line(seq, "autopilot", f"Suppressed {summary}", "warn", system=True)
        elif etype == "agent_stderr":
            if self.tail_agent and who == self.tail_agent:
                sys.stderr.write(payload.get("line", "") + "\n")
                sys.stderr.flush()


def format_agents(hub: Hub) -> str:
    names = ["app-server", "orchestrator"] + sorted(hub.subs.keys())
    parts = []
    for name in names:
        state = hub.agent_state.get(name, "unknown")
        parts.append(f"  - {name} [state: {state}]")
    return "Agents:\n" + "\n".join(parts)


async def handle_command(hub: Hub, printer: Printer, raw: str) -> bool:
    text = raw.strip()
    if not text:
        return True

    is_cmd = text[0] in {":", "/", "."}
    if not is_cmd:
        await hub.send_to_orchestrator(text)
        return True

    parts = text[1:].split()
    if not parts:
        return True
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in {"quit", "exit"}:
        return False

    if cmd in {"help", "?"}:
        print(HELP)
        return True

    if cmd == "agents":
        print(format_agents(hub))
        return True

    if cmd == "state":
        print("State:", hub.agent_state)
        return True

    if cmd == "say":
        if not args:
            print("Usage: :say <text>")
            return True
        payload = " ".join(args)
        await hub.send_to_orchestrator(payload)
        return True

    if cmd == "spawn":
        if len(args) < 2:
            print("Usage: :spawn <name> <task...>")
            return True
        name = args[0]
        task_text = " ".join(args[1:])
        await hub.spawn_sub(name, task_text, hub.default_cwd)
        return True

    if cmd == "send":
        if len(args) < 2:
            print("Usage: :send <name> <task...>")
            return True
        name = args[0]
        task_text = " ".join(args[1:])
        await hub.send_to_sub(name, task_text)
        return True

    if cmd == "close":
        if len(args) != 1:
            print("Usage: :close <name>")
            return True
        await hub.close_sub(args[0])
        return True

    if cmd == "stderr":
        if not args:
            print("Usage: :stderr <name> [N]")
            return True
        alias = args[0]
        name = {
            "app": "app-server",
            "app-server": "app-server",
            "orch": "orchestrator",
        }.get(alias, alias)
        count = int(args[1]) if len(args) > 1 else 100
        lines = list(hub._stderr_buf.get(name, []))[-count:]
        if not lines:
            print(f"No stderr for '{name}'")
        else:
            print(f"--- stderr last {len(lines)} lines for {name} ---")
            for line in lines:
                sys.stderr.write(line + "\n")
            sys.stderr.flush()
        return True

    if cmd == "tail":
        if not args:
            print("Usage: :tail <name|off>")
            return True
        alias = args[0].lower()
        target = {
            "app": "app-server",
            "app-server": "app-server",
            "orch": "orchestrator",
        }.get(alias, alias)
        if target == "off":
            printer.tail_agent = None
            print("Tail off")
            return True
        if target not in {"orchestrator", "app-server"} and target not in hub.subs:
            print(f"No such agent '{target}'")
            return True
        printer.tail_agent = target
        print(f"Tailing stderr for {target}. Use :tail off to stop or Ctrl+C.")
        return True

    if cmd == "autopilot":
        if not args or args[0].lower() not in {"on", "off"}:
            print("Usage: :autopilot on|off")
            return True
        enabled = args[0].lower() == "on"
        await hub.set_autopilot(enabled)
        status = "ENABLED" if enabled else "DISABLED"
        print(f"Autopilot {status}")
        return True

    if cmd == "issue-list":
        if github_sync is None:
            print("GitHub helpers unavailable (module import failed).")
            return True
        repo = hub.default_cwd or os.getcwd()
        try:
            issues = github_sync.list_orchestrate_issues(repo)
        except github_sync.GitHubError as exc:
            print(f"GitHub error: {exc}")
            return True
        if not issues:
            print("No open issues with label 'orchestrate'.")
            return True
        print("Open orchestrate issues:")
        for item in issues:
            labels = ", ".join(item.labels)
            suffix = f" [{labels}]" if labels else ""
            print(f"  - #{item.number} {item.title} ({item.state}){suffix}")
        return True

    if cmd in {"issue", "issue-prompt", "issueprompt"}:
        if github_sync is None:
            print("GitHub helpers unavailable (module import failed).")
            return True
        if not args:
            print("Usage: :issue <number> | :issue-prompt <number>")
            return True
        try:
            issue_number = int(args[0])
        except ValueError:
            print("Issue number must be an integer")
            return True
        repo = hub.default_cwd or os.getcwd()
        try:
            issue = github_sync.fetch_issue(repo, issue_number)
        except github_sync.GitHubError as exc:
            print(f"GitHub error: {exc}")
            return True
        charter = github_sync.parse_issue_body(issue.body)
        prompt = github_sync.format_issue_prompt(issue, charter)
        print(prompt)
        if cmd != "issue":
            await hub.send_to_orchestrator(prompt)
        return True

    if cmd == "gh-issue":
        if github_sync is None:
            print("GitHub helpers unavailable (module import failed).")
            return True
        if len(args) < 2:
            print("Usage: :gh-issue <number> <comment...>")
            return True
        try:
            number = int(args[0])
        except ValueError:
            print("Issue number must be an integer")
            return True
        comment = " ".join(args[1:])
        repo = hub.default_cwd or os.getcwd()
        try:
            github_sync.comment_issue(repo, number, comment)
        except github_sync.GitHubError as exc:
            print(f"GitHub error: {exc}")
            return True
        print(f"Commented on issue #{number}.")
        return True

    if cmd == "gh-pr":
        if github_sync is None:
            print("GitHub helpers unavailable (module import failed).")
            return True
        if len(args) < 2:
            print("Usage: :gh-pr <number> <comment...>")
            return True
        try:
            number = int(args[0])
        except ValueError:
            print("PR number must be an integer")
            return True
        comment = " ".join(args[1:])
        repo = hub.default_cwd or os.getcwd()
        try:
            github_sync.comment_pr(repo, number, comment)
        except github_sync.GitHubError as exc:
            print(f"GitHub error: {exc}")
            return True
        print(f"Commented on PR #{number}.")
        return True

    if cmd == "statefeed":
        if not args or args[0].lower() not in {"on", "off"}:
            print("Usage: :statefeed on|off")
            return True
        enabled = args[0].lower() == "on"
        printer.show_state_events = enabled
        status = "enabled" if enabled else "disabled"
        print(f"State change events {status}.")
        return True

    print(f"Unknown command: {cmd}. Try :help")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CLI Codex hub")
    parser.add_argument("--seed", default="You may begin planning tasks.", help="Initial brief for the orchestrator")
    parser.add_argument("--cwd", default=None, help="Default working directory for agents")
    parser.add_argument("--codex-path", default="codex", help="Path to the codex binary")
    parser.add_argument("--dangerous", action=argparse.BooleanOptionalAction, default=True, help="Toggle auto approvals and sandbox bypass")
    parser.add_argument("--model", default=None, help="Optional model override passed down to children")
    parser.add_argument("--no-colour", action="store_true", help="Disable ANSI colours")
    parser.add_argument("--script", default=None, help="Run commands from a file and exit")
    return parser


async def run_cli(args: argparse.Namespace) -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    install_signal_handlers(loop, stop_event)

    colours_enabled = sys.stdout.isatty() and (not args.no_colour)
    palette = Palette(enabled=colours_enabled)
    printer = Printer(palette)

    print_startup_context()

    hub = Hub(codex_path=args.codex_path, dangerous=bool(args.dangerous), default_cwd=args.cwd, model=args.model)
    await hub.start(seed_text=args.seed)
    queue = hub.subscribe()

    stdin_bridge: Optional[StdinBridge] = None
    script_lines: list[str] = []
    if args.script:
        if not os.path.exists(args.script):
            print(f"Script file not found: {args.script}")
            await hub.stop()
            return
        with open(args.script, "r", encoding="utf-8") as handle:
            script_lines = [line.rstrip("\n") for line in handle]
        script_lines.append(":quit")
    else:
        stdin_bridge = StdinBridge(loop)
        stdin_bridge.start()

    async def pump_events() -> None:
        while True:
            ev = await queue.get()
            printer.event(ev)

    async def pump_input() -> None:
        try:
            if script_lines:
                for line in script_lines:
                    cont = await handle_command(hub, printer, line)
                    if not cont:
                        stop_event.set()
                        return
                return
            assert stdin_bridge is not None
            while not stop_event.is_set():
                line = await stdin_bridge.queue.get()
                cont = await handle_command(hub, printer, line)
                if not cont:
                    stop_event.set()
                    return
        except asyncio.CancelledError:
            return

    tasks = [
        asyncio.create_task(pump_events(), name="events"),
        asyncio.create_task(pump_input(), name="input"),
    ]

    await stop_event.wait()

    for task in tasks:
        task.cancel()
    if stdin_bridge:
        stdin_bridge.stop()
    await hub.stop()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
