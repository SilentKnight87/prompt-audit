#!/usr/bin/env python3
"""Drive the real prompt-audit CLIs with isolated synthetic files and retain evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
AUDIT = [sys.executable, str(ROOT / "scripts/prompt_audit.py")]
PROBE = [sys.executable, str(ROOT / "scripts/runtime_probe.py")]


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run(args: list[str], evidence: Path, expected: int = 0) -> str:
    result = subprocess.run(
        args, cwd=ROOT, text=True, capture_output=True, check=False, timeout=600
    )
    with (evidence / "transcript.jsonl").open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {
                    "argv": args,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit": result.returncode,
                    "expected": expected,
                }
            )
            + "\n"
        )
    if result.returncode != expected:
        message = f"{args} exited {result.returncode}: {result.stderr}"
        raise RuntimeError(message)
    return result.stdout


def doctor() -> None:
    if sys.version_info < (3, 11):
        message = "Verification helpers need Python 3.11 or later"
        raise RuntimeError(message)
    for command in (AUDIT, PROBE):
        result = subprocess.run([*command, "--help"], cwd=ROOT, capture_output=True, check=True)
        if b"usage:" not in result.stdout:
            message = f"Unexpected help output from {command}"
            raise RuntimeError(message)
    for name in ("labs", "rules"):
        load(ROOT / "references" / f"{name}.json")
    print(f"doctor: ready; Python {sys.version.split()[0]}; checkout {ROOT}")


def audit_journey(work: Path, evidence: Path) -> None:
    source = work / "instructions" / "AGENTS.md"
    source.parent.mkdir()
    source.write_text("# Synthetic instructions\nGenerated: 2026-01-02T03:04:05Z\n")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    surfaces = work / "surfaces.json"
    save(
        surfaces,
        {
            "surfaces": [
                {
                    "id": "synthetic",
                    "label": "Synthetic instructions",
                    "harness": "multi",
                    "kind": "instructions",
                    "always_loaded": True,
                    "globs": [str(source)],
                    "readers": [{"type": "static", "model": "gpt-6-astra"}],
                }
            ]
        },
    )
    inventory = evidence / "inventory.json"
    candidates = evidence / "candidates.json"
    run([*AUDIT, "inventory", "--surfaces", str(surfaces), "--out", str(inventory)], evidence)
    files = load(inventory)["files"]
    if len(files) != 1 or files[0]["path"] != str(source):
        message = "Inventory did not discover the synthetic instruction file"
        raise AssertionError(message)
    run(
        [
            *AUDIT,
            "scan",
            "--inventory",
            str(inventory),
            "--surfaces",
            str(surfaces),
            "--out",
            str(candidates),
            "--no-blame",
        ],
        evidence,
    )
    rows: list[dict[str, Any]] = load(candidates)["candidates"]
    timestamp = next(row for row in rows if row["rule"] == "cache-timestamp")
    triage = evidence / "triage.json"
    save(
        triage,
        {
            "assumptions": ["Synthetic fixtures only. synthetic-private-marker"],
            "inventory_summary": ["One synthetic instruction file"],
            "summary": "Review the volatile timestamp before changing instructions.",
            "findings": [{"cid": timestamp["id"], "fid": "F1", "verdict": "change"}],
            "edits": [
                {
                    "finding": "F1",
                    "path": str(source),
                    "old": "Generated: 2026-01-02T03:04:05Z\n",
                    "new": "",
                }
            ],
        },
    )
    public = evidence / "public.json"
    save(public, {"denylist": ["synthetic-private-marker"], "path_alias": {str(work): "fixture"}})
    report = evidence / "report"
    run(
        [
            *AUDIT,
            "report",
            "--triage",
            str(triage),
            "--candidates",
            str(candidates),
            "--out",
            str(report),
            "--public-config",
            str(public),
        ],
        evidence,
    )
    private_text = (report / "report.private.md").read_text()
    public_text = (report / "report.public.md").read_text()
    proposed = (report / "proposed.public.diff").read_text()
    if "synthetic-private-marker" not in private_text or "synthetic-private-marker" in public_text:
        message = "Report redaction did not separate private and public evidence"
        raise AssertionError(message)
    if "F1" not in public_text or "-Generated:" not in proposed:
        message = "Report or proposed edit missing"
        raise AssertionError(message)
    if before != hashlib.sha256(source.read_bytes()).hexdigest():
        message = "Read-only audit changed its input"
        raise AssertionError(message)
    run(
        [
            *AUDIT,
            "leakcheck",
            str(report / "report.public.md"),
            str(report / "proposed.public.diff"),
            "--public-config",
            str(public),
        ],
        evidence,
    )
    leaky = work / "leaky.md"
    leaky.write_text("synthetic-private-marker\n")
    run([*AUDIT, "leakcheck", str(leaky), "--public-config", str(public)], evidence, expected=1)
    save(evidence / "audit-assertions.json", {"input_unchanged": True, "leak_rejected": True})


def runtime_journey(work: Path, evidence: Path) -> None:
    claude = work / "claude"
    codex = work / "codex"
    session = claude / "synthetic" / "session.jsonl"
    session.parent.mkdir(parents=True)
    rows = [
        {
            "type": "assistant",
            "timestamp": f"2026-01-02T03:0{index}:00Z",
            "message": {
                "id": f"request-{index}",
                "model": "claude-opus-5-5",
                "usage": {"input_tokens": 20, "cache_read_input_tokens": 80},
            },
        }
        for index in range(2)
    ]
    session.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n")
    args = [*PROBE, "--claude-dir", str(claude), "--codex-dir", str(codex), "--last", "30"]
    default: dict[str, Any] = json.loads(run(args, evidence))
    if default["claude_code"] != {"claude-opus-5-5@None": 1} or default["codex"] != {}:
        message = "Default runtime probe did not count only synthetic sessions"
        raise AssertionError(message)
    cache: dict[str, Any] = json.loads(run([*args, "--cache"], evidence))
    model = cache["harnesses"]["claude_code"]["claude-opus-5-5"]
    if model["requests"] != 2 or model["cached_input_share"] != 0.8:
        message = "Cache probe totals differ from synthetic usage"
        raise AssertionError(message)
    if model["gaps"]["<5m"]["requests"] != 1 or model["unbucketed_requests"] != 1:
        message = "Cache probe gap buckets differ from synthetic timestamps"
        raise AssertionError(message)
    save(evidence / "runtime-default.json", default)
    save(evidence / "runtime-cache.json", cache)


def guidance_journey(evidence: Path) -> None:
    cache = evidence / "guidance"
    run([*AUDIT, "fetch-guidance", "--cache", str(cache)], evidence)
    sources: list[dict[str, Any]] = load(cache / "manifest.json")["sources"]
    expected = sum(
        len(lab["sources"]) for lab in load(ROOT / "references/labs.json")["labs"].values()
    )
    if len(sources) != expected:
        message = "Guidance fetch omitted registered sources"
        raise AssertionError(message)
    failures = [row["id"] for row in sources if row["status"] != "ok"]
    if failures:
        message = f"External guidance unavailable: {', '.join(failures)}; see manifest.json"
        raise RuntimeError(message)
    for row in sources:
        text = Path(row["path"]).read_text(encoding="utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != row["sha256"] or not text:
            message = f"Guidance cache digest mismatch: {row['id']}"
            raise AssertionError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journey", choices=("doctor", "all", "guidance"), default="all", nargs="?")
    args = parser.parse_args()
    doctor()
    if args.journey == "doctor":
        return
    parent = ROOT / ".verify"
    parent.mkdir(exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix=f"{args.journey}-", dir=parent))
    try:
        if args.journey == "guidance":
            guidance_journey(evidence)
        else:
            with tempfile.TemporaryDirectory(prefix="scratch-", dir=evidence) as temp:
                work = Path(temp)
                audit_journey(work, evidence)
                runtime_journey(work, evidence)
                shutil.copytree(work / "instructions", evidence / "inputs")
        save(evidence / "result.json", {"verdict": "pass", "journey": args.journey})
    except (
        OSError,
        ValueError,
        RuntimeError,
        AssertionError,
        StopIteration,
        subprocess.SubprocessError,
    ) as error:
        save(evidence / "result.json", {"verdict": "fail", "error": str(error)})
        raise
    finally:
        print(f"evidence: {evidence}")
    print(f"verify-prompt-audit {args.journey}: pass; scratch cleaned; evidence retained")


if __name__ == "__main__":
    main()
