from __future__ import annotations

import asyncio
import itertools
import json
import os
import subprocess
from typing import Any, AsyncIterator, Dict, Optional


def _supports_stdio(binary: str) -> bool:
    try:
        result = subprocess.run(
            [binary, "app-server", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    combined = (result.stdout or "") + (result.stderr or "")
    return "--stdio" in combined


class AppServerProcess:
    """Run `codex app-server` and provide a small async client over stdio."""

    def __init__(self, codex_bin: str = "codex", cwd: Optional[str] = None) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd or os.getcwd()
        self._stdio_supported: Optional[bool] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._id_iter = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._pump_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._stdio_supported = _supports_stdio(self.codex_bin)
        if not self._stdio_supported:
            raise RuntimeError(
                "Your 'codex app-server' does not support --stdio. "
                "Update your Codex build to a version that supports stdio transport."
            )

        args = [self.codex_bin, "app-server", "--stdio"]
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1 << 16,
        )
        self._pump_tasks = [
            asyncio.create_task(self._pump_stdout(), name="app-server-stdout"),
            asyncio.create_task(self._pump_stderr(), name="app-server-stderr"),
        ]

    async def stop(self) -> None:
        if self.proc:
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
        for task in self._pump_tasks:
            task.cancel()

    async def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                raw = line.decode(errors="ignore").strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._events.put({"kind": "unknown", "payload": raw})
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    future = self._pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                    else:
                        await self._events.put({"kind": "response", **msg})
                elif "method" in msg:
                    await self._events.put(
                        {
                            "kind": "notification",
                            "method": str(msg.get("method")),
                            "params": msg.get("params") or {},
                        }
                    )
                else:
                    await self._events.put({"kind": "unknown", "payload": msg})
        except asyncio.CancelledError:
            return

    async def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="ignore").rstrip("\n")
                await self._events.put({"kind": "stderr", "line": text})
        except asyncio.CancelledError:
            return

    async def events(self) -> AsyncIterator[dict]:
        while True:
            ev = await self._events.get()
            if ev is None:
                break
            yield ev

    async def call(self, method: str, params: Optional[dict] = None, timeout: float = 60.0) -> dict:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("app-server not started")
        rid = next(self._id_iter)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params or {},
        }
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def initialize(
        self,
        name: str,
        version: str,
        user_agent_suffix: Optional[str] = None,
    ) -> None:
        suffix = user_agent_suffix or f"{name}/{version}"
        params = {"client_info": {"name": name, "version": version, "user_agent_suffix": suffix}}
        await self.call("initialize", params=params, timeout=30.0)

    async def create_conversation(
        self,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
        initial_messages: Optional[list] = None,
    ) -> str:
        params: dict[str, Any] = {}
        if workspace:
            params["workspace_path"] = workspace
        if model:
            params["model"] = model
        if initial_messages:
            params["initial_messages"] = initial_messages
        resp = await self.call("create_conversation", params=params, timeout=30.0)
        payload = resp.get("result") or {}
        for key in ("conversation_id", "session_id", "id"):
            if key in payload:
                return str(payload[key])
        raise RuntimeError(f"create_conversation unexpected result: {resp}")

    async def send_message(self, conversation_id: str, items: list[dict]) -> dict:
        params = {
            "conversation_id": conversation_id,
            "session_id": conversation_id,
            "items": items,
        }
        return await self.call("send_message", params=params, timeout=600.0)
