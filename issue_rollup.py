#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple


RE_ISSUE_LOCAL = re.compile(r"#(\d+)")
RE_ISSUE_CROSS = re.compile(r"(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)#(?P<num>\d+)")
RE_ISSUE_URL = re.compile(r"https://github.com/(?P<owner>[A-Za-z0-9_.\-]+)/(?P<repo>[A-Za-z0-9_.\-]+)/issues/(?P<num>\d+)")


def sh(args: List[str], cwd: Optional[str] = None, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"cmd failed: {' '.join(args)}")
    return proc.stdout


def gh_api(path: str, method: str = "GET", fields: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> str:
    args = ["gh", "api", path]
    if method.upper() != "GET":
        args += ["-X", method.upper()]
    if fields:
        for k, v in fields.items():
            args += ["-f", f"{k}={v}"]
    if input_text is not None:
        proc = subprocess.run(args, input=input_text, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"gh api {path} failed")
        return proc.stdout
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"gh api {path} failed")
    return proc.stdout


def repo_slug_from_cwd(cwd: Optional[str] = None) -> str:
    raw = sh(["gh", "repo", "view", "--json", "name,owner"], cwd=cwd)
    data = json.loads(raw or "{}")
    owner = (data.get("owner") or {}).get("login") or data.get("owner") or ""
    name = data.get("name") or ""
    slug = f"{owner}/{name}".strip("/")
    if not slug or "/" not in slug:
        raise RuntimeError("could not resolve repo slug; ensure `gh` is authenticated in this repo")
    return slug


@dataclass
class Issue:
    repo: str  # owner/name
    number: int
    title: str
    state: str
    url: str
    labels: List[str]
    body: str


def fetch_issue(repo_slug: str, number: int) -> Issue:
    raw = gh_api(f"repos/{repo_slug}/issues/{number}")
    obj = json.loads(raw or "{}")
    return Issue(
        repo=repo_slug,
        number=int(obj.get("number") or number),
        title=obj.get("title") or "",
        state=obj.get("state") or "",
        url=obj.get("html_url") or "",
        labels=[lab.get("name") for lab in obj.get("labels", []) if lab.get("name")],
        body=obj.get("body") or "",
    )


def extract_issue_refs(text: str, default_repo: str) -> List[Tuple[str, int]]:
    refs: List[Tuple[str, int]] = []
    for m in RE_ISSUE_URL.finditer(text or ""):
        refs.append((f"{m.group('owner')}/{m.group('repo')}", int(m.group("num"))))
    for m in RE_ISSUE_CROSS.finditer(text or ""):
        refs.append((f"{m.group('owner')}/{m.group('repo')}", int(m.group("num"))))
    for m in RE_ISSUE_LOCAL.finditer(text or ""):
        refs.append((default_repo, int(m.group(1))))
    # dedupe, preserve order
    seen = set()
    unique: List[Tuple[str, int]] = []
    for slug, num in refs:
        key = (slug.lower(), num)
        if key in seen:
            continue
        seen.add(key)
        unique.append((slug, num))
    return unique


HPC_KEYWORDS = {
    "hpc", "mpi", "openmp", "slurm", "pbs", "lsf", "singularity", "apptainer", "infiniband", "numa",
    "gpu", "cuda", "horovod", "multi-node", "spack", "qsub", "srun", "mpirun", "compute node",
    "node-hours", "petabyte", "terabyte", "job array", "rdma",
}

CLOUD_KEYWORDS = {
    "aws", "gcp", "azure", "lambda", "serverless", "cloud run", "cloud functions", "s3", "bigquery",
    "dataproc", "emr", "glue", "snowflake", "databricks", "airflow", "kubernetes", "eks", "gke", "aks",
    "terraform", "docker", "container registry", "cloudwatch", "stackdriver", "cloud logging", "cloud storage",
}


def classify_task(title: str, labels: List[str], body: str) -> str:
    text = f"{title}\n{body}".lower()
    labs = {lab.lower() for lab in labels}
    if any(k in text for k in HPC_KEYWORDS) or any(l in {"hpc", "gpu", "slurm", "mpi"} for l in labs):
        return "HPC"
    if any(k in text for k in CLOUD_KEYWORDS) or any(l in {"cloud", "aws", "gcp", "azure", "kubernetes"} for l in labs):
        return "Cloud"
    return "Unknown"


