from __future__ import annotations

import asyncio
import itertools
import json
import os
import subprocess
from typing import Any, AsyncIterator, Dict, Optional


def _supports_app_server(binary: str) -> bool:
    try:
        subprocess.run(
            [binary, "app-server", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception:
        return False
    return True


class AppServerProcess:
    """Run `codex app-server` and provide a small async client over stdio."""

    def __init__(
        self,
        codex_bin: str = "codex",
        cwd: Optional[str] = None,
        dangerous: bool = True,
    ) -> None:
        self.codex_bin = codex_bin
        self.cwd = cwd or os.getcwd()
        self.dangerous = dangerous
        self._app_server_available: Optional[bool] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._events: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
        self._id_iter = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._pump_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._app_server_available = _supports_app_server(self.codex_bin)
        if not self._app_server_available:
            raise RuntimeError(
                "Unable to run 'codex app-server'; ensure the Codex CLI is installed and on PATH."
            )

        args = [self.codex_bin, "app-server"]
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
                elif "id" in msg and "method" in msg:
                    await self._events.put(
                        {
                            "kind": "request",
                            "id": msg.get("id"),
                            "method": str(msg.get("method")),
                            "params": msg.get("params") or {},
                        }
                    )
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
            response = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response

    async def _write_json(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("app-server not started")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line.encode())
        await self.proc.stdin.drain()

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_json(payload)

    async def respond(self, request_id: Any, result: dict) -> None:
        await self._write_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    async def initialize(
        self,
        name: str,
        version: str,
        user_agent_suffix: Optional[str] = None,
    ) -> None:
        client_info: dict[str, Any] = {"name": name, "version": version}
        if user_agent_suffix:
            client_info["title"] = user_agent_suffix
        params = {"clientInfo": client_info}
        await self.call("initialize", params=params, timeout=30.0)
        await self.notify("initialized")

    async def create_conversation(
        self,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
        initial_messages: Optional[list] = None,
    ) -> str:
        params: dict[str, Any] = {}
        if model:
            params["model"] = model
        if workspace:
            params["cwd"] = workspace
        # Always request approvals so the hub can gate actions; sandbox level depends on danger.
        params["approvalPolicy"] = "on-request"
        params["sandbox"] = "danger-full-access" if self.dangerous else "workspace-write"

        resp = await self.call("newConversation", params=params, timeout=30.0)
        payload = resp.get("result") or {}
        conversation_id = payload.get("conversationId")
        if not conversation_id:
            raise RuntimeError(f"newConversation unexpected result: {resp}")

        conv_id = str(conversation_id)

        # Subscribe to live events so the hub receives codex/event notifications.
        try:
            await self.call(
                "addConversationListener",
                params={"conversationId": conv_id},
                timeout=10.0,
            )
        except RuntimeError:
            # Non-fatal; continue even if listener registration fails.
            pass

        if initial_messages:
            await self.send_message(conv_id, initial_messages)

        return conv_id

    async def send_message(self, conversation_id: str, items: list[dict]) -> dict:
        converted_items: list[dict[str, Any]] = []
        for item in items:
            kind = item.get("type")
            if kind == "text":
                converted_items.append(
                    {
                        "type": "text",
                        "data": {"text": item.get("text", "")},
                    }
                )
            elif kind == "image":
                image_url = item.get("imageUrl") or item.get("image_url")
                converted_items.append(
                    {
                        "type": "image",
                        "data": {"imageUrl": image_url},
                    }
                )
            elif kind in {"local_image", "localImage"}:
                converted_items.append(
                    {
                        "type": "localImage",
                        "data": {"path": item.get("path")},
                    }
                )
            else:
                converted_items.append(dict(item))

        params = {
            "conversationId": conversation_id,
            "items": converted_items,
        }
        return await self.call("sendUserMessage", params=params, timeout=600.0)
