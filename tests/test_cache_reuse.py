import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import prompt_audit as pa
import runtime_probe as rp


def instruction(tmp_path, text, always_loaded=True):
    path = tmp_path / "AGENTS.md"
    path.write_text(text)
    return {"path": str(path), "kind": "instructions", "owner": "authored", "surface": "test",
            "surface_label": "test", "harness": "multi", "always_loaded": always_loaded,
            "readers": [], "copies": [str(path)], "chars": len(text)}


def cache_hits(tmp_path, text, always_loaded=True):
    rules = [r for r in pa.jload(pa.RULES_FILE)["rules"] if r["category"] == "cache-reuse"]
    return pa.scan_file(instruction(tmp_path, text, always_loaded), rules)


@pytest.mark.parametrize("line,rule,confidence", [
    ("Generated: 2026-01-02T03:04:05Z", "cache-timestamp", "medium"),
    ("2026-01-02T03:04:05Z", "cache-timestamp", "medium"),
    ("**Last updated:** 03:04", "cache-timestamp", "medium"),
    ("Last updated: 03:04", "cache-timestamp", "medium"),
    ("As of 2026-01-02 03:04:05+00:00", "cache-timestamp", "medium"),
    ("Updated: 2026-01-02", "cache-date", "low"),
    ("Current date: {{date}}", "cache-template", "medium"),
    ("Today is {{now}}.", "cache-template", "medium"),
    ("Current date: ${DATE}", "cache-template", "medium"),
    ("Generated: $(date +%FT%T)", "cache-template", "medium"),
    ("Run count: 42", "cache-live-number", "low"),
    ("**Run count:** 42", "cache-live-number", "low"),
    ("Run #42", "cache-live-number", "low"),
    ("Tasks remaining: 12", "cache-live-number", "low"),
    ("Budget: 10% left", "cache-live-number", "low"),
    ("7 tasks left", "cache-live-number", "low"),
    ("Session ID: 00000000-0000-4000-8000-000000000001", "cache-generated-id", "medium"),
    ("**Session ID:** 00000000-0000-4000-8000-000000000001", "cache-generated-id", "medium"),
])
def test_cache_candidates(tmp_path, line, rule, confidence):
    hits = cache_hits(tmp_path, line + "\n")
    assert [h["rule"] for h in hits] == [rule]
    assert hits[0]["action"] == "flag" and hits[0]["confidence"] == confidence


@pytest.mark.parametrize("text", [
    "We shipped the migration on 2026-01-02 at 03:04.\n",
    "As of 2026-01-02 we used the old deployment.\n",
    "Updated the migration on 2026-01-02 at 03:04.\n",
    "## Changelog\nUpdated: 2026-01-02T03:04:05Z\n### Details\nRun count: 42\n",
    "## History\nGenerated: 03:04\n",
    "## Examples\nCurrent date: {{date}}\n",
    "```text\nCurrent date: {{date}}\n```\n",
    "~~~text\nGenerated: 03:04\n~~~\n",
    "    Generated: 03:04\n",
    "> Generated: 03:04\n",
    "Example: Current date: {{date}}\n",
    "Run at most 5 tasks.\nThere are 12 fixed steps.\n",
    "Reference ID: 00000000-0000-4000-8000-000000000001\n",
])
def test_cache_non_candidates(tmp_path, text):
    assert cache_hits(tmp_path, text) == []


def test_cache_scope_and_position(tmp_path):
    text = "Generated: 03:04\n" + "Keep this rule.\n" * 8 + "Generated: 03:04\n"
    assert cache_hits(tmp_path, text, always_loaded=False) == []
    first, last = cache_hits(tmp_path, text)
    assert (first["line"], last["line"]) == (1, 10)
    assert first["line_fraction"] == 0 and last["line_fraction"] == 0.9
    assert first["prefix_impact"] == 1 and last["prefix_impact"] == 0.1
    assert first["total_lines"] == 10


def test_cache_frontmatter_and_section_boundary(tmp_path):
    text = "---\nupdated: 2026-01-02\n---\n## Examples\nGenerated: 03:04\n## Current\nGenerated: 03:04\n"
    assert [h["line"] for h in cache_hits(tmp_path, text)] == [7, 2]


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def claude(ident, when, plain=10, read=60, creation=30, model="claude-test"):
    return {"type": "assistant", "timestamp": when, "message": {
        "id": ident, "model": model, "usage": {"input_tokens": plain,
        "cache_read_input_tokens": read, "cache_creation_input_tokens": creation},
        "content": [{"type": "text", "text": "PRIVATE MESSAGE MUST NOT APPEAR"}]}}


