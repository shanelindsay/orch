#!/usr/bin/env python3
"""Core orchestration logic for the Codex CLI hub (app-server rewrite)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from app_server_client import AppServerProcess

ORCHESTRATOR_SYSTEM = (
    "You are the ORCHESTRATOR agent.\n"
    "Plan work, spin up named sub-agents (new conversations), and iterate until goals are met.\n"
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
    "You are a SUB-AGENT named '{name}'.\n"
    "Follow the task from the human operator. Provide succinct progress updates and, when finished,\n"
    "give a short summary and suggested next actions."
)

FALLBACK_SYSTEM_PREFIX = "### SYSTEM MESSAGE (treat as system role) ###\n"
CONTROL_BLOCK_RE = re.compile(
    r"```(?:json\\s+)?control\\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE
)

TEXT_ITEM_TYPES = {"text", "assistant_delta", "assistant_message"}
ASSISTANT_METHODS = {
    "assistant_message",
    "agent_message",
    "response",
    "assistant_output",
}
TASK_STARTED_METHODS = {"task_started", "status", "progress_started"}
TASK_COMPLETE_METHODS = {"task_complete", "progress_complete"}
EXEC_APPROVAL_METHODS = {"exec_approval_request", "execute_approval_request"}
PATCH_APPROVAL_METHODS = {"apply_patch_approval_request", "patch_approval_request"}


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


def normalise_agent_name(name: Optional[str]) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return token or "agent"


@dataclass
class Agent:
    name: str
    conversation_id: str
    state: str = "idle"


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

        self.app = AppServerProcess(codex_bin=codex_path, cwd=default_cwd or os.getcwd())
        self.orchestrator: Optional[Agent] = None
        self.subs: Dict[str, Agent] = {}
        self._conv_to_name: Dict[str, str] = {}

        self.tasks: List[asyncio.Task] = []
        self._stopping = False
        self._subscribers: Set[asyncio.Queue] = set()
        self._sequence = 0
        self.agent_state: Dict[str, str] = {}
        self._stderr_buf: Dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=500))
        self._stderr_buf["app-server"]  # ensure key exists
        self._stderr_buf["orchestrator"]
        self.autopilot_enabled: bool = True
        self._autopilot_warned: bool = False

    async def start(self, seed_text: str) -> None:
        await self.app.start()
        await self.app.initialize(name="orch", version="0.2.0", user_agent_suffix="orch/0.2.0")
        initial = [
            {"type": "text", "text": f"{FALLBACK_SYSTEM_PREFIX}{ORCHESTRATOR_SYSTEM}"},
            {
                "type": "text",
                "text": (
                    "HUB: Ready. You may emit CONTROL blocks to spawn or message sub-agents.\n\n"
                    f"Seed context:\n{seed_text}\n"
                ),
            },
        ]
        workspace = self.default_cwd or os.getcwd()
        conv_id = await self.app.create_conversation(
            workspace=workspace,
            model=self.model,
            initial_messages=initial,
        )
        self.orchestrator = Agent(name="orchestrator", conversation_id=conv_id, state="idle")
        self.agent_state["orchestrator"] = "idle"

        self.tasks.append(asyncio.create_task(self._pump_app_events(), name="hub-events"))

        await self._broadcast(
            {"who": "orchestrator", "type": "agent_added", "payload": {"agent": "orchestrator"}}
        )
        await self._broadcast(
            {"who": "orchestrator", "type": "agent_state", "payload": {"agent": "orchestrator", "state": "idle"}}
        )
        await self._broadcast(
            {"who": "hub", "type": "autopilot_state", "payload": {"enabled": self.autopilot_enabled}}
        )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for task in self.tasks:
            task.cancel()
        await self.app.stop()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def set_autopilot(self, enabled: bool) -> None:
        if self.autopilot_enabled == enabled:
            return
        self.autopilot_enabled = enabled
        self._autopilot_warned = False
        await self._broadcast({"who": "hub", "type": "autopilot_state", "payload": {"enabled": enabled}})
        state_text = "enabled" if enabled else "disabled"
        try:
            await self._send_orch(f"HUB: autopilot {state_text} by human controller.")
        except Exception:
            pass

    async def _pump_app_events(self) -> None:
        try:
            async for event in self.app.events():
                kind = event.get("kind")
                if kind == "notification":
                    method = (event.get("method") or "").lower()
                    params = event.get("params") or {}
                    await self._handle_notification(method, params)
                elif kind == "stderr":
                    line = event.get("line", "")
                    self._stderr_buf["app-server"].append(line)
                    await self._broadcast({"who": "app-server", "type": "agent_stderr", "payload": {"line": line}})
                elif kind == "error":
                    await self._broadcast({"who": "app-server", "type": "error", "payload": event.get("payload", {})})
                else:
                    continue
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._broadcast(
                {"who": "app-server", "type": "error", "payload": {"message": f"event pump failed: {exc}"}}
            )

    async def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        low = method.lower()
        if low in {"session_configured", "sessionconfigured"}:
            await self._broadcast({"who": "app-server", "type": "info", "payload": {"message": "session configured", "raw": params}})
            return

        if low in ASSISTANT_METHODS:
            await self._handle_assistant_message(params)
            return

        if low in TASK_STARTED_METHODS:
            target = self._name_for_params(params) or "agent"
            message = params.get("message") or params.get("status") or "Working"
            await self._broadcast({"who": target, "type": "task_started", "payload": {"text": message}})
            await self._set_state(target, "working")
            return

        if low in TASK_COMPLETE_METHODS:
            target = self._name_for_params(params) or "agent"
            await self._set_state(target, "idle")
            final = params.get("message") or params.get("last_agent_message") or ""
            if target != "orchestrator" and final:
                await self._handle_sub_complete(target, final)
            return

        if low in EXEC_APPROVAL_METHODS:
            await self._autoapprove(params, kind="exec")
            return

        if low in PATCH_APPROVAL_METHODS:
            await self._autoapprove(params, kind="patch")
            return

        if low == "error":
            await self._broadcast({"who": self._name_for_params(params) or "app-server", "type": "error", "payload": params})
            return

        await self._broadcast({"who": "app-server", "type": "misc", "payload": {"method": method, "params": params}})

    async def _handle_assistant_message(self, params: Dict[str, Any]) -> None:
        text = self._extract_text(params)
        if not text:
            return
        conv_id = str(params.get("conversation_id") or params.get("session_id") or "")
        if self.orchestrator and conv_id == self.orchestrator.conversation_id:
            await self._handle_orchestrator_text(text)
            await self._set_state("orchestrator", "idle")
            return
        agent_name = self._conv_to_name.get(conv_id)
        if agent_name:
            await self._broadcast({"who": agent_name, "type": "agent_to_orch", "payload": {"text": text}})
        else:
            await self._broadcast({"who": "agent", "type": "agent_to_orch", "payload": {"text": text}})

    def _extract_text(self, params: Dict[str, Any]) -> Optional[str]:
        if isinstance(params.get("text"), str):
            return params["text"]
        items = params.get("items") or params.get("deltas") or []
        parts: List[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("type") in TEXT_ITEM_TYPES:
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None

    def _name_for_params(self, params: Dict[str, Any]) -> Optional[str]:
        conv_id = str(params.get("conversation_id") or params.get("session_id") or "")
        if not conv_id:
            return None
        if self.orchestrator and conv_id == self.orchestrator.conversation_id:
            return "orchestrator"
        return self._conv_to_name.get(conv_id)

    async def _handle_orchestrator_text(self, text: str) -> None:
        blocks = extract_control_blocks(text)
        display_text = strip_control_blocks(text)
        if display_text:
            await self._broadcast({"who": "orchestrator", "type": "orch_to_user", "payload": {"text": display_text}})
        if not blocks:
            return
        for block in blocks:
            await self._handle_control_block(block)

    async def _handle_control_block(self, block: Dict[str, Any]) -> None:
        if not self.autopilot_enabled:
            summary = next(iter(block), "control")
            await self._broadcast(
                {
                    "who": "orchestrator",
                    "type": "autopilot_suppressed",
                    "payload": {"summary": summary, "control": block},
                }
            )
            if not self._autopilot_warned:
                await self._send_orch(
                    "HUB: autopilot is currently disabled; ignoring control blocks. Use :autopilot on to allow automated actions."
                )
                self._autopilot_warned = True
            return

        if "spawn" in block:
            spec = block["spawn"]
            name = spec.get("name")
            task = spec.get("task") or ""
            cwd = spec.get("cwd") or self.default_cwd
            await self._broadcast(
                {
                    "who": "orchestrator",
                    "type": "orch_to_agent",
                    "payload": {"action": "spawn", "agent": name, "text": task},
                }
            )
            await self.spawn_sub(name, task, cwd)
            return

        if "send" in block:
            spec = block["send"]
            name = spec.get("to")
            task = spec.get("task") or ""
            await self._broadcast(
                {
                    "who": "orchestrator",
                    "type": "orch_to_agent",
                    "payload": {"action": "send", "agent": name, "text": task},
                }
            )
            await self.send_to_sub(name, task)
            return

        if "close" in block:
            spec = block["close"]
            name = spec.get("agent")
            await self._broadcast(
                {
                    "who": "orchestrator",
                    "type": "orch_to_agent",
                    "payload": {"action": "close", "agent": name, "text": spec.get("reason") or ""},
                }
            )
            await self.close_sub(name)
            return

    async def _handle_sub_complete(self, name: str, final: str) -> None:
        await self._send_orch(
            f"Sub-agent '{name}' reports task complete.\n"
            f"Final update:\n{final}\n"
            "To continue, emit CONTROL `send` or close with CONTROL `close`."
        )

    async def _autoapprove(self, params: Dict[str, Any], kind: str) -> None:
        approved = self.dangerous and self.autopilot_enabled
        reason = "Auto-approved by hub" if approved else "Autopilot disabled"
        if not self.dangerous and self.autopilot_enabled:
            reason = "Dangerous mode disabled"
        call_id = params.get("call_id") or params.get("id") or params.get("request_id")
        payload = {
            "call_id": call_id,
            "id": call_id,
            "approved": approved,
            "reason": reason,
            "decision": "approved" if approved else "denied",
        }
        method = "exec_approval" if kind == "exec" else "patch_approval"
        try:
            await self.app.call(method, params=payload, timeout=10.0)
        except Exception:
            pass

    async def send_to_orchestrator(self, text: str) -> None:
        await self._broadcast({"who": "user", "type": "user_to_orch", "payload": {"text": text}})
        await self._send_orch(text)

    async def _send_orch(self, text: str) -> None:
        if not self.orchestrator:
            return
        await self.app.send_message(self.orchestrator.conversation_id, items=[{"type": "text", "text": text}])

    async def spawn_sub(self, name: Optional[str], task_text: str, cwd: Optional[str]) -> None:
        if not name:
            await self._send_orch("HUB: spawn missing 'name'.")
            return
        key = normalise_agent_name(name)
        if key in self.subs:
            await self.app.send_message(
                self.subs[key].conversation_id,
                items=[{"type": "text", "text": task_text}],
            )
            await self._send_orch(f"HUB: sub-agent '{key}' already exists; forwarded new task.")
            return
        sys_message = SUBAGENT_SYSTEM_TEMPLATE.format(name=key)
        initial = [
            {"type": "text", "text": f"{FALLBACK_SYSTEM_PREFIX}{sys_message}"},
            {"type": "text", "text": task_text},
        ]
        conv_id = await self.app.create_conversation(
            workspace=cwd or self.default_cwd or os.getcwd(),
            model=self.model,
            initial_messages=initial,
        )
        agent = Agent(name=key, conversation_id=conv_id, state="idle")
        self.subs[key] = agent
        self._conv_to_name[conv_id] = key
        self.agent_state[key] = "idle"
        await self._broadcast({"who": key, "type": "agent_added", "payload": {"agent": key}})
        await self._broadcast({"who": key, "type": "agent_state", "payload": {"agent": key, "state": "idle"}})
        await self._send_orch(f"HUB: spawned sub-agent '{key}'.")

    async def send_to_sub(self, name: Optional[str], task_text: str) -> None:
        key = normalise_agent_name(name)
        agent = self.subs.get(key)
        if not agent:
            await self._send_orch(f"HUB: no such sub-agent '{name}'.")
            return
        await self.app.send_message(agent.conversation_id, items=[{"type": "text", "text": task_text}])
        await self._send_orch(f"HUB: forwarded instruction to '{key}'.")

    async def close_sub(self, name: Optional[str]) -> None:
        key = normalise_agent_name(name)
        agent = self.subs.pop(key, None)
        if not agent:
            await self._send_orch(f"HUB: no such sub-agent '{name}'.")
            return
        self.agent_state.pop(key, None)
        self._stderr_buf.pop(key, None)
        self._conv_to_name.pop(agent.conversation_id, None)
        await self._broadcast({"who": key, "type": "agent_removed", "payload": {"agent": key}})
        await self._send_orch(f"HUB: closed sub-agent '{key}'.")

    async def _set_state(self, agent: str, state: str) -> None:
        prev = self.agent_state.get(agent)
        if prev == state:
            return
        self.agent_state[agent] = state
        await self._broadcast({"who": agent, "type": "agent_state", "payload": {"agent": agent, "state": state}})

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


def install_signal_handlers(loop: asyncio.AbstractEventLoop, set_event: asyncio.Event) -> None:
    def _handler(*_: Any) -> None:
        set_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass
