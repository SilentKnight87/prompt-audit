#!/usr/bin/env python3
"""Read-only runtime evidence: what model and effort did recent sessions actually run with?

Reads only the model / effort fields from local session transcripts (never message text).
Usage: runtime_probe.py [--claude-dir ~/.claude/projects] [--codex-dir ~/.codex/sessions] [--last 30]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

MODEL_RE = {
    "claude": re.compile(r'"model":"(claude-[a-z0-9.-]+)"'),
    "codex": re.compile(r'"model":"([a-z0-9.-]+)"'),
}
EFFORT_RE = {
    "claude": re.compile(r'"effort":"([a-z]+)"'),
    "codex": re.compile(r'"(?:reasoning_effort|effort)":"([a-z]+)"'),
}


def first(rx, text):
    m = rx.search(text)
    return m.group(1) if m else None


def sessions(pattern: str, kind: str, last: int) -> collections.Counter:
    files = sorted(glob.glob(os.path.expanduser(pattern)), key=os.path.getmtime)[-last:]
    c = collections.Counter()
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                text = fh.read(2_000_000)
        except OSError:
            continue
        c[(first(MODEL_RE[kind], text), first(EFFORT_RE[kind], text))] += 1
    return c


def probe(claude_dir: str, codex_dir: str, last: int) -> dict:
    return {
        "claude_code": {f"{m}@{e}": n for (m, e), n in sessions(f"{claude_dir}/*/*.jsonl", "claude", last).most_common()},
        "codex": {f"{m}@{e}": n for (m, e), n in sessions(f"{codex_dir}/*/*/*/*.jsonl", "codex", last).most_common()},
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-dir", default="~/.claude/projects")
    ap.add_argument("--codex-dir", default="~/.codex/sessions")
    ap.add_argument("--last", type=int, default=30)
    a = ap.parse_args(argv)
    print(json.dumps(probe(a.claude_dir, a.codex_dir, a.last), indent=1))


if __name__ == "__main__":
    main()