def codex(when, total, plain=100, cached=60):
    return {"type": "event_msg", "timestamp": when, "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": plain, "cached_input_tokens": cached},
        "total_token_usage": {"input_tokens": total, "cached_input_tokens": total // 2}}}}


def context(model):
    return {"type": "turn_context", "payload": {"model": model}}


def test_normalize_input_semantics():
    assert rp.input_usage({"input_tokens": 10, "cache_read_input_tokens": 60,
                           "cache_creation_input_tokens": 30}, "claude") == (100, 60)
    assert rp.input_usage({"input_tokens": 100, "cached_input_tokens": 60}, "codex") == (100, 60)
    assert rp.input_usage({"input_tokens": 10}, "codex") == (10, 0)
    assert rp.input_usage({"input_tokens": 10, "cached_input_tokens": 11}, "codex") is None
    assert rp.input_usage({"input_tokens": -1}, "claude") is None
    assert rp.input_usage({"input_tokens": True}, "codex") is None
    assert rp.input_usage({}, "codex") is None


def test_claude_dedupe_and_weighted_share(tmp_path):
    source = tmp_path / "claude" / "project" / "session.jsonl"
    jsonl(source, [claude("a", "2026-01-01T00:00:00Z", plain=10, read=0, creation=0),
                   claude("a", "2026-01-01T00:00:03Z"),
                   claude("b", "2026-01-01T00:04:59Z", plain=900, read=0, creation=0)])
    result = rp.cache_probe(str(tmp_path / "claude"), str(tmp_path / "codex"), 30)
    counts = result["harnesses"]["claude_code"]["claude-test"]
    assert counts["requests"] == 2
    assert counts["input_tokens"] == 1000 and counts["cached_input_tokens"] == 60
    assert counts["cached_input_share"] == 0.06
    assert counts["gaps"]["<5m"]["requests"] == 1
    assert counts["gaps"]["<5m"]["cached_input_share"] == 0
    assert counts["gaps"][">24h"]["cached_input_share"] is None
    assert counts["unbucketed_requests"] == 1
    assert "PRIVATE MESSAGE" not in json.dumps(result)


def test_codex_last_usage_dedupe_and_model_switch(tmp_path):
    source = tmp_path / "codex" / "2026" / "01" / "01" / "session.jsonl"
    jsonl(source, [context("gpt-test-a"), codex("2026-01-01T00:00:00Z", 1000),
                   codex("2026-01-01T00:01:00Z", 1000),
                   codex("2026-01-01T00:02:00Z", 1100),
                   context("gpt-test-b"), codex("2026-01-01T01:02:00Z", 2100, plain=1000, cached=900)])
    result = rp.cache_probe(str(tmp_path / "claude"), str(tmp_path / "codex"), 30)
    a, b = (result["harnesses"]["codex"][name] for name in ("gpt-test-a", "gpt-test-b"))
    assert a["requests"] == 2 and a["input_tokens"] == 200 and a["cached_input_share"] == 0.6
    assert a["gaps"]["<5m"]["requests"] == 1
    assert b["requests"] == 1 and b["cached_input_share"] == 0.9
    assert b["gaps"]["1-24h"]["requests"] == 1


@pytest.mark.parametrize("seconds,bucket", [(0, "<5m"), (299, "<5m"), (300, "5-60m"),
    (3599, "5-60m"), (3600, "1-24h"), (86400, "1-24h"), (86401, ">24h")])
def test_gap_boundaries(seconds, bucket):
    assert rp.gap_bucket(seconds) == bucket


def test_gap_buckets_and_session_boundaries(tmp_path):
    times = ["2026-01-01T00:00:00Z", "2026-01-01T00:04:00Z", "2026-01-01T00:09:00Z",
             "2026-01-01T01:09:00Z", "2026-01-02T01:09:01Z"]
    root = tmp_path / "claude"
    jsonl(root / "project" / "first.jsonl", [claude(str(i), t) for i, t in enumerate(times)])
    jsonl(root / "project" / "second.jsonl", [claude("a", "2026-02-01T00:00:00Z")])
    counts = rp.cache_probe(str(root), str(tmp_path / "codex"), 30)["harnesses"]["claude_code"]["claude-test"]
    assert counts["requests"] == 6 and counts["unbucketed_requests"] == 2
    for bucket in counts["gaps"].values():
        assert bucket["requests"] == 1 and bucket["cached_input_share"] == 0.6


def test_probe_skips_invalid_records_and_missing_usage(tmp_path):
    path = tmp_path / "codex" / "2026" / "01" / "01" / "bad.jsonl"
    row = codex("bad timestamp", 100)
    cumulative_only = codex("2026-01-01T00:00:00Z", 1000)
    del cumulative_only["payload"]["info"]["last_token_usage"]
    jsonl(path, [None, [], {}, context("gpt-test"),
                 {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                 cumulative_only, row])
    with path.open("a") as f:
        f.write('{"truncated":\n')
    result = rp.cache_probe(str(tmp_path / "claude"), str(tmp_path / "codex"), 30)
    counts = result["harnesses"]["codex"]["gpt-test"]
    assert counts["requests"] == 1 and counts["unbucketed_requests"] == 1
    assert rp.cache_probe(str(tmp_path), str(tmp_path), 0)["harnesses"] == {"claude_code": {}, "codex": {}}


def test_cache_timestamps_missing_or_backward_are_unbucketed(tmp_path):
    path = tmp_path / "claude" / "project" / "one.jsonl"
    jsonl(path, [claude("a", "2026-01-01T00:05:00Z"), claude("b", "2026-01-01T00:00:00Z"),
                 claude("c", None), claude("d", "2026-01-01T00:10:00Z")])
    counts = rp.cache_probe(str(tmp_path / "claude"), str(tmp_path / "codex"), 30)["harnesses"]["claude_code"]["claude-test"]
    assert counts["requests"] == counts["unbucketed_requests"] == 4
    assert all(bucket["requests"] == 0 for bucket in counts["gaps"].values())


def test_probe_cli_default_unchanged_and_cache_opt_in(tmp_path, capsys):
    root = tmp_path / "claude"
    jsonl(root / "project" / "one.jsonl", [claude("a", "2026-01-01T00:00:00Z")])
    args = ["--claude-dir", str(root), "--codex-dir", str(tmp_path / "codex")]
    rp.main(args)
    assert json.loads(capsys.readouterr().out) == rp.probe(str(root), str(tmp_path / "codex"), 30)
    rp.main(args + ["--cache"])
    output = json.loads(capsys.readouterr().out)
    assert output["metric"] == "token-weighted share, not hit rate"
    assert output["harnesses"]["claude_code"]["claude-test"]["requests"] == 1
