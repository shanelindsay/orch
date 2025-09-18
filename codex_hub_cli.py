#!/usr/bin/env python3
"""Interactive CLI for the Codex hub without the web dashboard."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from typing import Optional

from codex_hub_core import Hub, install_signal_handlers


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
    "  :quit | :exit          Quit\n" \
    "Examples:\n" \
    "  hello world            (to orchestrator)\n" \
    "  :spawn coder Build the thing\n" \
    "  :send coder Run tests\n"


class Printer:
    """Pretty-printer for hub events."""

    def __init__(self, palette: Palette) -> None:
        self.p = palette
        self.tail_agent: Optional[str] = None

    def line(self, prefix: str, text: str, colour: str) -> None:
        if text and not text.endswith("\n"):
            text = text + "\n"
        sys.stdout.write(f"{self.p.c(colour)}{prefix}{self.p.r()} {text}")
        sys.stdout.flush()

    def event(self, ev: dict) -> None:
        payload = ev.get("payload") or {}
        seq = ev.get("seq")
        who = ev.get("who")
        etype = ev.get("type")

        if etype == "user_to_orch":
            self.line(f"[{seq}] You → ORCH:", payload.get("text", ""), "you")
        elif etype == "orch_to_user":
            self.line(f"[{seq}] ORCH → You:", payload.get("text", ""), "orch")
        elif etype == "orch_to_agent":
            agent = payload.get("agent", "?")
            action = (payload.get("action") or "").upper()
            self.line(f"[{seq}] ORCH → {agent} [{action}]:", payload.get("text", ""), "agent")
        elif etype == "agent_to_orch":
            self.line(f"[{seq}] {who} → ORCH:", payload.get("text", ""), "agent")
        elif etype == "task_started":
            msg = payload.get("text") or "Working"
            self.line(f"[{seq}] {who} working:", msg, "work")
        elif etype == "error":
            msg = payload.get("message") or "Unknown error"
            self.line(f"[{seq}] {who} ERROR:", msg, "err")
        elif etype == "agent_state":
            agent = payload.get("agent")
            state = payload.get("state")
            self.line(f"[{seq}] state:", f"{agent} -> {state}", "muted")
        elif etype == "agent_added":
            self.line(f"[{seq}] added:", payload.get("agent", ""), "ok")
        elif etype == "agent_removed":
            self.line(f"[{seq}] removed:", payload.get("agent", ""), "warn")
        elif etype == "agent_stderr":
            if self.tail_agent and who == self.tail_agent:
                sys.stderr.write(payload.get("line", "") + "\n")
                sys.stderr.flush()


def format_agents(hub: Hub) -> str:
    names = ["orchestrator"] + sorted(hub.subs.keys())
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
        await hub._broadcast({"who": "user", "type": "user_to_orch", "payload": {"text": text}})
        await hub.orch.send_text(text)
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
        await hub._broadcast({"who": "user", "type": "user_to_orch", "payload": {"text": payload}})
        await hub.orch.send_text(payload)
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
        name = args[0]
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
        target = args[0].lower()
        if target == "off":
            printer.tail_agent = None
            print("Tail off")
            return True
        if target != "orchestrator" and target not in hub.subs:
            print(f"No such agent '{target}'")
            return True
        printer.tail_agent = target
        print(f"Tailing stderr for {target}. Use :tail off to stop or Ctrl+C.")
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
