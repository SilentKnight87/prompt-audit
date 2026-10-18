import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import prompt_audit as pa  # noqa: E402


def w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_skill_frontmatter_portable():
    text = (ROOT / "SKILL.md").read_text()
    assert text.startswith("---\n")
    fm = text.split("\n---\n", 1)[0]
    assert "\nname: prompt-audit\n" in fm
    desc = pa.frontmatter_description(text)
    assert 0 < len(desc) <= 600  # our own trigger-bloat budget, well under the 1024 validator cap
    assert "anthropics/skills" in text  # credit to the base method


def test_registries_are_consistent():
    labs = pa.jload(pa.LABS_FILE)
    ids = {s["id"] for lab in labs["labs"].values() for s in lab["sources"]}
    for lab in labs["labs"].values():
        for s in lab["sources"]:
            assert s["url"].startswith("https://") and s["verified_on"]
    rules = pa.jload(pa.RULES_FILE)["rules"]
    assert len({r["id"] for r in rules}) == len(rules)
    for r in rules:
        for src in r.get("sources", []):
            assert src.split("#")[0] in ids, f"{r['id']} cites unknown source {src}"


def test_fetch_guidance_records_errors_and_model_ids(tmp_path):
    labs = {"stale_after_days": 45, "labs": {"x": {"match": [], "sources": [
        {"id": "ok", "url": "https://a", "verified_on": "2026-09-01", "model_id_regex": "gpt-[0-9][0-9a-z.-]*"},
        {"id": "bad", "url": "https://b", "verified_on": "2020-01-01"}]}}}
    lf = w(tmp_path / "labs.json", json.dumps(labs))

    def opener(url):
        if url.endswith("b"):
            raise OSError("down")
        return b"use gpt-6-astra not gpt-6-astra.png or gpt-5.6-sol"
    m = pa.fetch_guidance(str(tmp_path / "cache"), lf, opener)
    ok, bad = m["sources"]
    assert ok["status"] == "ok" and ok["model_ids"] == ["gpt-5.6-sol", "gpt-6-astra"]
    assert bad["status"] == "error" and bad["verified_stale"] is True


def test_lab_resolution_and_cross_lab_downgrade():
    labs = pa.jload(pa.LABS_FILE)
    assert pa.lab_for_model("claude-opus-5-5", labs) == "anthropic"
    assert pa.lab_for_model("gpt-6-astra", labs) == "openai"
    assert pa.lab_for_model("gemini-3.7-flash", labs) == "google"
    rule = {"labs": ["anthropic"], "contradicted_by": ["openai"]}
    assert pa.rule_applies(rule, {"google"}) == (False, None)
    ok, note = pa.rule_applies(rule, {"anthropic", "openai"})
    assert ok and "differs" in note


def test_scan_skips_fences_and_flags_vendor(tmp_path):
    f = w(tmp_path / "s" / "SKILL.md", "---\nname: s\ndescription: d\n---\n```\nCRITICAL: in code\n```\nCRITICAL: do it\n")
    rules = [{"id": "c", "regex": "CRITICAL:", "case_sensitive": True, "group": "1", "category": "pressure-language",
              "pattern": "p", "labs": ["all"], "confidence": "medium", "action": "rewrite", "why": "w"}]
    rec = {"path": str(f), "readers": [], "kind": "skill", "owner": "authored", "surface": "t", "surface_label": "t",
           "harness": "h", "always_loaded": False, "copies": [str(f)], "chars": 50}
    hits = pa.scan_file(rec, rules)
    assert [h["line"] for h in hits] == [8]
    rec["owner"] = "vendor:bundled"
    assert pa.scan_file(rec, rules)[0]["action"] == "flag"


def test_classify_path(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "HOME", str(tmp_path))
    (tmp_path / "Vault" / "Notes").mkdir(parents=True)
    assert pa.classify_path(str(tmp_path / "Vault" / "Notes" / "new-file.md")) is None  # generated leaf
    assert pa.classify_path(str(tmp_path / "Vault" / "Notes" / "YYYY-MM-DD.md")) is None
    assert pa.classify_path(str(tmp_path / "Vault" / "Gone" / "x.md"))[0] == "intermediate"
    assert pa.classify_path(str(tmp_path / "Nope" / "x"))[0] == "root"
    assert pa.classify_path(str(tmp_path / ".tool" / "cfg"))[0] == "tooldir"


