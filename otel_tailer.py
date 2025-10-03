from __future__ import annotations

import asyncio
import io
import json
import os
from typing import AsyncIterator, Dict, Optional


def _dig(obj: dict, dotted: str) -> Optional[str]:
    cur = obj
    for part in dotted.split('.'):
        if not isinstance(cur, dict):
            return None
        if part not in cur:
            return None
        cur = cur[part]
    if isinstance(cur, (str, int)):
        return str(cur)
    return None


def _extract_conversation_id(payload: Dict) -> Optional[str]:
    # Try several common shapes
    for key in ("conversation_id", "session_id", "conversationId", "sessionId"):
        if key in payload:
            val = payload.get(key)
            if isinstance(val, (str, int)):
                return str(val)
    # Attributes/resource fields (varies by collector/exporter)
    for root in ("attributes", "resource", "resource.attributes"):
        blob = payload.get(root) if isinstance(payload.get(root), dict) else payload
        for k in ("conversation.id", "conversation_id", "session.id", "session_id"):
            val = _dig(blob, k) if blob is not None else None
            if val:
                return val
    return None


class OTELJsonlTailer:
    """
    Very small JSONL tailer for OTEL logs written by a file exporter.
    Produces (conversation_id, event_kind) pairs you can interpret as heartbeats.
    """

    def __init__(self, path: str, poll_interval: float = 1.0) -> None:
        self.path = path
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        # Wait for the file to appear; exit quietly if stop requested.
        while not os.path.exists(self.path):
            if self._stop.is_set():
                return
            await asyncio.sleep(self.poll_interval)

        # Tail forever
        with open(self.path, 'r', encoding='utf-8', errors='ignore') as fh:
            # Seek to EOF to avoid replay unless you want historical events
            fh.seek(0, io.SEEK_END)
            while not self._stop.is_set():
                pos = fh.tell()
                line = fh.readline()
                if not line:
                    fh.seek(pos)
                    await asyncio.sleep(self.poll_interval)
                    continue
                try:
                    payload = json.loads(line.strip())
                except Exception:
                    continue
                conv = _extract_conversation_id(payload) or ''
                if not conv:
                    continue
                kind = (
                    payload.get('name')
                    or payload.get('event_name')
                    or payload.get('body', {}).get('name')
                    or 'otel_event'
                )
                yield (conv, str(kind))
