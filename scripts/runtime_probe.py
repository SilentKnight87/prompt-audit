#!/usr/bin/env python3
"""Read-only runtime evidence: what model and effort did recent sessions actually run with?

Reads model / effort fields by default; --cache selects usage metadata, never message text.
Usage: runtime_probe.py [--claude-dir ~/.claude/projects] [--codex-dir ~/.codex/sessions] [--last 30]
Add --cache for token-weighted cache shares by harness, model and inter-request gap.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
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


GAPS = ("<5m", "5-60m", "1-24h", ">24h")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def input_usage(usage, kind):
    if not isinstance(usage, dict):
        return None
    fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens") if kind == "claude" else (
        "input_tokens", "cached_input_tokens")
    values = [usage.get(k, 0) for k in fields]
    if any(type(v) is not int or v < 0 for v in values) or "input_tokens" not in usage:
        return None
    total = sum(values) if kind == "claude" else values[0]
    cached = values[1]
    return (total, cached) if total > 0 and cached <= total else None


def cache_requests(path, kind):
    """Select usage metadata only. Never inspect message content or tool payloads."""
    requests, records, seen_totals = {}, {}, set()
    has_records = False
    model = "unknown"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                when = timestamp(row.get("timestamp"))
                if kind == "claude":
                    if row.get("type") != "assistant":
                        continue
                    message = row.get("message")
                    if not isinstance(message, dict):
                        continue
                    key = message.get("id") or row.get("requestId")
                    raw_usage = message.get("usage")
                    if not isinstance(raw_usage, dict) or not isinstance(key, str):
                        continue
                    previous = requests.get(key)
                    # Streamed chunks repeat one message; keep its latest counters and first timestamp.
                    if previous and previous[0]["timestamp"] is not None:
                        when = previous[0]["timestamp"]
                    name = message.get("model")
                    iterations = raw_usage.get("iterations")
                    usages = iterations if isinstance(iterations, list) and len(iterations) > 1 else [raw_usage]
                    entries = []
                    for item in usages:
                        usage = input_usage(item, kind)
                        if usage:
                            item_model = item.get("model") or name
                            entries.append({"model": item_model if isinstance(item_model, str) else "unknown",
                                            "timestamp": when, "input_tokens": usage[0],
                                            "cached_input_tokens": usage[1]})
                    if entries:
                        requests[key] = entries
                    continue
                else:
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if row.get("type") == "turn_context":
                        name = payload.get("model")
                        model = name if isinstance(name, str) else "unknown"
                        continue
                    if row.get("type") == "token_usage_record":
                        has_records = True
                        key = payload.get("response_id")
                        usage = input_usage(payload.get("usage"), kind)
                        if isinstance(key, str) and usage:
                            previous = records.get(key)
                            if previous and previous["timestamp"] is not None:
                                when = previous["timestamp"]
                            records[key] = {"model": model, "timestamp": when,
                                            "input_tokens": usage[0], "cached_input_tokens": usage[1]}
                        continue
                    if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    last = info.get("last_token_usage")
                    usage = input_usage(last, kind)
                    if not usage:
                        continue
                    totals = info.get("total_token_usage")
                    if not isinstance(totals, dict) or not input_usage(totals, kind):
                        continue
                    key = tuple(sorted((k, v) for k, v in totals.items() if type(v) is int))
                    if key in seen_totals:
                        continue
                    seen_totals.add(key)
                    name = model
                requests[key] = {"model": name if isinstance(name, str) else "unknown",
                                 "timestamp": when, "input_tokens": usage[0], "cached_input_tokens": usage[1]}
    except OSError:
        pass
    if kind == "claude":
        return [entry for entries in requests.values() for entry in entries]
    return list(records.values()) if has_records else list(requests.values())


def gap_bucket(seconds):
    if seconds < 300:
        return "<5m"
    if seconds < 3600:
        return "5-60m"
    if seconds <= 86400:
        return "1-24h"
    return ">24h"


def cache_probe(claude_dir: str, codex_dir: str, last: int) -> dict:
    out = {"metric": "token-weighted share, not hit rate", "harnesses": {}}

    def empty():
        return {"requests": 0, "input_tokens": 0, "cached_input_tokens": 0}

    def add(counts, request):
        counts["requests"] += 1
        for field in ("input_tokens", "cached_input_tokens"):
            counts[field] += request[field]

    def share(counts):
        total = counts["input_tokens"]
        counts["cached_input_share"] = counts["cached_input_tokens"] / total if total else None

    for harness, kind, pattern in (("claude_code", "claude", f"{claude_dir}/*/*.jsonl"),
                                   ("codex", "codex", f"{codex_dir}/*/*/*/*.jsonl")):
        models = {}
        files = sorted(glob.glob(os.path.expanduser(pattern)), key=os.path.getmtime)[-last:] if last > 0 else []
        for path in files:
            previous = None
            for request in cache_requests(path, kind):
                name = request["model"]
                if name not in models:
                    models[name] = {**empty(), "gaps": {gap: empty() for gap in GAPS}, "unbucketed_requests": 0}
                counts = models[name]
                add(counts, request)
                when = request["timestamp"]
                if previous is not None and when is not None and when >= previous:
                    add(counts["gaps"][gap_bucket(when - previous)], request)
                else:
                    counts["unbucketed_requests"] += 1
                previous = when
        for counts in models.values():
            share(counts)
            for bucket in counts["gaps"].values():
                share(bucket)
        out["harnesses"][harness] = dict(sorted(models.items()))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude-dir", default="~/.claude/projects")
    ap.add_argument("--codex-dir", default="~/.codex/sessions")
    ap.add_argument("--last", type=int, default=30)
    ap.add_argument("--cache", action="store_true", help="usage-only cache evidence (last N session files per harness)")
    a = ap.parse_args(argv)
    run = cache_probe if a.cache else probe
    print(json.dumps(run(a.claude_dir, a.codex_dir, a.last), indent=1))


if __name__ == "__main__":
    main()