def render_rollup(master: Issue, epic1: Issue, epic2: Issue, sub_master: List[Issue], sub_e1: List[Issue], sub_e2: List[Issue]) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    def sec(issue: Issue, subs: List[Issue]) -> str:
        lines: List[str] = []
        lines.append(f"### {issue.title} (#{issue.number})")
        lines.append(f"Repo: {issue.repo} • State: {issue.state} • Link: {issue.url}")
        lines.append("")
        if subs:
            lines.append("Open sub-issues:")
            for it in subs:
                lines.append(f"- [ ] #{it.number} — {it.title} ({it.state}) — {it.url}")
        else:
            lines.append("No open sub-issues detected from the body/task list.")
        lines.append("")
        return "\n".join(lines)

    # Consolidated view with lightweight classification
    all_open = [("Master", sub_master), ("Epic 1", sub_e1), ("Epic 2", sub_e2)]
    hpc: List[Issue] = []
    cloud: List[Issue] = []
    unknown: List[Issue] = []
    for _, group in all_open:
        for it in group:
            kind = classify_task(it.title, it.labels, it.body)
            if kind == "HPC":
                hpc.append(it)
            elif kind == "Cloud":
                cloud.append(it)
            else:
                unknown.append(it)

    advice = [
        "- Prefer HPC for multi-node MPI/OpenMP workloads, GPU-heavy training, very large memory jobs, or when a scheduler (Slurm/PBS) is already required.",
        "- Use Cloud for bursty pipelines, web/data services, CI/CD, and one-off compute where managed services (Batch, Dataproc/EMR, Kubernetes) fit well.",
        "- Containerize once (Docker) and adapt: HPC via Apptainer/Singularity, Cloud via registries and orchestrators.",
        "- For HPC tasks: submit job arrays, pre-stage data to fast storage, and budget queue times; for Cloud tasks: autoscale, use spot/low-priority nodes, and set per-task budgets.",
    ]

    out: List[str] = []
    out.append("# Issue Rollup: Master + Epics 1 & 2")
    out.append("")
    out.append(f"Generated: {now}")
    out.append("")
    out.append("## Overview")
    out.append("")
    out.append("This rollup pulls the master issue and two epics with their open sub-issues found in their task lists or referenced issue links.")
    out.append("")
    out.append("## Master & Epics")
    out.append("")
    out.append(sec(master, sub_master))
    out.append(sec(epic1, sub_e1))
    out.append(sec(epic2, sub_e2))
    out.append("## Consolidated Plan")
    out.append("")
    out.append("### Needs HPC")
    if hpc:
        for it in hpc:
            out.append(f"- #{it.number} — {it.title} ({it.repo}) — {it.url}")
    else:
        out.append("- None identified")
    out.append("")
    out.append("### Cloud OK")
    if cloud:
        for it in cloud:
            out.append(f"- #{it.number} — {it.title} ({it.repo}) — {it.url}")
    else:
        out.append("- None identified")
    out.append("")
    out.append("### Needs Triage")
    if unknown:
        for it in unknown:
            out.append(f"- #{it.number} — {it.title} ({it.repo}) — {it.url}")
    else:
        out.append("- None")
    out.append("")
    out.append("### Recommended Course of Action")
    out.extend(advice)
    out.append("")
    return "\n".join(out)


def select_open_subissues(container: Issue) -> List[Issue]:
    refs = extract_issue_refs(container.body, container.repo)
    results: List[Issue] = []
    for slug, num in refs:
        try:
            item = fetch_issue(slug, num)
        except Exception:
            continue
        if item.state.lower() == "open":
            results.append(item)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Roll up a master issue, epic 1 and epic 2 with their open sub-issues into a markdown file.")
    ap.add_argument("--repo", default=None, help="owner/name repo slug (default: current repo)")
    ap.add_argument("--master", type=int, required=True, help="Issue number for master issue")
    ap.add_argument("--epic1", type=int, required=True, help="Issue number for Epic 1")
    ap.add_argument("--epic2", type=int, required=True, help="Issue number for Epic 2")
    ap.add_argument("--output", default="docs/issues-rollup.md", help="Output markdown path")
    args = ap.parse_args()

    repo_slug = args.repo or repo_slug_from_cwd()

    master = fetch_issue(repo_slug, args.master)
    epic1 = fetch_issue(repo_slug, args.epic1)
    epic2 = fetch_issue(repo_slug, args.epic2)

    sub_master = select_open_subissues(master)
    sub_e1 = select_open_subissues(epic1)
    sub_e2 = select_open_subissues(epic2)

    content = render_rollup(master, epic1, epic2, sub_master, sub_e1, sub_e2)

    out_path = args.output
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()

