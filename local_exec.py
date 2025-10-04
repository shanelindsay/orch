from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional

DEFAULT_ALLOWED = {
    "git": {"status", "rev-parse", "checkout", "switch", "add", "commit", "push", "fetch", "pull", "merge", "worktree"},
    "gh": {"issue", "pr", "repo", "auth"},
}


@dataclass
class ExecResult:
    ok: bool
    code: int
    cmd: str
    cwd: str
    stdout: str
    stderr: str


def _is_allowed(argv: List[str], allow: dict[str, set[str]]) -> bool:
    if not argv:
        return False
    prog = os.path.basename(argv[0])
    if prog not in allow:
        return False
    if len(argv) == 1:
        return True
    sub = argv[1]
    return sub in allow[prog] or sub.startswith("-")


def run_exec(payload: dict, allow: Optional[dict[str, set[str]]] = None) -> ExecResult:
    """Run a limited command described by an `exec` control block."""

    spec = dict(payload or {})
    argv = list(spec.get("argv") or [])
    cwd = spec.get("cwd") or os.getcwd()
    env_overrides = dict(spec.get("env") or {})
    allow = allow or DEFAULT_ALLOWED
    if not _is_allowed(argv, allow):
        cmd_text = " ".join(argv)
        return ExecResult(False, 126, cmd_text, cwd, "", f"denied: {cmd_text or 'empty command'}")

    env = dict(os.environ)
    env.update(env_overrides)
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    except FileNotFoundError as exc:
        cmd_text = " ".join(argv)
        return ExecResult(False, 127, cmd_text, cwd, "", str(exc))

    cmd_text = " ".join(argv)
    return ExecResult(proc.returncode == 0, proc.returncode, cmd_text, cwd, proc.stdout, proc.stderr)
