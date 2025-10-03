"""Helpers for treating GitHub Issues as orchestration charters."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

__all__ = [
    "GitHubError",
    "IssueDetails",
    "IssueCharter",
    "comment_issue",
    "comment_pr",
    "fetch_issue",
    "list_orchestrate_issues",
    "parse_issue_body",
    "format_issue_prompt",
    "parse_blockers",
    "sla_from_labels",
    "repo_slug",
    "ensure_status_comment",
    "update_comment",
    "fetch_prs_for_issue",
]


class GitHubError(RuntimeError):
    """Raised when a GitHub CLI command fails."""


@dataclass
class IssueDetails:
    number: int
    title: str
    state: str
    url: str
    labels: List[str]
    body: str = ""


@dataclass
class IssueCharter:
    goal: str
    acceptance: List[str]
    scope_notes: List[str]
    validation: str


_SECTION_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SECTION_KEYS = {
    "goal": ["goal"],
    "acceptance": ["acceptance-checklist", "acceptance", "acceptance-criteria"],
    "scope": ["scope", "scope-notes"],
    "validation": ["validation", "test-plan", "tests"],
}
_CHECKBOX_RE = re.compile(r"^[\-\*\+]\s*(?:\[[ xX*]\]\s*)?(.*)$")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise_heading(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned


def _clean_lines(lines: Iterable[str]) -> List[str]:
    return [line.strip() for line in lines if line.strip()]


def _parse_checklist(lines: Iterable[str]) -> List[str]:
    items: List[str] = []
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        match = _CHECKBOX_RE.match(text)
        if match:
            candidate = match.group(1).strip()
        else:
            candidate = text
        if candidate:
            items.append(candidate)
    return items


def parse_issue_body(body: str | None) -> IssueCharter:
    """Extract goal, acceptance checklist, scope, and validation sections."""

    text = body or ""
    sections: dict[str, List[str]] = {"__preamble__": []}
    current = "__preamble__"
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            current = _normalise_heading(match.group(1))
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line.rstrip())

    def _section(keys: Sequence[str]) -> List[str]:
        for key in keys:
            if key in sections:
                return sections[key]
        # allow prefix matches (e.g., "goal-and-background")
        for name, content in sections.items():
            for key in keys:
                if name.startswith(key):
                    return content
        return []

    goal_lines = _clean_lines(_section(_SECTION_KEYS["goal"]))
    acceptance_lines = _section(_SECTION_KEYS["acceptance"])
    scope_lines = _section(_SECTION_KEYS["scope"]) or _section(["scope-notes", "scope-and-limits"])
    validation_lines = _clean_lines(_section(_SECTION_KEYS["validation"]))

    goal_text = " ".join(goal_lines)
    acceptance_items = _parse_checklist(acceptance_lines)
    scope_items = _parse_checklist(scope_lines) or _clean_lines(scope_lines)
    validation_text = "\n".join(validation_lines)

    return IssueCharter(
        goal=goal_text,
        acceptance=acceptance_items,
        scope_notes=scope_items,
        validation=validation_text,
    )


def _run_gh(args: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GH_PAGER", "cat")
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitHubError("GitHub CLI 'gh' not found on PATH") from exc
    return proc


def _ensure_success(proc: subprocess.CompletedProcess[str], context: str) -> str:
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        message = stderr or proc.stdout.strip() or context
        raise GitHubError(message)
    return proc.stdout


def comment_issue(repo_path: str, issue_number: int, body: str) -> None:
    proc = _run_gh(["issue", "comment", str(issue_number), "-b", body], cwd=repo_path)
    _ensure_success(proc, f"failed to comment on issue #{issue_number}")


def comment_pr(repo_path: str, pr_number: int, body: str) -> None:
    proc = _run_gh(["pr", "comment", str(pr_number), "-b", body], cwd=repo_path)
    _ensure_success(proc, f"failed to comment on PR #{pr_number}")


def fetch_issue(repo_path: str, issue_number: int) -> IssueDetails:
    fields = "number,title,state,url,labels,body"
    proc = _run_gh(["issue", "view", str(issue_number), "--json", fields], cwd=repo_path)
    output = _ensure_success(proc, f"failed to fetch issue #{issue_number}")
    data = json.loads(output or "{}")
    labels = [item.get("name", "") for item in data.get("labels", []) if item.get("name")]
    return IssueDetails(
        number=data.get("number", issue_number),
        title=data.get("title", ""),
        state=data.get("state", ""),
        url=data.get("url", ""),
        labels=labels,
        body=data.get("body", ""),
    )


def list_orchestrate_issues(repo_path: str, limit: int = 20) -> List[IssueDetails]:
    fields = "number,title,state,url,labels"
    proc = _run_gh([
        "issue",
        "list",
        "--label",
        "orchestrate",
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        fields,
    ], cwd=repo_path)
    output = _ensure_success(proc, "failed to list orchestrate issues")
    payload = json.loads(output or "[]")
    issues: List[IssueDetails] = []
    for item in payload:
        labels = [lab.get("name", "") for lab in item.get("labels", []) if lab.get("name")]
        issues.append(
            IssueDetails(
                number=item.get("number", 0),
                title=item.get("title", ""),
                state=item.get("state", ""),
                url=item.get("url", ""),
                labels=labels,
            )
        )
    return issues


def format_issue_prompt(issue: IssueDetails, charter: IssueCharter) -> str:
    lines: List[str] = [f"Work on Issue #{issue.number}: {issue.title}"]
    if charter.goal:
        goal = _WHITESPACE_RE.sub(" ", charter.goal.strip())
        lines.append(f"Goal: {goal}")
    if charter.acceptance:
        lines.append("Acceptance:")
        for idx, item in enumerate(charter.acceptance, start=1):
            cleaned = _WHITESPACE_RE.sub(" ", item.strip())
            lines.append(f"{idx}. {cleaned}")
    if charter.scope_notes:
        joined = "; ".join(charter.scope_notes)
        lines.append(f"Scope: {joined}")
    if charter.validation:
        validation = charter.validation.strip()
        if validation:
            lines.append(f"Validation: {validation}")
    if issue.labels:
        labels = ", ".join(sorted(issue.labels))
        lines.append(f"Labels: {labels}")
    return "\n".join(lines)


# Blockers, SLAs, and status comment helpers
_BLOCKERS_LABEL_RE = re.compile(r"^blocked-by:(.+)$", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#(\d+)")
_SLA_CHECKIN_RE = re.compile(r"^checkin:(\d+)([smhd])$", re.IGNORECASE)
_SLA_BUDGET_RE = re.compile(r"^budget:(\d+)([smhd])$", re.IGNORECASE)


def _parse_duration_to_seconds(num: int, unit: str) -> int:
    unit = unit.lower()
    if unit == "s":
        return num
    if unit == "m":
        return num * 60
    if unit == "h":
        return num * 3600
    if unit == "d":
        return num * 86400
    return num


def parse_blockers(body: str, labels: List[str]) -> List[int]:
    """Return a list of issue numbers that block this issue."""

    blockers: List[int] = []
    for lab in labels:
        match = _BLOCKERS_LABEL_RE.match(lab.strip())
        if not match:
            continue
        for ref in _ISSUE_REF_RE.findall(match.group(1)):
            try:
                blockers.append(int(ref))
            except Exception:
                continue

    for raw in (body or "").splitlines():
        text = raw.strip()
        if not text.lower().startswith(("blocked by:", "blocked-by:")):
            continue
        for ref in _ISSUE_REF_RE.findall(text):
            try:
                blockers.append(int(ref))
            except Exception:
                continue

    return sorted({n for n in blockers if n > 0})


def sla_from_labels(labels: List[str]) -> Dict[str, int]:
    """Extract check-in and budget SLAs from labels like checkin:10m, budget:45m."""

    values: Dict[str, int] = {}
    for lab in labels:
        text = lab.strip()
        match_checkin = _SLA_CHECKIN_RE.match(text)
        if match_checkin:
            values["checkin_seconds"] = _parse_duration_to_seconds(int(match_checkin.group(1)), match_checkin.group(2))
        match_budget = _SLA_BUDGET_RE.match(text)
        if match_budget:
            values["budget_seconds"] = _parse_duration_to_seconds(int(match_budget.group(1)), match_budget.group(2))
    return values


def repo_slug(repo_path: str) -> str:
    """Return owner/name for the repository at repo_path."""

    raw = _ensure_success(_run_gh(["repo", "view", "--json", "name,owner"], cwd=repo_path), "gh repo view failed")
    data = json.loads(raw or "{}")
    owner = (data.get("owner") or {}).get("login") or data.get("owner") or ""
    name = data.get("name") or ""
    return f"{owner}/{name}".strip("/")


def _gh_api(path: str, method: str = "GET", cwd: Optional[str] = None, fields: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> str:
    args = ["api", path]
    verb = (method or "GET").upper()
    if verb != "GET":
        args.extend(["-X", verb])
    if fields:
        for key, value in fields.items():
            args.extend(["-f", f"{key}={value}"])
    if input_text is not None:
        proc = subprocess.run(["gh", *args], input=input_text, cwd=cwd, text=True, capture_output=True)
        return _ensure_success(proc, f"gh api {path} failed")
    return _ensure_success(_run_gh(args, cwd=cwd), f"gh api {path} failed")


def ensure_status_comment(repo_path: str, issue_number: int, marker: str = "<!-- orch:status -->") -> int:
    """Return the id of the marker comment, creating it if needed."""

    slug = repo_slug(repo_path)
    raw = _gh_api(f"repos/{slug}/issues/{issue_number}/comments", cwd=repo_path)
    items = json.loads(raw or "[]")
    for item in items:
        body = (item.get("body") or "").strip()
        if body.startswith(marker):
            return int(item.get("id"))

    initial = f"{marker}\n\nStatus: tracking started."
    created = _gh_api(
        f"repos/{slug}/issues/{issue_number}/comments",
        method="POST",
        cwd=repo_path,
        input_text=initial,
    )
    data = json.loads(created or "{}")
    return int(data.get("id"))


def update_comment(repo_path: str, comment_id: int, body: str) -> None:
    slug = repo_slug(repo_path)
    _gh_api(
        f"repos/{slug}/issues/comments/{comment_id}",
        method="PATCH",
        cwd=repo_path,
        input_text=body,
    )


def fetch_prs_for_issue(repo_path: str, issue_number: int) -> List[Dict]:
    """List PRs whose title references #<issue_number>."""

    query = f'in:title "#{issue_number}"'
    raw = _ensure_success(
        _run_gh(
            [
                "pr",
                "list",
                "--search",
                query,
                "--json",
                "number,title,state,url,headRefName,baseRefName",
            ],
            cwd=repo_path,
        ),
        "gh pr list failed",
    )
    return json.loads(raw or "[]")