def test_config_drift_effort_key_and_truncation(tmp_path):
    claude = w(tmp_path / "claude" / "CLAUDE.md", "<!-- X BEGIN -->\nrule one.\nrule two cut …\n<!-- X END -->\n")
    inv = {"files": [{"path": str(claude), "always_loaded": True, "harness": "claude-code", "copies": [str(claude)]}],
           "configs": {"hermes": {"a": {"aux": {"vision": "gemini-2.5-flash"}}, "b": {"aux": {"vision": "gemini-3.7-flash"}}},
                       "claude_code": {"model": "claude-fable-5-1[1m]", "model_settings": {
                           "claude-fable-5": {"effortLevel": "high"}, "claude-fable-5-1": {"effortLevel": "high"}}},
                       "omp": {"roles": {}, "fallbacks": {"x": ["openai-codex/gpt-5.6-sol"]}}}}
    env = {"current_versions": pa.current_versions(["gpt-6-astra", "gemini-3.7-flash"]),
           "paths": {"hermes": {"a": "a.yaml", "b": "b.yaml"}, "claude_code": "settings.json", "omp": "omp.yml"}}
    cats = {(c["category"], c["match"]) for c in pa.config_drift(inv, env)}
    assert ("drift:effort", "modelSettings.claude-fable-5") in cats
    assert ("drift:effort", "modelSettings.claude-fable-5-1") not in cats
    assert ("drift:truncation", "managed-block") in cats
    assert ("drift:aux", "auxiliary.vision") in cats
    assert ("drift:model", "fallbackChains.x") in cats


def test_hermes_config_reader_reads_only_model_keys(tmp_path):
    cfg = w(tmp_path / "config.yaml", "model:\n  default: claude-opus-5-5[1m]\n  provider: p\n  api_key: SECRET\n"
            "agent:\n  reasoning_effort: medium\nauxiliary:\n  vision:\n    provider: gemini\n    model: gemini-2.5-flash\n")
    r = pa.read_hermes_config(str(cfg))
    assert r == {"model": "claude-opus-5-5[1m]", "provider": "p", "effort": "medium", "aux": {"vision": "gemini-2.5-flash"}}
    assert "SECRET" not in json.dumps(r)
    assert pa.strip_model("openai-codex/gpt-6-astra:high") == "gpt-6-astra"


def test_report_public_redaction_edits_and_leakcheck(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pa, "HOME", str(home))
    target = w(home / "proj" / "CLAUDE.md", "# P\n**IMPORTANT: You MUST follow these:**\nbody by Alice\n")
    cands = w(tmp_path / "c.json", json.dumps({"candidates": []}))
    tri = {"assumptions": ["Alice's stack"], "inventory_summary": ["1 file"], "summary": "Alice summary",
           "findings": [{"fid": "F01", "path": str(target), "line": 2, "surface_label": "repo", "category": "pressure-language",
                         "pattern": "1a", "evidence": "**IMPORTANT** Alice", "public_evidence": "IMPORTANT marker",
                         "why": "w", "confidence": "medium", "action": "rewrite", "headline": "h"}],
           "edits": [{"finding": "F01", "path": str(target), "old": "**IMPORTANT: You MUST follow these:**",
                      "new": "Follow these:"},
                     {"finding": "F02", "path": str(target), "old": "missing text", "new": "x"}],
           "top": ["F01"]}
    tf = w(tmp_path / "t.json", json.dumps(tri))
    pub = {"denylist": ["Alice"], "path_alias": {str(home / "proj"): "repo"}}
    res = pa.render_report(str(tf), str(cands), str(tmp_path / "out"), pub)
    assert res["rows"] == 1 and len(res["edit_errors"]) == 1
    public = (tmp_path / "out" / "report.public.md").read_text()
    assert "Alice" not in public and str(home) not in public and "`repo/CLAUDE.md:2`" in public
    assert "+Follow these:" in (tmp_path / "out" / "proposed.public.diff").read_text()
    assert target.read_text().startswith("# P\n**IMPORTANT")  # nothing applied
    outs = [str(tmp_path / "out" / "report.public.md"), str(tmp_path / "out" / "proposed.public.diff")]
    assert pa.leakcheck(outs, ["Alice"]) == []
    assert pa.leakcheck([str(tmp_path / "out" / "report.private.md")], ["Alice"])


