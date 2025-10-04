#!/usr/bin/env python3
"""Core orchestration logic for the Codex CLI hub (app-server rewrite)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from app_server_client import AppServerProcess
from local_exec import run_exec
from otel_tailer import OTELJsonlTailer

import artifacts

try:
    import github_sync as ghx  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    ghx = None

ORCHESTRATOR_SYSTEM = (
    "You are the ORCHESTRATOR agent.\n"
    "Plan work, spin up named sub-agents (new conversations), and iterate until goals are met.\n"
    "Treat GitHub Issues as charters: respect Goal, Acceptance, Scope, Validation.\n"
    "When autopilot is enabled, you may emit CONTROL blocks to spawn/send/close agents.\n"
    "Use small steps, keep progress concise, and request check-ins from sub-agents.\n"
    "Parallelise ready tasks within WIP limits; sequence tasks that have blockers.\n"
    "On completion, require sub-agents to map work to the Issue's Acceptance checklist and open PRs.\n"
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
    "You work in the given workspace, creating branches and small, testable commits.\n"
    "Follow the task from the human operator. Provide succinct progress updates and, when finished,\n"
    "give a short summary and suggested next actions. If changes are code-related, open a PR referencing the Issue.\n"
    "Always run tests if present. Provide check-ins with the next small step."
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
    last_checkin_ts: float = 0.0
    last_artifact_id: Optional[str] = None
    last_summary: Optional[str] = None


@dataclass
class AgentMeta:
    issue_number: Optional[int] = None
    epic: Optional[int] = None
    started_at: float = 0.0
    last_event_at: float = 0.0
    checkin_seconds: int = 600
    budget_seconds: int = 2700
    nudges_sent: int = 0
    max_nudges: int = 2
    status_comment_id: Optional[int] = None
    workspace: Optional[str] = None
    closing_after_budget: bool = False


class Hub:
    def __init__(
        self,
        codex_path: str = "codex",
        dangerous: bool = True,
        default_cwd: Optional[str] = None,
        model: Optional[str] = None,
        wip_limit: int = 3,
        default_checkin: str = "10m",
        default_budget: str = "45m",
        otel_log_path: Optional[str] = None,
        github_poll: bool = True,
    ) -> None:
        self.codex_path = codex_path
        self.dangerous = dangerous
        self.default_cwd = default_cwd
        self.model = model
        self.repo_path = default_cwd or os.getcwd()
        self.wip_limit = max(0, int(wip_limit))
        self.default_checkin_seconds = self._parse_duration(default_checkin, 600)
        self.default_budget_seconds = self._parse_duration(default_budget, 45 * 60)
        self.otel_log_path = otel_log_path
        self.github_poll = github_poll

        self.app = AppServerProcess(
            codex_bin=codex_path,
            cwd=self.repo_path,
            dangerous=dangerous,
        )
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
        self.decide_debounce_s = 3.0
        self._orch_dirty: Set[str] = set()
        self._orch_extra_blocks: List[Dict[str, Any]] = []
        self._orch_last_sent = 0.0
        self._decision_log: deque[Dict[str, Any]] = deque(maxlen=100)
        self.last_checkin: Dict[str, int] = {}
        self._digest_timer: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._event_log: deque[str] = deque(maxlen=500)
        self.agent_meta: Dict[str, AgentMeta] = {}
        self.issue_to_agent: Dict[int, str] = {}
        self._status_cache: Dict[int, int] = {}
        state_dir = os.path.join(self.repo_path, ".orch")
        os.makedirs(state_dir, exist_ok=True)
        self._state_file = os.path.join(state_dir, "state.jsonl")

    async def start(self, seed_text: str) -> None:
        await self.app.start()
        await self.app.initialize(name="orch", version="0.2.0", user_agent_suffix="orch/0.2.0")
        self.agent_state["app-server"] = "running"
        await self._broadcast(
            {"who": "app-server", "type": "agent_added", "payload": {"agent": "app-server"}}
        )
        await self._broadcast(
            {
                "who": "app-server",
                "type": "agent_state",
                "payload": {"agent": "app-server", "state": "running"},
            }
        )
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
        conv_id = await self.app.create_conversation(
            workspace=self.repo_path,
            model=self.model,
            initial_messages=initial,
        )
        self.orchestrator = Agent(name="orchestrator", conversation_id=conv_id, state="idle")
        self.agent_state["orchestrator"] = "idle"

        self.tasks.append(asyncio.create_task(self._pump_app_events(), name="hub-events"))
        self.tasks.append(asyncio.create_task(self._scheduler(), name="hub-scheduler"))
        watchdog = asyncio.create_task(self._watchdog_loop(), name="hub-watchdog")
        self.tasks.append(watchdog)
        self._watchdog_task = watchdog
        if self.github_poll and ghx is not None:
            self.tasks.append(asyncio.create_task(self._poll_github(), name="hub-github"))
        if self.otel_log_path:
            self.tasks.append(asyncio.create_task(self._pump_otel(self.otel_log_path), name="hub-otel"))

        await self._broadcast(
            {"who": "orchestrator", "type": "agent_added", "payload": {"agent": "orchestrator"}}
        )
        await self._broadcast(
            {"who": "orchestrator", "type": "agent_state", "payload": {"agent": "orchestrator", "state": "idle"}}
        )
        await self._broadcast(
            {"who": "hub", "type": "autopilot_state", "payload": {"enabled": self.autopilot_enabled}}
        )
        self.last_checkin["app-server"] = -1
        self.last_checkin["orchestrator"] = -1

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for task in self.tasks:
            task.cancel()
        if self._digest_timer:
            self._digest_timer.cancel()
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
                elif kind == "request":
                    method = event.get("method") or ""
                    params = event.get("params") or {}
                    request_id = event.get("id")
                    await self._handle_request(method, params, request_id)
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

    async def _pump_otel(self, path: str) -> None:
        tailer = OTELJsonlTailer(path)
        try:
            async for conv_id, _kind in tailer.events():
                name = self._conv_to_name.get(conv_id)
                if name:
                    meta = self.agent_meta.get(name)
                    if meta:
                        meta.last_event_at = time.time()
        except asyncio.CancelledError:
            pass
        finally:
            tailer.stop()

    async def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        low = method.lower()
        if low in {"session_configured", "sessionconfigured"}:
            await self._broadcast({"who": "app-server", "type": "info", "payload": {"message": "session configured", "raw": params}})
            return

        if low.startswith("codex/event/"):
            await self._handle_codex_event(low, params)
            return

        if low in ASSISTANT_METHODS:
            await self._handle_assistant_message(params)
            return

        if low in TASK_STARTED_METHODS:
            target = self._name_for_params(params) or "agent"
            message = params.get("message") or params.get("status") or "Working"
            await self._broadcast({"who": target, "type": "task_started", "payload": {"text": message}})
            await self._set_state(target, "working")
            meta = self.agent_meta.get(target)
            if meta:
                meta.last_event_at = time.time()
            return

        if low in TASK_COMPLETE_METHODS:
            target = self._name_for_params(params) or "agent"
            await self._set_state(target, "idle")
            final = params.get("message") or params.get("last_agent_message") or ""
            if target != "orchestrator" and final:
                await self._handle_sub_complete(target, final)
            meta = self.agent_meta.get(target)
            if meta:
                meta.last_event_at = time.time()
            return

        if low == "error":
            await self._broadcast({"who": self._name_for_params(params) or "app-server", "type": "error", "payload": params})
            return

        await self._broadcast({"who": "app-server", "type": "misc", "payload": {"method": method, "params": params}})

    async def _handle_request(self, method: str, params: Dict[str, Any], request_id: Any) -> None:
        if request_id is None:
            return
        low = method.lower()
        if low in {"execcommandapproval", "applypatchapproval"}:
            await self._autoapprove(request_id, method, params)
            return
        await self.app.respond_error(request_id, -32601, f"Unhandled request: {method}")

    async def _handle_codex_event(self, method: str, params: Dict[str, Any]) -> None:
        msg = params.get("msg") or {}
        msg_type = (msg.get("type") or "").lower()
        conv_id = str(
            params.get("conversation_id")
            or params.get("conversationId")
            or params.get("session_id")
            or params.get("sessionId")
            or ""
        )

        if msg_type == "agent_message":
            text = self._extract_codex_message_text(msg.get("message"))
            if not text:
                return
            await self._handle_assistant_message({"text": text, "conversation_id": conv_id})
            return

        if msg_type == "task_started":
            target = self._name_for_params({"conversation_id": conv_id}) or "agent"
            message = msg.get("message") or msg.get("status") or "Working"
            await self._broadcast({"who": target, "type": "task_started", "payload": {"text": message}})
            await self._set_state(target, "working")
            return

        if msg_type == "task_complete":
            target = self._name_for_params({"conversation_id": conv_id}) or "agent"
            await self._set_state(target, "idle")
            final = msg.get("last_agent_message") or msg.get("message") or ""
            if target != "orchestrator" and final:
                await self._handle_sub_complete(target, final)
            return

        if msg_type in {"exec_command_begin", "exec_command_end", "exec_command_output_delta"}:
            target = self._name_for_params({"conversation_id": conv_id}) or "agent"
            summary = msg.get("command") or msg.get("output") or msg_type.replace("_", " ")
            await self._broadcast({"who": target, "type": "status", "payload": {"text": str(summary)}})
            return

        if msg_type in {"token_count", "agent_reasoning", "agent_reasoning_delta", "agent_reasoning_section_break"}:
            return

        await self._broadcast({"who": "app-server", "type": "misc", "payload": {"method": method, "params": params}})

    def _extract_codex_message_text(self, message: Any) -> Optional[str]:
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            text = message.get("text")
            if isinstance(text, str):
                return text
            content = message.get("content")
            if isinstance(content, list):
                parts = [
                    item.get("text")
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                return "\n".join(parts) if parts else None
        if isinstance(message, list):
            parts = []
            for item in message:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts) if parts else None
        return None

    async def _handle_assistant_message(self, params: Dict[str, Any]) -> None:
        text = self._extract_text(params)
        if not text:
            return
        conv_id = str(params.get("conversation_id") or params.get("session_id") or "")
        name_for_heartbeat = self._conv_to_name.get(conv_id)
        if name_for_heartbeat:
            meta = self.agent_meta.get(name_for_heartbeat)
            if meta:
                meta.last_event_at = time.time()
        if self.orchestrator and conv_id == self.orchestrator.conversation_id:
            await self._handle_orchestrator_text(text)
            await self._set_state("orchestrator", "idle")
            return
        agent_name = self._conv_to_name.get(conv_id)
        if agent_name:
            await self._broadcast({"who": agent_name, "type": "agent_to_orch", "payload": {"text": text}})
            root = self.repo_path
            art_id = None
            try:
                art_id = artifacts.store_text(root, "agent_message", text, meta={"agent": agent_name})
            except Exception:
                art_id = None
            agent = self.subs.get(agent_name)
            if agent:
                summary = (text.strip().splitlines() or [""])[0][:300]
                agent.last_artifact_id = art_id
                agent.last_summary = summary or agent.last_summary
                agent.last_checkin_ts = time.time()
            self.last_checkin[agent_name] = 0
            self._mark_dirty(agent_name)
            await self._maybe_send_digest(reason="agent_message")
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

        if "exec" in block:
            if not self.dangerous:
                await self._send_orch("HUB: exec control block ignored because dangerous mode is off.")
                return
            spec = block.get("exec") or {}
            result = run_exec(spec)
            body = (
                f"exec> {result.cmd}\n"
                f"cwd: {result.cwd}\n"
                f"code: {result.code}\n\n"
                f"stdout:\n{result.stdout}\n"
                f"\nstderr:\n{result.stderr}"
            )
            await self._broadcast({"who": "orchestrator", "type": "orch_to_user", "payload": {"text": body}})
            if not result.ok:
                await self._send_orch(f"HUB: exec command failed with exit code {result.code}.")
            return

        if "status" in block:
            spec = block["status"] or {}
            issue = spec.get("issue")
            text_body = (spec.get("text") or "").strip()
            scope = f"issue#{issue}" if issue else "project"
            await self._broadcast({"who": "hub", "type": "status_posted", "payload": {"scope": scope, "text": text_body}})
            if ghx is not None and issue and text_body:
                try:
                    ghx.comment_issue(self.repo_path, int(issue), text_body)
                except Exception:
                    pass
            return

        if "fetch" in block:
            spec = block["fetch"] or {}
            art_id = spec.get("artifact")
            max_chars_value = spec.get("max_chars")
            try:
                max_chars = int(max_chars_value) if max_chars_value is not None else 4000
            except Exception:
                max_chars = 4000
            if art_id:
                root = self.repo_path
                try:
                    body, total = artifacts.load_text(root, art_id, max_chars=max_chars)
                    event = {"type": "ARTIFACT", "id": art_id, "chars": len(body), "total": total, "body": body}
                    note = f"Fetched artifact {art_id} ({len(body)}/{total} chars)"
                except Exception as exc:
                    event = {"type": "ARTIFACT_ERROR", "id": art_id, "error": str(exc)}
                    note = f"Artifact {art_id} not available ({exc})"
                self._orch_extra_blocks.append(event)
                await self._broadcast({"who": "hub", "type": "artifact_note", "payload": {"note": note}})
                self._ensure_digest_timer()
                await self._maybe_send_digest(reason="fetch")
            return

        if "spawn" in block:
            spec = block["spawn"]
            name = spec.get("name")
            task = spec.get("task") or ""
            cwd = spec.get("cwd") or self.default_cwd
            if self.wip_limit and len(self.subs) >= self.wip_limit:
                await self._send_orch(
                    f"HUB: WIP limit {self.wip_limit} reached; please close an agent before spawning '{name}'."
                )
                return
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
        root = self.repo_path
        art_id = None
        try:
            art_id = artifacts.store_text(root, "agent_complete", final, meta={"agent": name})
        except Exception:
            art_id = None
        agent = self.subs.get(name)
        if agent:
            summary = (final.strip().splitlines() or [""])[0][:300]
            agent.last_artifact_id = art_id
            agent.last_summary = summary or agent.last_summary
            agent.last_checkin_ts = time.time()
        self.last_checkin[name] = 0
        self._mark_dirty(name)
        await self._maybe_send_digest(reason="agent_complete")
        await self._send_orch(
            f"Sub-agent '{name}' reports task complete.\n"
            f"Final update:\n{final}\n"
            "To continue, emit CONTROL `send` or close with CONTROL `close`."
        )

    async def _autoapprove(self, request_id: Any, method: str, params: Dict[str, Any]) -> None:
        approved = self.dangerous and self.autopilot_enabled
        decision = "approved" if approved else "denied"

        description: str
        lower = method.lower()
        if lower == "execcommandapproval":
            command = " ".join(params.get("command", [])) if isinstance(params.get("command"), list) else ""
            description = f"exec command {command}".strip()
        elif lower == "applypatchapproval":
            description = "apply patch"
        else:
            description = method

        denial_reason = "autopilot disabled" if not self.autopilot_enabled else "dangerous mode disabled"
        status_text = (
            f"HUB: auto-approved {description}."
            if approved
            else f"HUB: denied {description} because {denial_reason}."
        )

        try:
            await self.app.respond(request_id, {"decision": decision})
        except Exception:
            return

        await self._broadcast({"who": "hub", "type": "status", "payload": {"text": status_text}})

        if not approved:
            try:
                await self._send_orch(
                    f"HUB: denied {description} because {denial_reason}. Enable autopilot or dangerous mode to allow."
                )
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
        workspace = cwd or self.default_cwd or os.getcwd()
        conv_id = await self.app.create_conversation(
            workspace=workspace,
            model=self.model,
            initial_messages=initial,
        )
        agent = Agent(name=key, conversation_id=conv_id, state="idle")
        self.subs[key] = agent
        self._conv_to_name[conv_id] = key
        self.agent_state[key] = "idle"
        self.agent_meta[key] = AgentMeta(
            started_at=time.time(),
            last_event_at=time.time(),
            checkin_seconds=self.default_checkin_seconds,
            budget_seconds=self.default_budget_seconds,
            workspace=workspace,
        )
        await self._broadcast({"who": key, "type": "agent_added", "payload": {"agent": key}})
        await self._broadcast({"who": key, "type": "agent_state", "payload": {"agent": key, "state": "idle"}})
        await self._send_orch(f"HUB: spawned sub-agent '{key}'.")
        self.last_checkin[key] = -1
        self._mark_dirty(key)
        await self._maybe_send_digest(reason="spawn")

    async def send_to_sub(self, name: Optional[str], task_text: str) -> None:
        key = normalise_agent_name(name)
        agent = self.subs.get(key)
        if not agent:
            await self._send_orch(f"HUB: no such sub-agent '{name}'.")
            return
        await self.app.send_message(agent.conversation_id, items=[{"type": "text", "text": task_text}])
        meta = self.agent_meta.get(key)
        if meta:
            meta.last_event_at = time.time()
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
        self.agent_meta.pop(key, None)
        self.last_checkin.pop(key, None)
        to_remove = [issue for issue, holder in self.issue_to_agent.items() if holder == key]
        for issue in to_remove:
            self.issue_to_agent.pop(issue, None)
        await self._broadcast({"who": key, "type": "agent_removed", "payload": {"agent": key}})
        await self._send_orch(f"HUB: closed sub-agent '{key}'.")

    async def _set_state(self, agent: str, state: str) -> None:
        prev = self.agent_state.get(agent)
        if prev == state:
            return
        self.agent_state[agent] = state
        await self._broadcast({"who": agent, "type": "agent_state", "payload": {"agent": agent, "state": state}})
        if agent != "orchestrator":
            self._mark_dirty(agent)
            await self._maybe_send_digest(reason="state_change")

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        dead: List[asyncio.Queue] = []
        self._sequence += 1
        event = dict(payload)
        event["seq"] = self._sequence
        try:
            who = event.get("who") or "?"
            etype = event.get("type") or "?"
            self._event_log.append(f"[{self._sequence:03d}] {who} {etype}")
            with open(self._state_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except Exception:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    def _mark_dirty(self, agent: str) -> None:
        if not agent or agent == "orchestrator":
            return
        self._orch_dirty.add(agent)
        self._ensure_digest_timer()

    def _ensure_digest_timer(self) -> None:
        if self._digest_timer and not self._digest_timer.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._digest_timer = loop.create_task(self._debounced_digest())

    async def _debounced_digest(self) -> None:
        try:
            await asyncio.sleep(self.decide_debounce_s)
            await self._maybe_send_digest(reason="debounce")
        except asyncio.CancelledError:
            pass
        finally:
            self._digest_timer = None

    async def decide_now(self, reason: str = "manual") -> None:
        await self._send_digest(reason=reason, force=True)

    async def _maybe_send_digest(self, reason: str = "debounce") -> None:
        if not self.orchestrator:
            return
        if not self._orch_dirty and not self._orch_extra_blocks:
            return
        now = time.time()
        if not self._orch_last_sent or (now - self._orch_last_sent) >= self.decide_debounce_s:
            await self._send_digest(reason=reason)

    def _build_digest_text(self) -> str:
        lines: List[str] = ["Decision-ready digest:"]
        events: List[Dict[str, Any]] = []
        now = time.time()

        for name in sorted(self._orch_dirty):
            agent = self.subs.get(name)
            state = self.agent_state.get(name, "unknown")
            summary = (agent.last_summary or "").strip() if agent else ""
            artifact_id: Optional[str] = agent.last_artifact_id if agent else None
            last_seconds: Optional[int] = None
            if agent and agent.last_checkin_ts:
                last_seconds = int(max(0, now - agent.last_checkin_ts))
            last_text = f"{last_seconds}s" if last_seconds is not None else "n/a"
            lines.append(f"- {name} [{state}, last check-in {last_text}]")
            if summary:
                lines.append(f'  "{summary}"')
            event: Dict[str, Any] = {"type": "AGENT_UPDATE", "agent": name, "state": state}
            meta = self.agent_meta.get(name)
            if meta and meta.issue_number:
                event["issue"] = meta.issue_number
            if artifact_id:
                event["artifacts"] = {"last_message": artifact_id}
            events.append(event)

        extra_blocks = list(self._orch_extra_blocks)
        self._orch_extra_blocks.clear()

        if len(lines) == 1:
            lines.append("- No agent updates; awaiting check-ins.")

        text = "\n".join(lines)
        for ev in events:
            text += "\n\n```event\n" + json.dumps(ev, ensure_ascii=False) + "\n```"
        for extra in extra_blocks:
            text += "\n\n```event\n" + json.dumps(extra, ensure_ascii=False) + "\n```"
        return text


    async def _send_digest(self, reason: str, force: bool = False) -> None:
        if not self.orchestrator:
            return
        if not force and not self._orch_dirty and not self._orch_extra_blocks:
            return
        if self._digest_timer and not self._digest_timer.done():
            self._digest_timer.cancel()
        text = self._build_digest_text()
        if not text.strip():
            if not force:
                return
            text = "Decision-ready digest: (no updates)"
        await self.app.send_message(self.orchestrator.conversation_id, items=[{"type": "text", "text": text}])
        self._digest_timer = None
        self._orch_last_sent = time.time()
        record = {"ts": int(self._orch_last_sent), "who": "hub", "action": "digest_sent", "reason": reason}
        self._decision_log.append(record)
        await self._broadcast({"who": "hub", "type": "decision", "payload": record})
        self._orch_dirty.clear()

    def recent_decisions(self, count: int = 20) -> List[Dict[str, Any]]:
        if count <= 0:
            return []
        return list(self._decision_log)[-count:]

    async def _watchdog_loop(self) -> None:
        try:
            while not self._stopping:
                await asyncio.sleep(5)
                now = time.time()
                dirty = False
                for name, agent in list(self.subs.items()):
                    if agent.last_checkin_ts:
                        delta = int(max(0, now - agent.last_checkin_ts))
                    else:
                        delta = -1
                    self.last_checkin[name] = delta
                    meta = self.agent_meta.get(name)
                    threshold = meta.checkin_seconds if meta else self.default_checkin_seconds
                    if threshold and delta >= 0 and delta > threshold:
                        self._orch_extra_blocks.append({
                            "type": "TIMEOUT_CHECKIN",
                            "agent": name,
                            "seconds": delta,
                        })
                        dirty = True
                if dirty:
                    await self._maybe_send_digest(reason="watchdog")
        except asyncio.CancelledError:
            return

    async def _scheduler(self) -> None:
        try:
            while not self._stopping:
                now = time.time()
                for name, meta in list(self.agent_meta.items()):
                    self._maybe_update_status_comment(name)
                    if now - meta.last_event_at > meta.checkin_seconds and meta.nudges_sent < meta.max_nudges:
                        await self._nudge_agent(name)
                        meta.nudges_sent += 1
                    if now - meta.started_at > meta.budget_seconds and not meta.closing_after_budget:
                        await self._ask_wrap_up(name)
                        meta.closing_after_budget = True
                    if meta.closing_after_budget and now - meta.last_event_at > 60:
                        await self.close_sub(name)
                await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            return

    async def _poll_github(self) -> None:
        if ghx is None:
            return
        try:
            while not self._stopping:
                try:
                    issues = ghx.list_orchestrate_issues(self.repo_path, limit=50)
                except Exception:
                    issues = []
                closed = {item.number for item in issues if (item.state or "").lower() == "closed"}
                active = set(self.issue_to_agent.keys())
                ready: List[Any] = []
                for issue in issues:
                    if issue.number in active or issue.number in closed:
                        continue
                    blockers = ghx.parse_blockers(issue.body, issue.labels)
                    blockers = [b for b in blockers if b not in closed]
                    if blockers:
                        continue
                    ready.append((issue, blockers))
                capacity = max(0, self.wip_limit - len(self.subs)) if self.wip_limit else len(ready)
                for issue, _ in ready[:capacity]:
                    try:
                        charter = ghx.parse_issue_body(issue.body)
                        prompt = ghx.format_issue_prompt(issue, charter)
                    except Exception:
                        prompt = f"Work on Issue #{issue.number}: {issue.title}"
                    prompt += (
                        "\n\nYou have high permissions and autopilot is enabled. "
                        "Create a small, testable branch or PR. Provide regular check-ins. "
                        "When done, map outcomes to Acceptance and reference this issue."
                    )
                    name = f"iss{issue.number}"
                    await self.spawn_sub(name, prompt, self.default_cwd)
                    meta = self.agent_meta.get(name)
                    if meta:
                        sla = ghx.sla_from_labels(issue.labels)
                        meta.issue_number = issue.number
                        meta.checkin_seconds = int(sla.get("checkin_seconds", self.default_checkin_seconds))
                        meta.budget_seconds = int(sla.get("budget_seconds", self.default_budget_seconds))
                        try:
                            comment_id = ghx.ensure_status_comment(self.repo_path, issue.number)
                            meta.status_comment_id = comment_id
                            self._status_cache[issue.number] = comment_id
                        except Exception:
                            pass
                    self.issue_to_agent[issue.number] = name
                await asyncio.sleep(90.0)
        except asyncio.CancelledError:
            return

    async def _nudge_agent(self, name: str) -> None:
        agent = self.subs.get(name)
        if not agent:
            return
        message = (
            "Quick check-in:\n"
            "- What is the next small step?\n"
            "- Is anything blocking you?\n"
            "- ETA to a minimal PR or result?"
        )
        await self.app.send_message(agent.conversation_id, items=[{"type": "text", "text": message}])

    async def _ask_wrap_up(self, name: str) -> None:
        agent = self.subs.get(name)
        if not agent:
            return
        message = (
            "Time budget reached. Please summarise status, remaining work, and immediate next actions. "
            "If you have a branch or partial PR, share links now."
        )
        await self.app.send_message(agent.conversation_id, items=[{"type": "text", "text": message}])

    def _maybe_update_status_comment(self, name: str) -> None:
        if ghx is None:
            return
        meta = self.agent_meta.get(name)
        if not meta or not meta.issue_number:
            return
        now = time.time()
        if (now - meta.last_event_at) < 180:
            return
        try:
            comment_id = meta.status_comment_id or self._status_cache.get(meta.issue_number) or ghx.ensure_status_comment(
                self.repo_path, meta.issue_number
            )
            meta.status_comment_id = comment_id
            self._status_cache[meta.issue_number] = comment_id
            body = self._render_status_comment(name, meta)
            ghx.update_comment(self.repo_path, comment_id, body)
        except Exception:
            pass

    def _render_status_comment(self, name: str, meta: AgentMeta) -> str:
        marker = "<!-- orch:status -->"

        def fmt_delta(seconds: float) -> str:
            seconds = int(max(0, seconds))
            if seconds >= 3600:
                return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
            return f"{seconds // 60}m"

        now = time.time()
        elapsed = fmt_delta(now - meta.started_at)
        since = fmt_delta(now - meta.last_event_at)
        left = fmt_delta(max(0, meta.budget_seconds - (now - meta.started_at)))
        conversation_id = self.subs.get(name).conversation_id if name in self.subs else ""
        return (
            f"{marker}\n\n"
            f"**Agent**: `{name}`  \n"
            f"**State**: {self.agent_state.get(name, '?')}  \n"
            f"**Elapsed**: {elapsed}  \n"
            f"**Last event**: {since} ago  \n"
            f"**Budget left**: {left}  \n"
            f"**Workspace**: `{meta.workspace or ''}`  \n"
            f"**Conversation**: `{conversation_id}`\n"
            "\n_Update cadence: automated by orch._"
        )

    def render_wip_table(self) -> str:
        if not self.subs:
            return "No active sub-agents."
        now = time.time()
        header = f"{'AGENT':<14} {'STATE':<10} {'ISSUE':<6} {'ELAPSED':<8} {'LAST':<8} {'BUDGET':<8} {'NUDGES':<6}"
        lines = [header, "-" * len(header)]

        def fmt_delta(value: float) -> str:
            value = int(max(0, value))
            if value >= 3600:
                return f"{value // 3600}h"
            return f"{value // 60}m"

        for name in sorted(self.subs.keys()):
            meta = self.agent_meta.get(name) or AgentMeta()
            elapsed = fmt_delta(now - meta.started_at)
            last = fmt_delta(now - meta.last_event_at)
            remaining = fmt_delta(max(0, meta.budget_seconds - (now - meta.started_at)))
            issue = str(meta.issue_number or "-")
            lines.append(
                f"{name:<14} {self.agent_state.get(name, '?'):<10} {issue:<6} {elapsed:<8} {last:<8} {remaining:<8} {meta.nudges_sent}/{meta.max_nudges:<6}"
            )
        return "\n".join(lines)

    def render_recent(self, count: int = 50) -> List[str]:
        if count <= 0:
            return []
        return list(self._event_log)[-count:]

    def render_plan(self) -> str:
        if ghx is None:
            return "GitHub helpers unavailable."
        try:
            issues = ghx.list_orchestrate_issues(self.repo_path, limit=100)
        except Exception as exc:
            return f"GitHub error: {exc}"
        closed = {issue.number for issue in issues if (issue.state or "").lower() == "closed"}
        ready: List[str] = []
        blocked: List[str] = []
        for issue in issues:
            blockers = ghx.parse_blockers(issue.body, issue.labels)
            blockers = [b for b in blockers if b not in closed]
            entry = f"#{issue.number} {issue.title}"
            if blockers:
                blocked.append(f"  - {entry} (blocked by {', '.join('#' + str(b) for b in blockers)})")
            else:
                ready.append(f"  - {entry}")
        lines = ["Ready issues:"]
        lines.extend(ready or ["  (none)"])
        lines.append("Blocked issues:")
        lines.extend(blocked or ["  (none)"])
        return "\n".join(lines)

    def render_issue_summary(self, issue_number: int) -> str:
        if ghx is None:
            return "GitHub helpers unavailable."
        try:
            issue = ghx.fetch_issue(self.repo_path, issue_number)
            charter = ghx.parse_issue_body(issue.body)
            prompt = ghx.format_issue_prompt(issue, charter)
            prs = ghx.fetch_prs_for_issue(self.repo_path, issue_number)
        except Exception as exc:
            return f"GitHub error: {exc}"
        if prs:
            prompt += "\n\nPRs:\n" + "\n".join(
                [
                    f"- #{pr.get('number')} {pr.get('title')} ({pr.get('state')}) {pr.get('url')}"
                    for pr in prs
                ]
            )
        return prompt

    @staticmethod
    def _parse_duration(value: str | int | float | None, default_seconds: int) -> int:
        if value in (None, ""):
            return default_seconds
        text = str(value).strip().lower()
        try:
            if text.endswith("ms"):
                return max(1, int(float(text[:-2]) / 1000.0))
            if text.endswith("s"):
                return int(float(text[:-1]))
            if text.endswith("m"):
                return int(float(text[:-1]) * 60)
            if text.endswith("h"):
                return int(float(text[:-1]) * 3600)
            if text.endswith("d"):
                return int(float(text[:-1]) * 86400)
            return int(float(text))
        except Exception:
            return default_seconds


def install_signal_handlers(loop: asyncio.AbstractEventLoop, set_event: asyncio.Event) -> None:
    def _handler(*_: Any) -> None:
        set_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            pass
