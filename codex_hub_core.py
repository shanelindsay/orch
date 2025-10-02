#!/usr/bin/env python3
"""Core orchestration logic for the Codex CLI hub."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

DANGEROUS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

ORCHESTRATOR_SYSTEM = (
    "You are the ORCHESTRATOR agent.\n"
    "Plan work, spin up named sub-agents, and iterate until goals are met.\n"
    "Emit control blocks in replies when you want the hub to act:\n\n"
    "```control\n"
    '{"spawn":{"name":"<agent_name>","task":"<task text>","cwd":null}}\n'
    "```\n\n"
    "```control\n"
    '{"send":{"to":"<agent_name>","task":"<follow-up instruction>"}}\n'
    "```\n\n"
    "```control\n"
    '{"close":{"agent":"<agent_name>"}}\n'
    "```\n\n"
    "Also write normal prose updates for the human.\n"
)

SUBAGENT_SYSTEM_TEMPLATE = (
    'You are a SUB-AGENT named "{name}".\n'
    "Follow the task from the user. Provide succinct progress updates and, when finished,\n"
    "give a short summary and suggested next actions."
)

FALLBACK_SYSTEM_PREFIX = "### SYSTEM MESSAGE (treat as system role) ###\n"
CONTROL_BLOCK_RE = re.compile(
    r"```(?:json\\s+)?control\\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE
)


def strip_control_blocks(text: str) -> str:
    if not text:
        return ""
    cleaned = CONTROL_BLOCK_RE.sub("", text)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


def jdump(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def new_id(prefix: str = "req:") -> str:
    return f"{prefix}{uuid.uuid4()}"


def extract_control_blocks(text: str | None) -> List[Dict[str, Any]]:
    if not text:
        return []

    blocks: List[Dict[str, Any]] = []

    for match in CONTROL_BLOCK_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            blocks.append(payload)

    seen = {json.dumps(block, sort_keys=True) for block in blocks}
    for line in text.splitlines():
        candidate = line.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if not any(k in payload for k in ("spawn", "send", "close")):
            continue
        signature = json.dumps(payload, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        blocks.append(payload)

    return blocks


@dataclass
class ProtoEvent:
    who: str
    raw: Dict[str, Any]


@dataclass
class ProtoChild:
    name: str
    codex_path: str = "codex"
    cwd: Optional[str] = None
    dangerous: bool = True
    system_message: Optional[str] = None
    extra_args: List[str] = field(default_factory=list)
    proc: Optional[asyncio.subprocess.Process] = None
    first_send_done: bool = False

    async def start(self) -> None:
        cmd = [self.codex_path]
        if self.dangerous:
            cmd.append(DANGEROUS_FLAG)
        cmd.append("proto")
        if self.system_message:
            initial = (
                'initial_messages=[{"role":"system","content":'
                f"{json.dumps(self.system_message)}" "}]"
            )
            cmd.extend(["-c", initial])
        if self.extra_args:
            cmd.extend(self.extra_args)

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.cwd or os.getcwd(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1 << 16,
        )

    async def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.write_eof()
        except Exception:
            pass

        try:
            await asyncio.wait_for(self.proc.wait(), timeout=1.0)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    async def send_turn_and_text(self, text: str, include_fallback_system: bool = False) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"{self.name}: process not started")

        turn_op = {"id": new_id(), "op": {"type": "user_turn"}}
        self.proc.stdin.write((jdump(turn_op) + "\n").encode())

        items: List[Dict[str, Any]] = []
        if include_fallback_system and self.system_message and not self.first_send_done:
            items.append({"type": "text", "text": f"{FALLBACK_SYSTEM_PREFIX}{self.system_message}"})
        items.append({"type": "text", "text": text})

        input_op = {"id": new_id(), "op": {"type": "user_input", "items": items}}
        self.proc.stdin.write((jdump(input_op) + "\n").encode())
        await self.proc.stdin.drain()
        self.first_send_done = True

    async def send_text(self, text: str) -> None:
        await self.send_turn_and_text(text, include_fallback_system=False)

    async def stderr(self) -> AsyncIterator[str]:
        if not self.proc or not self.proc.stderr:
            raise RuntimeError(f"{self.name}: process not started")
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            yield line.decode(errors="ignore").rstrip("\n")

    async def events(self) -> AsyncIterator[ProtoEvent]:
        if not self.proc or not self.proc.stdout:
            raise RuntimeError(f"{self.name}: process not started")
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            raw_line = line.decode(errors="ignore").strip()
            if not raw_line:
                continue
            try:
                yield ProtoEvent(self.name, json.loads(raw_line))
            except json.JSONDecodeError:
                continue


class Hub:
    def __init__(
        self,
        codex_path: str = "codex",
        dangerous: bool = True,
        default_cwd: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.codex_path = codex_path
        self.dangerous = dangerous
        self.default_cwd = default_cwd
        self.model = model

        extra: List[str] = ["-c", f"model={json.dumps(model)}"] if model else []
        self.orch = ProtoChild(
            name="orchestrator",
            codex_path=codex_path,
            cwd=default_cwd,
            dangerous=dangerous,
            system_message=ORCHESTRATOR_SYSTEM,
            extra_args=extra,
        )

        self.subs: Dict[str, ProtoChild] = {}
        self.tasks: List[asyncio.Task] = []
        self._stopping = False
        self._subscribers: Set[asyncio.Queue] = set()
        self._sequence = 0
        self.agent_state: Dict[str, str] = {"orchestrator": "idle"}
        self._stderr_buf: Dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=500))
        self.autopilot_enabled: bool = True
        self._autopilot_warned: bool = False

    async def start(self, seed_text: str) -> None:
        await self.orch.start()
        await self.orch.send_turn_and_text(
            "HUB: Ready. You may emit CONTROL blocks to spawn or message sub-agents.\n\n"
            f"Seed context:\n{seed_text}\n",
            include_fallback_system=True,
        )
        self.tasks.append(asyncio.create_task(self._pump(self.orch)))
        self.tasks.append(asyncio.create_task(self._pump_stderr(self.orch)))
        await self._broadcast({"who": "orchestrator", "type": "agent_added", "payload": {"agent": "orchestrator"}})
        await self._broadcast({"who": "orchestrator", "type": "agent_state", "payload": {"agent": "orchestrator", "state": "idle"}})
        await self._broadcast({"who": "hub", "type": "autopilot_state", "payload": {"enabled": self.autopilot_enabled}})

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for task in self.tasks:
            task.cancel()
        await self.orch.stop()
        for agent in list(self.subs.values()):
            await agent.stop()

    async def set_autopilot(self, enabled: bool) -> None:
        if self.autopilot_enabled == enabled:
            return
        self.autopilot_enabled = enabled
        self._autopilot_warned = False
        await self._broadcast({"who": "hub", "type": "autopilot_state", "payload": {"enabled": enabled}})
        state_text = "enabled" if enabled else "disabled"
        try:
            await self.orch.send_text(f"HUB: autopilot {state_text} by human controller.")
        except Exception:
            pass

    async def _set_state(self, agent: str, state: str) -> None:
        prev = self.agent_state.get(agent)
        if prev == state:
            return
        self.agent_state[agent] = state
        await self._broadcast({"who": agent, "type": "agent_state", "payload": {"agent": agent, "state": state}})

    async def _pump(self, child: ProtoChild) -> None:
        try:
            async for event in child.events():
                await self._handle(event)
        except asyncio.CancelledError:
            return

    async def _pump_stderr(self, child: ProtoChild) -> None:
        try:
            async for line in child.stderr():
                self._stderr_buf[child.name].append(line)
                await self._broadcast({"who": child.name, "type": "agent_stderr", "payload": {"line": line}})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._set_state(child.name, "error")
            await self._broadcast({"who": child.name, "type": "error", "payload": {"message": f"stderr pump failed: {exc}"}})

    async def _handle(self, event: ProtoEvent) -> None:
        msg = event.raw.get("msg") or {}
        msg_type = msg.get("type")

        if msg_type == "task_started":
            await self._broadcast({"who": event.who, "type": "task_started", "payload": {"text": msg.get("message") or msg.get("content") or ""}})
            await self._set_state(event.who, "working")

        elif msg_type == "agent_message":
            text = (msg.get("message") or msg.get("content") or "").strip()
            if event.who == "orchestrator":
                display_text = strip_control_blocks(text)
                if display_text:
                    await self._broadcast({"who": "orchestrator", "type": "orch_to_user", "payload": {"text": display_text}})
                await self._set_state("orchestrator", "idle")
            else:
                await self._broadcast({"who": event.who, "type": "agent_to_orch", "payload": {"text": text}})

        elif msg_type == "error":
            await self._broadcast({"who": event.who, "type": "error", "payload": msg})
            await self._set_state(event.who, "error")

        elif msg_type == "task_complete":
            await self._set_state(event.who, "idle")

        if event.who == "orchestrator":
            await self._handle_orch(msg_type, msg)
        else:
            await self._handle_sub(event.who, msg_type, msg)

        if msg_type in {"exec_approval_request", "apply_patch_approval_request"}:
            kind = "exec" if msg_type.startswith("exec") else "patch"
            await self._autoapprove(event.who, msg, kind=kind)

    async def _handle_orch(self, msg_type: Optional[str], msg: Dict[str, Any]) -> None:
        if msg_type != "agent_message":
            return
        text = msg.get("message") or msg.get("content") or ""
        for block in extract_control_blocks(text):
            if not self.autopilot_enabled:
                summary = next(iter(block), "unknown")
                await self._broadcast(
                    {
                        "who": "orchestrator",
                        "type": "autopilot_suppressed",
                        "payload": {"summary": summary, "control": block},
                    }
                )
                if not self._autopilot_warned:
                    try:
                        await self.orch.send_text(
                            "HUB: autopilot is currently disabled; ignoring control blocks. "
                            "Use :autopilot on to allow automated actions."
                        )
                    except Exception:
                        pass
                    self._autopilot_warned = True
                continue
            if "spawn" in block:
                spec = block["spawn"]
                name = spec.get("name")
                task_text = spec.get("task") or ""
                await self._broadcast({"who": "orchestrator", "type": "orch_to_agent", "payload": {"action": "spawn", "agent": name, "text": task_text}})
                await self.spawn_sub(name, task_text, spec.get("cwd") or self.default_cwd)

            elif "send" in block:
                spec = block["send"]
                name = spec.get("to")
                task_text = spec.get("task") or ""
                await self._broadcast({"who": "orchestrator", "type": "orch_to_agent", "payload": {"action": "send", "agent": name, "text": task_text}})
                await self.send_to_sub(name, task_text)

            elif "close" in block:
                spec = block["close"]
                name = spec.get("agent")
                await self._broadcast({"who": "orchestrator", "type": "orch_to_agent", "payload": {"action": "close", "agent": name, "text": spec.get("reason") or ""}})
                await self.close_sub(name)

    async def _handle_sub(self, name: str, msg_type: Optional[str], msg: Dict[str, Any]) -> None:
        if msg_type == "agent_message":
            text = msg.get("message") or msg.get("content") or ""
            if text:
                await self.orch.send_text(f"Update from sub-agent '{name}':\n{text}")
        elif msg_type == "task_complete":
            final = msg.get("last_agent_message") or ""
            await self.orch.send_text(
                f"Sub-agent '{name}' reports task complete.\n"
                f"Final update:\n{final}\n"
                "To continue, emit CONTROL `send` or close with CONTROL `close`."
            )
            await self._set_state(name, "idle")
        elif msg_type == "error":
            err = msg.get("message") or "unknown error"
            await self.orch.send_text(f"Sub-agent '{name}' error: {err}")
            await self._set_state(name, "error")

    async def _autoapprove(self, who: str, msg: Dict[str, Any], kind: str) -> None:
        child = self.orch if who == "orchestrator" else self.subs.get(who)
        if not child or not child.proc or not child.proc.stdin:
            return
        call_id = msg.get("call_id") or msg.get("id")
        op_type = "exec_approval" if kind == "exec" else "patch_approval"
        approved = self.dangerous and self.autopilot_enabled
        reason = "Auto-approved by hub" if approved else "Autopilot disabled"
        if not self.dangerous and self.autopilot_enabled:
            reason = "Dangerous mode disabled"
        approval = {
            "id": new_id(),
            "op": {
                "type": op_type,
                "call_id": call_id,
                "id": call_id,
                "approved": approved,
                "reason": reason,
                "decision": "approved" if approved else "denied",
            },
        }
        child.proc.stdin.write((jdump(approval) + "\n").encode())
        await child.proc.stdin.drain()

    async def spawn_sub(self, name: Optional[str], task_text: str, cwd: Optional[str]) -> None:
        if not name:
            await self.orch.send_text("HUB: spawn missing 'name'.")
            return
        if name in self.subs:
            await self.orch.send_text(f"HUB: sub-agent '{name}' already exists.")
            return

        sys_message = SUBAGENT_SYSTEM_TEMPLATE.format(name=name)
        extra = ["-c", f"model={json.dumps(self.model)}"] if self.model else []

        agent = ProtoChild(
            name=name,
            codex_path=self.codex_path,
            cwd=cwd or self.default_cwd,
            dangerous=self.dangerous,
            system_message=sys_message,
            extra_args=extra,
        )
        await agent.start()
        self.subs[name] = agent
        await agent.send_turn_and_text(task_text, include_fallback_system=True)
        self.tasks.append(asyncio.create_task(self._pump(agent)))
        self.tasks.append(asyncio.create_task(self._pump_stderr(agent)))
        await self.orch.send_text(f"HUB: spawned sub-agent '{name}'.")
        await self._broadcast({"who": name, "type": "agent_added", "payload": {"agent": name}})
        await self._set_state(name, "idle")

    async def send_to_sub(self, name: Optional[str], task_text: str) -> None:
        agent = self.subs.get(name or "")
        if not agent:
            await self.orch.send_text(f"HUB: no such sub-agent '{name}'.")
            return
        await agent.send_text(task_text)
        await self.orch.send_text(f"HUB: forwarded instruction to '{name}'.")

    async def close_sub(self, name: Optional[str]) -> None:
        agent = self.subs.pop(name or "", None)
        if not agent:
            await self.orch.send_text(f"HUB: no such sub-agent '{name}'.")
            return
        await agent.stop()
        self.agent_state.pop(name, None)
        self._stderr_buf.pop(name, None)
        await self.orch.send_text(f"HUB: closed sub-agent '{name}'.")
        await self._broadcast({"who": name, "type": "agent_removed", "payload": {"agent": name}})

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        dead: List[asyncio.Queue] = []
        self._sequence += 1
        event = dict(payload)
        event["seq"] = self._sequence
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


def install_signal_handlers(loop: asyncio.AbstractEventLoop, set_event: asyncio.Event) -> None:
    def _handler(*_: Any) -> None:
        set_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass
