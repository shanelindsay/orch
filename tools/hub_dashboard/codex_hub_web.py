#!/usr/bin/env python3
"""Non-blocking Codex hub with a tiny web dashboard."""

from __future__ import annotations

import argparse
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

from aiohttp import web

DANGEROUS_FLAG = "--dangerously-bypass-approvals-and-sandbox"

ORCHESTRATOR_SYSTEM = """You are the ORCHESTRATOR agent.\nPlan work, but DO NOT emit control blocks unless autopilot is enabled by the human.\nEmit control blocks in replies when you want the hub to act:\n\n```control\n{"spawn":{"name":"<agent_name>","task":"<task text>","cwd":null}}\n```\n\n```control\n{"send":{"to":"<agent_name>","task":"<follow-up instruction>"}}\n```\n\n```control\n{"close":{"agent":"<agent_name>"}}\n```\n\nAlso write normal prose updates for the human.\n"""

SUBAGENT_SYSTEM_TEMPLATE = (
    "You are a SUB-AGENT named \"{name}\".\n"
    "Follow the task from the user. Provide succinct progress updates and, when finished,\n"
    "give a short summary and suggested next actions."
)

FALLBACK_SYSTEM_PREFIX = "### SYSTEM MESSAGE (treat as system role) ###\n"
CONTROL_BLOCK_RE = re.compile(
    r"```(?:json\s+)?control\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def strip_control_blocks(text: str) -> str:
    """Remove ```control``` fences and collapse whitespace for display."""

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

    # Primary: explicit ```control``` fences
    for match in CONTROL_BLOCK_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            blocks.append(payload)

    # Fallback: single-line JSON objects (e.g., {"spawn": ...})
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
        if not any(key in payload for key in ("spawn", "send", "close")):
            continue
        signature = json.dumps(payload, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        blocks.append(payload)

    return blocks


# ---------- Codex proto child ----------


@dataclass
class ProtoEvent:
    who: str
    raw: Dict[str, Any]


@dataclass
class ProtoChild:
    name: str
    codex_path: str = "codex"
    cwd: Optional[str] = None
    dangerous: bool = False
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
                "initial_messages=[{\"role\":\"system\",\"content\":"
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


# ---------- Hub ----------


class Hub:
    def __init__(
        self,
        codex_path: str = "codex",
        dangerous: bool = False,
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
        # Block orchestrator control blocks and auto-approvals unless explicitly enabled
        self.allow_controls: bool = False

    # ----- lifecycle -----

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
        await self._broadcast(
            {
                "who": "orchestrator",
                "type": "agent_state",
                "payload": {"agent": "orchestrator", "state": "idle"},
            }
        )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for task in self.tasks:
            task.cancel()
        await self.orch.stop()
        for agent in list(self.subs.values()):
            await agent.stop()

    # ----- event routing -----

    async def _set_state(self, agent: str, state: str) -> None:
        prev = self.agent_state.get(agent)
        if prev == state:
            return
        self.agent_state[agent] = state
        await self._broadcast(
            {"who": agent, "type": "agent_state", "payload": {"agent": agent, "state": state}}
        )

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
                await self._broadcast(
                    {"who": child.name, "type": "agent_stderr", "payload": {"line": line}}
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            await self._set_state(child.name, "error")
            await self._broadcast(
                {
                    "who": child.name,
                    "type": "error",
                    "payload": {"message": f"stderr pump failed: {exc}"},
                }
            )

    async def _handle(self, event: ProtoEvent) -> None:
        msg = event.raw.get("msg") or {}
        msg_type = msg.get("type")

        if msg_type == "task_started":
            await self._broadcast(
                {
                    "who": event.who,
                    "type": "task_started",
                    "payload": {"text": msg.get("message") or msg.get("content") or ""},
                }
            )
            await self._set_state(event.who, "working")
        elif msg_type == "agent_message":
            text = (msg.get("message") or msg.get("content") or "").strip()
            if event.who == "orchestrator":
                display_text = strip_control_blocks(text)
                if display_text:
                    await self._broadcast(
                        {
                            "who": "orchestrator",
                            "type": "orch_to_user",
                            "payload": {"text": display_text},
                        }
                    )
                await self._set_state("orchestrator", "idle")
            else:
                await self._broadcast(
                    {
                        "who": event.who,
                        "type": "agent_to_orch",
                        "payload": {"text": text},
                    }
                )
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
            if not self.allow_controls:
                # Ignore control blocks when autopilot is disabled
                await self._broadcast(
                    {
                        "who": "orchestrator",
                        "type": "autopilot_suppressed",
                        "payload": {"control": block},
                    }
                )
                continue
            if "spawn" in block:
                spec = block["spawn"]
                name = spec.get("name")
                task_text = spec.get("task") or ""
                await self._broadcast(
                    {
                        "who": "orchestrator",
                        "type": "orch_to_agent",
                        "payload": {
                            "action": "spawn",
                            "agent": name,
                            "text": task_text,
                        },
                    }
                )
                await self.spawn_sub(
                    name,
                    task_text,
                    spec.get("cwd") or self.default_cwd,
                )
            elif "send" in block:
                spec = block["send"]
                name = spec.get("to")
                task_text = spec.get("task") or ""
                await self._broadcast(
                    {
                        "who": "orchestrator",
                        "type": "orch_to_agent",
                        "payload": {
                            "action": "send",
                            "agent": name,
                            "text": task_text,
                        },
                    }
                )
                await self.send_to_sub(name, task_text)
            elif "close" in block:
                spec = block["close"]
                name = spec.get("agent")
                await self._broadcast(
                    {
                        "who": "orchestrator",
                        "type": "orch_to_agent",
                        "payload": {
                            "action": "close",
                            "agent": name,
                            "text": spec.get("reason") or "",
                        },
                    }
                )
                await self.close_sub(name)

    async def _handle_sub(self, name: str, msg_type: Optional[str], msg: Dict[str, Any]) -> None:
        if msg_type == "agent_message":
            text = msg.get("message") or msg.get("content") or ""
            if text:
                await self.orch.send_text(f"Update from sub-agent '{name}':\n{text}")
            # Do not set idle yet; wait for task_complete.
        elif msg_type == "task_complete":
            final = msg.get("last_agent_message") or ""
            await self.orch.send_text(
                f"Sub-agent '{name}' reports task complete.\n"
                f"Final update:\n{final}\n"
                f"To continue, emit CONTROL `send` or close with CONTROL `close`."
            )
            await self._set_state(name, "idle")
        elif msg_type == "error":
            err = msg.get("message") or "unknown error"
            await self.orch.send_text(f"Sub-agent '{name}' error: {err}")
            await self._set_state(name, "error")

    async def _autoapprove(self, who: str, msg: Dict[str, Any], kind: str) -> None:
        if not self.allow_controls:
            # Deny approvals when autopilot is off
            child = self.orch if who == "orchestrator" else self.subs.get(who)
            if not child or not child.proc or not child.proc.stdin:
                return
            call_id = msg.get("call_id") or msg.get("id")
            op_type = "exec_approval" if kind == "exec" else "patch_approval"
            denial = {
                "id": new_id(),
                "op": {
                    "type": op_type,
                    "call_id": call_id,
                    "id": call_id,
                    "approved": False,
                    "reason": "Autopilot disabled",
                    "decision": "denied",
                },
            }
            child.proc.stdin.write((jdump(denial) + "\n").encode())
            await child.proc.stdin.drain()
            return
        child = self.orch if who == "orchestrator" else self.subs.get(who)
        if not child or not child.proc or not child.proc.stdin:
            return

        call_id = msg.get("call_id") or msg.get("id")
        op_type = "exec_approval" if kind == "exec" else "patch_approval"
        approval = {
            "id": new_id(),
            "op": {
                "type": op_type,
                "call_id": call_id,
                "id": call_id,
                "approved": True,
                "reason": "Auto-approved by hub",
                "decision": "approved",
            },
        }
        child.proc.stdin.write((jdump(approval) + "\n").encode())
        await child.proc.stdin.drain()

    # ----- actions -----

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

    # ----- dashboard broadcast -----

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


# ---------- Web app ----------


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Codex Hub</title>
<style>
body{font:14px system-ui,Segoe UI,Roboto,Helvetica,Arial;margin:0;background:#f7f9fc;color:#1f2933}
#bar{display:flex;gap:8px;padding:10px 14px;background:#101727;color:#fff;align-items:center;position:sticky;top:0;z-index:10}
#brand{background:#1f7aec;padding:4px 10px;border-radius:999px;font-weight:600}
#sendForm{flex:1;display:flex;gap:8px;align-items:center;margin:0}
#messageInput{flex:1;border-radius:6px;border:none;padding:8px 10px;font:inherit;background:#fff;color:#111;max-width:720px}
#messageInput:focus{outline:2px solid #3b82f6;outline-offset:1px}
#sendBtn{background:#3b82f6;color:#fff;border:0;padding:8px 14px;border-radius:6px;font-weight:600;cursor:pointer}
#sendBtn:hover{background:#2563eb}
#status{margin-left:8px;font-size:12px;color:#cbd5f5}
#log{padding:16px;display:flex;flex-direction:column;gap:8px}
.entry{background:#fff;border-left:4px solid #1f7aec;border-radius:8px;padding:10px 12px;box-shadow:0 1px 3px rgba(15,23,42,0.08)}
.entry.user{border-color:#8e44ad}
.entry.orch{border-color:#1f7aec}
.entry.agent{border-color:#14b8a6}
.entry.error{border-color:#ef4444;color:#b91c1c}
.entry.info{border-color:#94a3b8;color:#475569}
.entry small{display:block;color:#64748b;font-size:12px;margin-bottom:4px}
</style>
</head>
<body>
<div id="bar">
  <span id="brand">Orchestrator Hub</span>
  <form id="sendForm">
    <input id="messageInput" placeholder="Type a message to the orchestrator…" autocomplete="off" />
    <button id="sendBtn" type="submit">Send</button>
  </form>
  <small id="status">live</small>
</div>
<div id="log"></div>
<script>
(function(){
  function addEntry(text, kind, meta){
    var log = document.getElementById('log');
    if(!log) return;
    var entry = document.createElement('div');
    entry.className = 'entry' + (kind ? ' ' + kind : '');
    if(meta){
      var small = document.createElement('small');
      small.textContent = meta;
      entry.appendChild(small);
    }
    var body = document.createElement('div');
    body.textContent = text;
    entry.appendChild(body);
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
  }

  function sendMsg(){
    var input = document.getElementById('messageInput') || document.getElementById('send');
    if(!input) return;
    var raw = input.value || '';
    var txt = raw.trim();
    if(!txt) return;
    addEntry(txt, 'user', 'You → ORCH');
    fetch(resolvePath('api/say'), {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({text: txt})
    }).then(function(res){
      if(!res.ok){
        addEntry('HTTP ' + res.status, 'error', 'Send failed');
      }
    }).catch(function(err){
      console.error('POST /api/say failed', err);
      addEntry(String(err), 'error', 'Send failed');
    });
    input.value = '';
    input.focus();
  }

  window.sendMsg = sendMsg;

  function resolvePath(path){
    var clean = (path || '').replace(/^\/+/, '');
    var loc = window.location;
    var basePath = loc.pathname.endsWith('/') ? loc.pathname : loc.pathname + '/';
    return new URL(clean, loc.origin + basePath).toString();
  }

  var sendForm = document.getElementById('sendForm');
  var sendBtn = document.getElementById('sendBtn');
  var messageInput = document.getElementById('messageInput') || document.getElementById('send');

  function stopAll(ev){
    if(!ev) return;
    ev.preventDefault();
    ev.stopPropagation();
    if(ev.stopImmediatePropagation){ ev.stopImmediatePropagation(); }
  }

  if(sendForm){
    sendForm.addEventListener('submit', function(ev){
      stopAll(ev);
      sendMsg();
    }, true);
  }

  if(sendBtn){
    sendBtn.addEventListener('click', function(ev){
      stopAll(ev);
      sendMsg();
    }, true);
  }

  if(messageInput){
    messageInput.addEventListener('keydown', function(ev){
      var key = ev.key || String.fromCharCode(ev.keyCode || 0);
      if((key === 'Enter' || ev.keyCode === 13) && !ev.isComposing){
        stopAll(ev);
        sendMsg();
      }
    }, true);
  }

  document.addEventListener('keydown', function(ev){
    var key = (ev.key || '').toLowerCase();
    if((ev.ctrlKey || ev.metaKey) && (key === 'enter' || ev.keyCode === 13)){
      stopAll(ev);
      sendMsg();
    }
  }, true);

  var statusEl = document.getElementById('status');
  try {
    var es = new EventSource(resolvePath('events'));
    es.onmessage = function(ev){
      try{
        var msg = JSON.parse(ev.data);
        handleEvent(msg);
      }catch(err){
        console.error('SSE parse', err);
      }
    };
    es.onerror = function(){
      if(statusEl){
        statusEl.textContent = 'disconnected';
        statusEl.style.color = '#f97316';
      }
    };
  } catch (err){
    console.error('EventSource error', err);
  }

  function handleEvent(msg){
    if(!msg) return;
    var type = msg.type || '';
    var who = msg.who || '';
    var payload = msg.payload || {};
    if(type === 'orch_to_user'){
      addEntry(payload.text || '', 'orch', 'ORCH → You');
    } else if(type === 'agent_to_orch'){
      var label = who ? who + ' → ORCH' : 'Agent → ORCH';
      addEntry(payload.text || '', 'agent', label);
    } else if(type === 'task_started'){
      addEntry(payload.text || '', 'info', (who || 'Agent') + ' started');
    } else if(type === 'agent_state'){
      var agentName = payload.agent || who || 'Agent';
      addEntry(agentName + ' → ' + (payload.state || ''), 'info', 'State change');
    } else if(type === 'error'){
      addEntry(payload.message || '', 'error', who ? ('Error from ' + who) : 'Error');
    } else if(type === 'orch_to_agent'){
      var target = payload.agent || 'agent';
      var actionLabel = payload.action ? ' [' + payload.action + ']' : '';
      addEntry(payload.text || '', 'info', 'ORCH → ' + target + actionLabel);
    } else if(type === 'agent_stderr'){
      return;
    } else if(type === 'user_to_orch'){
      return;
    } else if(type){
      addEntry(JSON.stringify(payload), 'info', type);
    }
  }

  console.log('Hub dashboard script initialized');
})();
</script>
</body>
</html>"""


async def sse(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app["hub"]
    queue = hub.subscribe()

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await response.prepare(request)

    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                try:
                    await response.write(b": ping\n\n")
                except ConnectionResetError:
                    break
                continue

            data = json.dumps(payload, ensure_ascii=False)
            try:
                await response.write(f"data: {data}\n\n".encode())
            except ConnectionResetError:
                break
    except (asyncio.CancelledError, RuntimeError):
        pass
    finally:
        hub.unsubscribe(queue)

    return response


async def say(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    payload = await request.json()
    text = str(payload.get("text") or "")
    if text:
        await hub._broadcast(
            {
                "who": "user",
                "type": "user_to_orch",
                "payload": {"text": text.strip()},
            }
        )
    await hub.orch.send_text(text)
    return web.json_response({"ok": True})


async def agents(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    return web.json_response(
        {
            "orchestrator": "orchestrator",
            "sub_agents": sorted(hub.subs.keys()),
            "states": hub.agent_state,
            "autopilot": hub.allow_controls,
        }
    )


async def autopilot(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    if request.method == "GET":
        return web.json_response({"enabled": hub.allow_controls})
    data = await request.json()
    enabled = bool(data.get("enabled"))
    hub.allow_controls = enabled
    await hub._broadcast({"who": "orchestrator", "type": "autopilot", "payload": {"enabled": enabled}})
    return web.json_response({"ok": True, "enabled": enabled})


async def agent_send(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    payload = await request.json()
    target = str(payload.get("to") or "").strip()
    task = str(payload.get("task") or "").strip()
    if not target or not task:
        return web.json_response({"ok": False, "error": "missing 'to' or 'task'"}, status=400)
    if target == "orchestrator":
        await hub._broadcast(
            {"who": "user", "type": "user_to_orch", "payload": {"text": task}}
        )
        await hub.orch.send_text(task)
        return web.json_response({"ok": True})
    if target not in hub.subs:
        return web.json_response({"ok": False, "error": f"no such sub-agent '{target}'"}, status=404)
    await hub.send_to_sub(target, task)
    return web.json_response({"ok": True})


async def agent_close(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    payload = await request.json()
    agent = str(payload.get("agent") or "").strip()
    if not agent:
        return web.json_response({"ok": False, "error": "missing 'agent'"}, status=400)
    if agent == "orchestrator":
        return web.json_response({"ok": False, "error": "cannot close orchestrator"}, status=400)
    await hub.close_sub(agent)
    return web.json_response({"ok": True})


async def get_stderr(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    agent = request.query.get("agent") or ""
    lines = list(hub._stderr_buf.get(agent, deque()))
    return web.json_response({"agent": agent, "lines": lines})


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


# ---------- CLI ----------


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex hub with live dashboard")
    parser.add_argument(
        "--seed",
        default="You may begin planning tasks.",
        help="Initial brief to the orchestrator",
    )
    parser.add_argument("--cwd", default=None, help="Default working directory for agents")
    parser.add_argument("--codex-path", default="codex", help="Path to the codex binary")
    parser.add_argument(
        "--dangerous",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bypass approvals and sandbox (default on). Use --no-dangerous to disable.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override passed via -c model=...",
    )
    parser.add_argument("--port", type=int, default=8765, help="Dashboard port")
    return parser


async def async_main() -> None:
    parser = build_argparser()
    args = parser.parse_args()

    hub = Hub(
        codex_path=args.codex_path,
        dangerous=bool(args.dangerous),
        default_cwd=args.cwd,
        model=args.model,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(*_: Any) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    await hub.start(seed_text=args.seed)

    app = web.Application()
    app["hub"] = hub
    app.add_routes(
        [
            web.get("/", index),
            web.get("/events", sse),
            web.get("/api/agents", agents),
            web.get("/api/autopilot", autopilot),
            web.post("/api/autopilot", autopilot),
            web.post("/api/say", say),
            web.post("/api/agent/send", agent_send),
            web.post("/api/agent/close", agent_close),
            web.get("/api/agent/stderr", get_stderr),
        ]
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()

    print(f"Dashboard at http://127.0.0.1:{args.port}/", flush=True)

    await stop_event.wait()
    await hub.stop()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
