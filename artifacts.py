"""Simple append-only artifact store for text blobs."""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional, Tuple

ART_DIRNAME = "artifacts"
INDEX_BASENAME = "index.jsonl"


def _ensure_dir(root: str) -> str:
    path = os.path.join(root, ART_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _index_path(root: str) -> str:
    return os.path.join(_ensure_dir(root), INDEX_BASENAME)


def _blob_path(root: str, art_id: str) -> str:
    return os.path.join(_ensure_dir(root), f"{art_id}.txt")


def store_text(root: str, kind: str, body: str, meta: Optional[dict] = None) -> str:
    """Persist a text artifact and return its identifier."""
    now = int(time.time())
    art_id = f"{now}-{uuid.uuid4().hex[:8]}"
    with open(_blob_path(root, art_id), "w", encoding="utf-8") as handle:
        handle.write(body or "")
    record = {"id": art_id, "kind": kind, "ts": now, "meta": meta or {}}
    with open(_index_path(root), "a", encoding="utf-8") as index:
        index.write(json.dumps(record, ensure_ascii=False) + "\n")
    return art_id


def load_text(root: str, art_id: str, max_chars: Optional[int] = None) -> Tuple[str, int]:
    """Load a stored text artifact returning (text, total_length)."""
    path = _blob_path(root, art_id)
    with open(path, "r", encoding="utf-8") as handle:
        data = handle.read()
    total = len(data)
    if max_chars is not None and total > max_chars:
        data = data[:max_chars]
    return data, total