# ---- 2026-10-17 review: regressions found by the synthetic dry run ----


def test_model_key_is_tier_aware():
    cur = pa.current_versions(["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "gemini-3.8-flash"])
    # a point release of one OpenAI tier must not make the other tiers look stale
    assert pa.model_key("gpt-6-astra") == ("gpt-astra", (6, 0))
    assert not pa.is_stale("gpt-6-astra", cur)
    assert not pa.is_stale("gpt-6-luna", cur)
    assert pa.is_stale("gpt-5.6-sol", cur)
    # a bare family name compares on the major generation only
    assert not pa.is_stale("gpt-6", cur) and not pa.is_stale("GPT-6", cur)
    assert pa.is_stale("gpt-5.5", cur)
    assert pa.is_stale("gemini-3.5-flash", cur)


def test_fetch_follows_308(monkeypatch):
    import urllib.request
    assert any(isinstance(h, pa.Redirect308) for h in pa.build_opener().handlers)
    req = urllib.request.Request("https://a.example/x")
    new = pa.Redirect308().redirect_request(req, None, 308, "moved", {}, "https://b.example/y")
    assert new is not None and new.full_url == "https://b.example/y"


def test_codex_override_shadows_agents_md(tmp_path):
    base = w(tmp_path / "codex" / "AGENTS.md", "base rules\n")
    w(tmp_path / "codex" / "AGENTS.override.md", "override\n")
    lone = w(tmp_path / "repo" / "AGENTS.md", "repo rules\n")
    codex = [{"model": "gpt-6-astra", "via": "codex", "lab": "openai"}]
    inv = {"files": [{"path": str(base), "readers": codex, "harness": "codex", "copies": [str(base)]},
                     {"path": str(lone), "readers": codex, "harness": "codex", "copies": [str(lone)]}]}
    hits = pa.shadow_drift(inv)
    assert [h["path"] for h in hits] == [str(base)] and hits[0]["confidence"] == "high"
    (tmp_path / "codex" / "AGENTS.override.md").write_text("  \n")  # empty override is skipped by Codex
    assert pa.shadow_drift(inv) == []


def test_rules_track_current_lab_guidance(tmp_path):
    rules = {r["id"]: r for r in pa.jload(pa.RULES_FILE)["rules"]}
    f = w(tmp_path / "AGENTS.md", 'Include a "reasoning" field in every JSON reply.\nCall it with temperature: 0.2\n')
    rec = {"path": str(f), "kind": "instructions", "owner": "authored", "surface": "t", "surface_label": "t",
           "harness": "codex", "always_loaded": True, "copies": [str(f)], "chars": 80}
    rec["readers"] = [{"model": "claude-opus-5-5", "lab": "anthropic"}]
    assert any(h["rule"] == "1b-show-reasoning" for h in pa.scan_file(rec, [rules["1b-show-reasoning"]]))
    rec["readers"] = [{"model": "gpt-6-astra", "lab": "openai"}]
    assert any(h["rule"] == "4-sampling-params" for h in pa.scan_file(rec, [rules["4-sampling-params"]]))


def test_inventory_skips_dataless_placeholders(tmp_path, monkeypatch):
    # a cloud-evicted (dataless) file blocks open() until it downloads; the collector must not hang on it
    good = w(tmp_path / "a" / "AGENTS.md", "rules\n")
    evicted = w(tmp_path / "b" / "AGENTS.md", "never read\n")
    real_stat = os.stat

    class St:
        def __init__(self, st, flags):
            self._st, self.st_flags = st, flags

        def __getattr__(self, k):
            return getattr(self._st, k)

    def fake_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        return St(st, pa.SF_DATALESS) if str(p) == str(evicted) else st
    monkeypatch.setattr(pa.os, "stat", fake_stat)
    assert pa.is_dataless(str(evicted)) and not pa.is_dataless(str(good))
    surfaces = w(tmp_path / "s.json", json.dumps({"surfaces": [
        {"id": "r", "label": "r", "harness": "multi", "kind": "instructions", "globs": [str(tmp_path / "*" / "AGENTS.md")]}]}))
    inv = pa.inventory(str(surfaces))
    assert [f["path"] for f in inv["files"]] == [str(good)]
    assert inv["skipped"] == [{"path": str(evicted), "reason": "dataless (cloud placeholder, not downloaded)"}]
