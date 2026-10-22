#!/usr/bin/env python3
"""prompt-audit collector: deterministic, read-only evidence for a multi-lab prompt audit.

Subcommands
  fetch-guidance  download every lab source in references/labs.json into a dated cache
  inventory       resolve surfaces -> files, readers (target models), ownership
  scan            pattern + drift pass over the inventory -> candidates.json
  report          merge candidates with a triage file -> private.md, public.md, proposed.diff
  leakcheck       fail if a public artifact contains any denylisted term

The collector never edits an audited file and never executes a command it finds.
Judgment (is this line load-bearing?) belongs to the model running the skill; this
script only gathers evidence and renders what the model decided.
Stdlib only (PyYAML optional, not required).
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import fnmatch
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LABS_FILE = ROOT / "references" / "labs.json"
RULES_FILE = ROOT / "references" / "rules.json"
HOME = os.path.expanduser("~")

# --------------------------------------------------------------------------- utils


def jload(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jdump(obj, p):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def today() -> str:
    return dt.date.today().isoformat()


# ------------------------------------------------------------------ lab guidance


ASSET_RE = re.compile(r"\.(png|webp|jpe?g|svg|gif)$|texture|-models$|-preview-\d\d-\d{4}$")


class Redirect308(urllib.request.HTTPRedirectHandler):
    """Python < 3.11 does not follow 308 Permanent Redirect; docs hosts use it when pages move."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


def build_opener():
    return urllib.request.build_opener(Redirect308())


def fetch_guidance(cache_dir: str, labs_file=LABS_FILE, opener=None) -> dict:
    """Fetch every registered source. Returns manifest with status per source."""
    labs = jload(labs_file)
    stale_days = labs.get("stale_after_days", 45)
    if opener is None:
        http = build_opener()

        def default_opener(url):
            with http.open(urllib.request.Request(url, headers={"User-Agent": "prompt-audit/0.2"}), timeout=30) as r:
                return r.geturl(), r.read()
        opener = default_opener
    out = {"fetched_on": today(), "sources": []}
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    for lab, spec in labs["labs"].items():
        for src in spec.get("sources", []):
            rec = {"lab": lab, **src}
            age = (dt.date.today() - dt.date.fromisoformat(src["verified_on"])).days
            rec["verified_age_days"] = age
            rec["verified_stale"] = age > stale_days
            try:
                body = opener(src["url"])
                if isinstance(body, tuple):  # (final_url, bytes) from the default opener
                    final, body = body
                    if final and final != src["url"]:
                        rec["redirected_to"] = final  # update labs.json so the registry stays current
                text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
                path = cache / f"{src['id']}.txt"
                path.write_text(text, encoding="utf-8")
                rec.update(status="ok", bytes=len(text), sha256=sha(text), path=str(path))
                if src.get("model_id_regex"):
                    ids = set(re.findall(src["model_id_regex"], text))
                    rec["model_ids"] = sorted(i.rstrip(".-") for i in ids if not ASSET_RE.search(i))
            except Exception as e:  # network errors are data, not crashes
                rec.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
            out["sources"].append(rec)
    jdump(out, cache / "manifest.json")
    return out


def lab_for_model(model: str, labs: dict) -> str:
    m = model.lower()
    for lab, spec in labs["labs"].items():
        for pat in spec.get("match", []):
            if re.search(pat, m):
                return lab
    return "unknown"


# ------------------------------------------------------------- config readers
# Read only the keys that carry model / effort. Values of other keys are never
# returned, so a secret stored beside them cannot reach a report.

MODEL_KEY_RE = re.compile(r"^(\s*)(default|model|provider|reasoning_effort|model_reasoning_effort|effortLevel)\s*[:=]\s*\"?([^\"#\n]+?)\"?\s*$")


def read_hermes_config(path: str) -> dict:
    """Return {'model': top-level model.default, 'effort': agent reasoning_effort, 'aux': {role: model}}."""
    res = {"model": None, "provider": None, "effort": None, "aux": {}}
    if not os.path.exists(path):
        return res
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    section, sub = None, None
    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent == 0:
            section = ln.split(":")[0].strip()
            sub = None
            continue
        if section == "auxiliary" and indent == 2 and ln.strip().endswith(":"):
            sub = ln.strip()[:-1]
            continue
        m = MODEL_KEY_RE.match(ln)
        if not m:
            continue
        key, val = m.group(2), m.group(3).strip().strip("'")
        if section == "model" and key == "default":
            res["model"] = val
        elif section == "model" and key == "provider":
            res["provider"] = val
        elif key == "reasoning_effort" and res["effort"] is None:
            res["effort"] = val
        elif section == "auxiliary" and sub and key == "model":
            res["aux"][sub] = val
    return res


def read_codex_config(path: str) -> dict:
    res = {"model": None, "effort": None}
    if not os.path.exists(path):
        return res
    for ln in open(path, encoding="utf-8", errors="replace"):
        if ln.startswith("["):
            break  # only top-level keys
        m = re.match(r'^\s*(model|model_reasoning_effort)\s*=\s*"([^"]+)"', ln)
        if m:
            res["model" if m.group(1) == "model" else "effort"] = m.group(2)
    return res


def read_claude_settings(path: str) -> dict:
    res = {"model": None, "effort": None, "model_settings": {}, "always_thinking": None}
    if not os.path.exists(path):
        return res
    try:
        data = jload(path)
    except Exception:
        return res
    res["model"] = data.get("model")
    res["effort"] = data.get("effortLevel")
    res["always_thinking"] = data.get("alwaysThinkingEnabled")
    ms = data.get("modelSettings") or {}
    res["model_settings"] = {k: {"effortLevel": (v or {}).get("effortLevel")} for k, v in ms.items() if isinstance(v, dict)}
    return res


def read_omp_config(path: str) -> dict:
    res = {"roles": {}, "fallbacks": {}}
    if not os.path.exists(path):
        return res
    section, sub = None, None
    for ln in open(path, encoding="utf-8", errors="replace").read().splitlines():
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        s = ln.strip()
        if indent == 0:
            section = s.split(":")[0]
            continue
        if section == "modelRoles" and indent == 2 and ":" in s:
            k, v = s.split(":", 1)
            res["roles"][k.strip()] = v.strip()
        if section == "retry":
            if indent == 4 and s.endswith(":"):
                sub = s[:-1]
                res["fallbacks"].setdefault(sub, [])
            elif indent >= 6 and s.startswith("- ") and sub:
                res["fallbacks"][sub].append(s[2:].strip())
    return res


def strip_model(m: str | None) -> str | None:
    """'openai-codex/gpt-6-astra:high' -> 'gpt-6-astra'; 'claude-opus-5-5[1m]' -> 'claude-opus-5-5'."""
    if not m:
        return m
    m = m.split("/")[-1]
    m = re.sub(r"\[.*?\]$", "", m)
    m = m.split(":")[0]
    return m.strip()


def resolve_readers(readers: list, env: dict) -> list:
    """Readers spec -> list of {model, lab, via, effort}."""
    labs = env["labs"]
    out = []
    for r in readers:
        t = r["type"]
        if t == "static":
            out.append({"model": r["model"], "via": "static", "effort": r.get("effort")})
        elif t == "hermes_profile":
            c = read_hermes_config(expand(r["config"]))
            if c["model"]:
                out.append({"model": strip_model(c["model"]), "via": f"hermes:{r.get('profile','default')}", "effort": c["effort"]})
        elif t == "codex":
            c = read_codex_config(expand(r["config"]))
            if c["model"]:
                out.append({"model": c["model"], "via": "codex", "effort": c["effort"]})
        elif t == "claude_code":
            c = read_claude_settings(expand(r["config"]))
            if c["model"]:
                mid = strip_model(c["model"])
                eff = (c["model_settings"].get(mid) or {}).get("effortLevel") or c["effort"]
                out.append({"model": mid, "via": "claude-code", "effort": eff})
        elif t == "omp":
            c = read_omp_config(expand(r["config"]))
            for role in r.get("roles", ["default"]):
                v = c["roles"].get(role)
                if v:
                    eff = v.split(":")[1] if ":" in v.split("/")[-1] else None
                    out.append({"model": strip_model(v), "via": f"omp:{role}", "effort": eff})
    seen, uniq = set(), []
    for o in out:
        o["lab"] = lab_for_model(o["model"], labs)
        key = (o["model"], o["via"])
        if key not in seen:
            seen.add(key)
            uniq.append(o)
    return uniq


# ----------------------------------------------------------------- inventory


def load_vendor_names(patterns: list) -> set:
    names = set()
    for pat in patterns:
        for p in glob.glob(expand(pat)):
            try:
                if p.endswith(".json"):
                    data = jload(p)
                    names.update((data.get("installed") or {}).keys())
                else:
                    for ln in open(p, encoding="utf-8", errors="replace"):
                        n = ln.split(":")[0].strip()
                        if n:
                            names.add(n)
            except Exception:
                continue
    return names


SF_DATALESS = 0x40000000  # macOS: file content evicted to the cloud; open() blocks until it downloads


def is_dataless(path: str) -> bool:
    try:
        return bool(getattr(os.stat(path), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


def is_third_party_pack(path: str, stop: str) -> str | None:
    """A skill inside a directory that ships its own LICENSE and package manifest is vendored."""
    d = os.path.dirname(path)
    stop = os.path.abspath(stop)
    while d.startswith(stop) and d != stop:
        if os.path.exists(os.path.join(d, "LICENSE")) and (
            os.path.exists(os.path.join(d, "package.json")) or os.path.isdir(os.path.join(d, ".git"))):
            return os.path.basename(d)
        d = os.path.dirname(d)
    return None


def skill_name(path: str) -> str:
    try:
        head = open(path, encoding="utf-8", errors="replace").read(2000)
        m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", head, re.M)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return os.path.basename(os.path.dirname(path))


def inventory(surfaces_file: str, labs_file=LABS_FILE) -> dict:
    cfg = jload(surfaces_file)
    env = {"labs": jload(labs_file)}
    vendor = load_vendor_names(cfg.get("vendor_manifests", []))
    exclude = cfg.get("exclude_globs", [])
    files, by_hash, skipped = [], {}, []
    for s in cfg["surfaces"]:
        readers = resolve_readers(s.get("readers", []), env)
        matched = []
        for g in s["globs"]:
            matched.extend(glob.glob(expand(g), recursive=True))
        for p in sorted(set(matched)):
            if any(fnmatch.fnmatch(p, expand(x)) for x in exclude):
                continue
            if not os.path.isfile(p):
                continue
            if is_dataless(p):
                skipped.append({"path": p, "reason": "dataless (cloud placeholder, not downloaded)"})
                continue
            try:
                text = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            h = sha(text)
            owner = "authored"
            if s["kind"] == "skill":
                name = skill_name(p)
                pack = is_third_party_pack(p, expand(s.get("root", os.path.dirname(p))))
                if pack:
                    owner = f"vendor:{pack}"
                elif name in vendor or os.path.basename(os.path.dirname(p)) in vendor:
                    owner = "vendor:bundled"
            rec = {"path": p, "surface": s["id"], "surface_label": s["label"], "harness": s["harness"],
                   "kind": s["kind"], "always_loaded": s.get("always_loaded", False),
                   "readers": readers, "owner": owner, "sha256": h, "chars": len(text),
                   "lines": text.count("\n") + 1}
            if h in by_hash:
                kept = by_hash[h]
                kept["copies"].append(p)
                have = {(r["model"], r["via"]) for r in kept["readers"]}
                kept["readers"] += [r for r in readers if (r["model"], r["via"]) not in have]
                kept["always_loaded"] = kept["always_loaded"] or rec["always_loaded"]
                continue
            rec["copies"] = [p]
            by_hash[h] = rec
            files.append(rec)
    return {"generated_on": today(), "files": files, "skipped": skipped,
            "configs": collect_configs(cfg.get("configs", {}))}


def collect_configs(spec: dict) -> dict:
    out = {"hermes": {}, "codex": None, "claude_code": None, "omp": None}
    for name, path in (spec.get("hermes") or {}).items():
        out["hermes"][name] = read_hermes_config(expand(path))
    if spec.get("codex"):
        out["codex"] = read_codex_config(expand(spec["codex"]))
    if spec.get("claude_code"):
        out["claude_code"] = read_claude_settings(expand(spec["claude_code"]))
    if spec.get("omp"):
        out["omp"] = read_omp_config(expand(spec["omp"]))
    return out


# ---------------------------------------------------------------------- scan

FENCE_RE = re.compile(r"^\s*(```|~~~)")
CACHE_CONTEXT_RE = re.compile(r"\b(changelog|history|historical|examples?|samples?|demo)\b", re.I)
CACHE_SECTION_RE = re.compile(r"^(?:changelog|history|historical|examples?|samples?|demos?)\b", re.I)


def cache_lines(text: str):
    """Exclude examples and history, including their nested Markdown sections."""
    excluded_level = None
    for i, ln, zone in body_lines(text):
        if zone == "fence":
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)", ln)
        if heading:
            level = len(heading[1])
            if excluded_level is not None and level <= excluded_level:
                excluded_level = None
            if level > 1 and excluded_level is None and CACHE_SECTION_RE.match(heading[2]):
                excluded_level = level
            continue
        if excluded_level is not None or re.match(r"^\s*>", ln):
            continue
        if re.match(r"^(?: {4}|\t)", ln) and not re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", ln):
            continue
        if CACHE_CONTEXT_RE.search(ln):
            continue
        yield i, ln


def body_lines(text: str):
    """Yield (lineno, line, zone) with zone in {frontmatter, fence, body}."""
    lines = text.splitlines()
    in_fm = bool(lines) and lines[0].strip() == "---"
    in_fence = False
    for i, ln in enumerate(lines, 1):
        if in_fm:
            if i > 1 and ln.strip() == "---":
                in_fm = False
            yield i, ln, "frontmatter"
            continue
        if FENCE_RE.match(ln):
            in_fence = not in_fence
            yield i, ln, "fence"
            continue
        yield i, ln, "fence" if in_fence else "body"


def frontmatter_description(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    fm = text[3:end] if end > 0 else ""
    m = re.search(r"^description:\s*(.*?)(?=^\w[\w-]*:|\Z)", fm, re.M | re.S)
    if not m:
        return ""
    d = m.group(1).strip()
    d = re.sub(r"^[|>][-+]?\s*", "", d)
    return re.sub(r"\s+", " ", d).strip().strip("'\"")


CONF_ORDER = ["low", "medium", "high"]


def downgrade(c: str, n: int = 1) -> str:
    return CONF_ORDER[max(0, CONF_ORDER.index(c) - n)]


def rule_applies(rule: dict, reader_labs: set) -> tuple[bool, str | None]:
    """Cross-lab rule resolution. Returns (applies, note)."""
    labs = set(rule.get("labs", ["all"]))
    if "all" in labs:
        return True, None
    hit = labs & reader_labs
    if not hit:
        return False, None
    contra = set(rule.get("contradicted_by", [])) & reader_labs
    if contra:
        return True, f"guidance differs for {', '.join(sorted(contra))} readers"
    others = reader_labs - labs - {"unknown", "open-weights"}
    if others:
        return True, f"lab-specific ({', '.join(sorted(hit))}); file is also read by {', '.join(sorted(others))}"
    return True, None


def scan_file(rec: dict, rules: list) -> list:
    try:
        text = open(rec["path"], encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    reader_labs = {r["lab"] for r in rec["readers"]} or {"unknown"}
    out = []
    for rule in rules:
        if rule.get("kind") == "cache_volatility" and not rec.get("always_loaded"):
            continue
        if rule.get("scope") and rec["kind"] not in rule["scope"]:
            continue
        ok, note = rule_applies(rule, reader_labs)
        if not ok:
            continue
        conf = rule["confidence"]
        if note and "differs" in note:
            conf = "low"
        elif note:
            conf = downgrade(conf)
        if rec["owner"] != "authored":
            conf_action = "flag"  # vendor text: report, edit upstream
        else:
            conf_action = rule["action"]
        if rule.get("kind") == "description_length":
            d = frontmatter_description(text)
            if len(d) > rule["max_chars"]:
                out.append(finding(rec, rule, 2, f"description is {len(d)} chars", conf, conf_action, note))
            continue
        if rule.get("kind") == "file_size":
            if rec["chars"] > rule["max_chars"] and not os.path.isdir(os.path.join(os.path.dirname(rec["path"]), "references")):
                out.append(finding(rec, rule, 1, f"{rec['chars']} chars, no references/ split", conf, conf_action, note))
            continue
        if rule.get("kind") == "caps_density":
            n = len(re.findall(r"\b(MUST|NEVER|ALWAYS|CRITICAL|IMPORTANT|DO NOT)\b", text))
            per_k = n / max(1, rec["chars"]) * 1000
            if n >= rule["min_count"] and per_k >= rule["min_per_1k"]:
                out.append(finding(rec, rule, 1, f"{n} caps markers ({per_k:.1f}/1k chars)", conf, conf_action, note))
            continue
        rx = re.compile(rule["regex"], 0 if rule.get("case_sensitive") else re.I)
        if rule.get("kind") == "cache_volatility":
            total_lines = max(1, len(text.splitlines()))
            for i, ln in cache_lines(text):
                m = rx.search(ln)
                if not m:
                    continue
                position = (i - 1) / total_lines
                candidate = finding(rec, rule, i, ln.strip()[:240], conf, "flag", note, match=m.group(0))
                candidate.update(line_fraction=round(position, 4),
                                 prefix_impact=round(1 - position, 4), total_lines=total_lines)
                out.append(candidate)
            continue
        zones = set(rule.get("zones", ["body"]))
        unless = re.compile(rule["unless"], re.I) if rule.get("unless") else None
        hits = 0
        for i, ln, zone in body_lines(text):
            if zone not in zones:
                continue
            m = rx.search(ln)
            if not m or (unless and unless.search(ln)):
                continue
            out.append(finding(rec, rule, i, ln.strip()[:240], conf, conf_action, note, match=m.group(0)))
            hits += 1
            if hits >= rule.get("max_per_file", 6):
                break
    return out


def finding(rec, rule, line, evidence, conf, action, note, match=None):
    return {"id": None, "path": rec["path"], "line": line, "surface": rec["surface"],
            "surface_label": rec["surface_label"], "harness": rec["harness"], "owner": rec["owner"],
            "always_loaded": rec["always_loaded"], "copies": len(rec.get("copies", [])),
            "rule": rule["id"], "group": rule["group"], "category": rule["category"],
            "pattern": rule["pattern"], "why": rule["why"], "sources": rule.get("sources", []),
            "confidence": conf, "action": action, "cross_lab_note": note,
            "evidence": evidence, "match": match or evidence[:60],
            "readers": sorted({f"{r['model']}" for r in rec["readers"]})}


# ------------------------------------------------------------------- drift pass

MODEL_MENTION_RE = re.compile(
    r"\b(claude[- ](?:opus|sonnet|haiku|fable|mythos)[- ]\d+(?:[.-]\d+)?|"
    r"(?:opus|sonnet|haiku|fable|mythos)[- ]\d+(?:\.\d+)?|"
    r"gpt-\d+(?:\.\d+)?(?:-[a-z]+)?|gemini-\d+(?:\.\d+)?-[a-z]+)\b", re.I)


def model_key(mention: str):
    """-> (family, version tuple) for comparison, e.g. ('opus', (5,5)), ('gpt-sol', (6,1)).

    Tiers are separate families: labs version tiers independently (gpt-6.1-sol ships while
    gpt-6-astra stays current), so comparing across tiers would flag current models as stale.
    A bare 'gpt-6' / 'GPT-6' is family 'gpt' and compares on the major generation only."""
    s = mention.lower().replace(" ", "-")
    s = s.replace("claude-", "")
    m = re.match(r"(opus|sonnet|haiku|fable|mythos)-(\d+)(?:[.-](\d+))?", s)
    if m:
        return m.group(1), (int(m.group(2)), int(m.group(3) or 0))
    m = re.match(r"gpt-(\d+)(?:\.(\d+))?(?:-([a-z]+))?", s)
    if m:
        return (f"gpt-{m.group(3)}" if m.group(3) else "gpt"), (int(m.group(1)), int(m.group(2) or 0))
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?-([a-z]+)", s)
    if m:
        return f"gemini-{m.group(3)}", (int(m.group(1)), int(m.group(2) or 0))
    return None, None


def current_versions(models: list) -> dict:
    cur = {}
    for m in models:
        fam, ver = model_key(m)
        if fam and (fam not in cur or ver > cur[fam]):
            cur[fam] = ver
    return cur


def is_stale(mention: str, cur: dict) -> str | None:
    """Newer version label if `mention` is older than the current generation, else None.
    Same tier: full version compare. Bare name or a tier with no catalogued version: compare
    the major generation against the newest of any tier in that lab family."""
    fam, ver = model_key(mention or "")
    if not fam:
        return None
    if fam in cur and fam != "gpt":
        return ".".join(map(str, cur[fam])) if ver < cur[fam] else None
    group = fam.split("-")[0]
    majors = [v for f, v in cur.items() if f.split("-")[0] == group]
    if not majors:
        return None
    top = max(majors)
    return f"{top[0]}.x" if ver[0] < top[0] else None


PATH_RE = re.compile(r"(?<![\w/.])((?:~|\$HOME|/Users|/home|/opt|/etc)/[\w@%+=:,./-]+)")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
PLACEHOLDER_RE = re.compile(r"[<>{}*]|\$\{|example|/path/to|/foo|xxx|YOUR_", re.I)


DATE_TOKEN_RE = re.compile(r"YYYY|MM-DD|Www|\bDD\b|\d{4}-\d{2}-\d{2}")


def classify_path(p: str):
    """None if the path resolves (or only its generated leaf is missing).
    Otherwise (level, first_missing_component) with level in root|intermediate|tooldir."""
    if os.path.lexists(p):
        return None
    parts = Path(p).parts
    base = Path(HOME) if p.startswith(HOME) else Path(parts[0])
    rel = Path(p).relative_to(base).parts
    # stop before any templated component: the part before it must exist
    cut = next((i for i, c in enumerate(rel) if DATE_TOKEN_RE.search(c)), len(rel))
    rel = rel[:cut]
    if not rel:
        return None
    cur = base
    for depth, comp in enumerate(rel):
        nxt = cur / comp
        if not os.path.lexists(nxt):
            is_leaf = depth == len(rel) - 1 and cut == len(Path(p).relative_to(base).parts)
            if is_leaf and depth > 0:
                return None  # parent exists; leaf is a file the workflow creates
            shown = "~/" + str(nxt.relative_to(HOME)) if str(nxt).startswith(HOME) else str(nxt)
            if depth == 0:
                return ("tooldir" if comp.startswith(".") else "root", shown)
            return ("intermediate", shown)
        cur = nxt
    return None


def drift_file(rec: dict, env: dict) -> list:
    out = []
    try:
        text = open(rec["path"], encoding="utf-8", errors="replace").read()
    except Exception:
        return out
    authored = rec["owner"] == "authored"
    cur = env["current_versions"]
    vault = env.get("vault_root")
    seen_paths = set()
    for i, ln, zone in body_lines(text):
        if zone == "frontmatter":
            continue
        # model mentions older than the current generation of the same family
        for m in MODEL_MENTION_RE.finditer(ln):
            fam, _ = model_key(m.group(0))
            newer = is_stale(m.group(0), cur)
            if not newer:
                continue
            if re.search(r"\b(retired|deprecated|legacy|older|previous|from|vs\.?|than|migrat|history|was)\b", ln, re.I):
                continue  # the line is already about the old model
            conf = "medium" if rec["always_loaded"] and authored else "low"
            out.append(drift(rec, i, "model-id", ln, m.group(0), conf,
                             f"names {m.group(0)}; the newest configured or catalogued {fam} is "
                             f"{newer}. Pinned names silently go stale when the alias or default moves.",
                             "rewrite" if authored else "flag"))
        # absolute / home paths that do not resolve
        for m in PATH_RE.finditer(ln):
            raw = m.group(1).rstrip(".,:;)`'\"")
            if raw in seen_paths or PLACEHOLDER_RE.search(raw) or zone == "fence":
                continue
            seen_paths.add(raw)
            p = expand(raw)
            if not p.startswith(HOME) and not p.startswith("/opt") and not p.startswith("/etc"):
                continue  # another user's home: not ours to probe
            dep = next((d for d in env.get("deprecated_paths", []) if p.startswith(expand(d["path"]))), None)
            if dep:
                out.append(drift(rec, i, "deprecated", ln, raw, "high" if authored else "low",
                                 f"path resolves but is declared retired: {dep['reason']}",
                                 "rewrite" if authored else "flag"))
                continue
            verdict = classify_path(p)
            if verdict is None:
                continue
            level, missing = verdict
            if level == "root":
                conf, why = ("high" if authored else "low"), (
                    f"root {missing} does not exist on this host, so every path under it is dead.")
            elif level == "intermediate":
                conf, why = ("medium" if authored else "low"), (
                    f"{missing} does not exist; the tree was likely reorganised after this was written.")
            else:  # tool-owned dot directory: the tool may simply not be installed here
                conf, why = "low", f"{missing} (tool-owned state dir) is absent; the tool may not be installed on this host."
            out.append(drift(rec, i, "path", ln, raw, conf, why, "rewrite" if authored and conf != "low" else "flag"))
        # vault wikilinks
        if vault and rec.get("vault_links"):
            for m in WIKILINK_RE.finditer(ln):
                target = m.group(1).strip()
                if PLACEHOLDER_RE.search(target) or target in seen_paths:
                    continue
                seen_paths.add(target)
                cand = [os.path.join(vault, target), os.path.join(vault, target + ".md")]
                if any(os.path.exists(c) for c in cand):
                    continue
                base = os.path.basename(target)
                if env["vault_basenames"].get(base) or env["vault_basenames"].get(base + ".md"):
                    continue
                out.append(drift(rec, i, "wikilink", ln, f"[[{target}]]", "medium",
                                 "wikilink target does not resolve in the vault.", "rewrite"))
    return out


def drift(rec, line, kind, evidence, match, conf, why, action):
    return {"id": None, "path": rec["path"], "line": line, "surface": rec["surface"],
            "surface_label": rec["surface_label"], "harness": rec["harness"], "owner": rec["owner"],
            "always_loaded": rec["always_loaded"], "copies": len(rec.get("copies", [])),
            "rule": f"drift-{kind}", "group": "D", "category": f"drift:{kind}",
            "pattern": {"model-id": "Drift - pinned or older model ID", "path": "Drift - named path does not exist",
                        "wikilink": "Drift - vault link does not resolve",
                        "deprecated": "Drift - path points at a retired location"}[kind],
            "why": why, "sources": ["anthropic-audit-method#group-2-volatile-specifics"],
            "confidence": conf, "action": action, "cross_lab_note": None,
            "evidence": evidence.strip()[:240], "match": match,
            "readers": sorted({r["model"] for r in rec["readers"]})}


def config_drift(inv: dict, env: dict) -> list:
    """Configured-vs-running checks that live in harness config, not prose."""
    out = []
    cfg = inv["configs"]
    cur = env["current_versions"]

    def cfind(cid, path, key, evidence, conf, why, action, category):
        out.append({"id": None, "path": path, "line": 0, "surface": "config", "surface_label": "harness config",
                    "harness": cid, "owner": "authored", "always_loaded": True, "copies": 1,
                    "rule": f"drift-{category}", "group": "D", "category": f"drift:{category}",
                    "pattern": {"effort": "Drift - effort override keyed to a model not in use",
                                "model": "Drift - configured model older than current generation",
                                "aux": "Drift - profiles disagree on the same auxiliary role",
                                "truncation": "Drift - generated instruction block cut mid-sentence"}[category],
                    "why": why, "sources": ["anthropic-whats-new-opus-5-5", "anthropic-audit-method#group-4"],
                    "confidence": conf, "action": action, "cross_lab_note": None,
                    "evidence": evidence, "match": key, "readers": []})

    cc = cfg.get("claude_code") or {}
    if cc.get("model"):
        active = strip_model(cc["model"])
        for key, v in (cc.get("model_settings") or {}).items():
            if key != active:
                cfind("claude-code", env["paths"].get("claude_code", "claude settings"), f"modelSettings.{key}",
                      f'"modelSettings": {{"{key}": {{"effortLevel": "{v.get("effortLevel")}"}}}} (active model: {active})',
                      "medium", f"override is keyed to {key}; the configured model is {active}. Keys match exact model IDs, "
                      "so this entry applies to nothing unless that model is selected by hand.", "remove", "effort")
    # generated managed blocks that end mid-sentence (budget truncation)
    for f in inv["files"]:
        if not f["always_loaded"]:
            continue
        try:
            text = open(f["path"], encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in re.finditer(r"([^\n]*?)(…|\.\.\.)\s*\n\s*<!--\s*[\w-]+ END", text):
            line = text.count("\n", 0, m.start()) + 1
            cfind(f["harness"], f["path"], "managed-block", m.group(1).strip()[-120:] + m.group(2), "high",
                  "the generated block hits its character budget and is cut mid-rule; every session loads a "
                  "half-sentence instruction and loses whatever followed.", "rewrite", "truncation")
            out[-1]["line"] = line
            out[-1]["copies"] = len(f["copies"])
    # older-generation configured models (primary, fallback, aux)
    models = []
    for name, h in cfg.get("hermes", {}).items():
        for role, m in (h.get("aux") or {}).items():
            models.append((f"hermes:{name}", f"auxiliary.{role}", m, env["paths"]["hermes"].get(name, name)))
    for role, v in (cfg.get("omp") or {}).get("roles", {}).items():
        models.append(("omp", f"modelRoles.{role}", strip_model(v), env["paths"].get("omp", "omp")))
    for src, chain in (cfg.get("omp") or {}).get("fallbacks", {}).items():
        for v in chain:
            models.append(("omp", f"fallbackChains.{src}", strip_model(v), env["paths"].get("omp", "omp")))
    for cid, key, m, path in models:
        fam, _ = model_key(m or "")
        newer = is_stale(m or "", cur)
        if newer:
            cfind(cid, path, key, f"{key}: {m}", "medium",
                  f"{m} is older than the newest configured or catalogued {fam} ({newer}).",
                  "flag", "model")
    # auxiliary role disagreement across Hermes profiles
    roles = {}
    for name, h in cfg.get("hermes", {}).items():
        for role, m in (h.get("aux") or {}).items():
            roles.setdefault(role, {}).setdefault(m, []).append(name)
    for role, by_model in roles.items():
        if len(by_model) > 1:
            desc = "; ".join(f"{m}: {', '.join(sorted(p))}" for m, p in sorted(by_model.items()))
            cfind("hermes", "hermes profile configs", f"auxiliary.{role}", desc, "medium",
                  f"profiles resolve auxiliary.{role} to different models; the older one is likely a copied default "
                  "that never moved.", "flag", "aux")
    return out


def fork_drift(inv: dict) -> list:
    """Same skill name, authored, present in several places with different content.
    Each fork is read by a different agent, so one fix does not reach the others."""
    by_name = {}
    for f in inv["files"]:
        if f["kind"] != "skill" or f["owner"] != "authored":
            continue
        by_name.setdefault(skill_name(f["path"]), []).append(f)
    out = []
    for name, recs in sorted(by_name.items()):
        if len(recs) < 2:
            continue
        paths = sorted(p for r in recs for p in r["copies"])
        newest = max(recs, key=lambda r: max(os.path.getmtime(p) for p in r["copies"]))
        out.append({"id": None, "path": newest["path"], "line": 0, "surface": "forks", "surface_label": "skill forks",
                    "harness": "multi", "owner": "authored", "always_loaded": False, "copies": len(paths),
                    "rule": "drift-fork", "group": "D", "category": "drift:fork",
                    "pattern": "Drift - one skill, divergent forks across profiles/harnesses",
                    "why": f"{len(recs)} different versions of '{name}' across {len(paths)} files; a fix made in one fork "
                           "does not reach agents that load another.",
                    "sources": ["anthropic-audit-method#group-2-contradictions"], "confidence": "medium", "action": "flag",
                    "cross_lab_note": None, "evidence": f"{len(recs)} variants / {len(paths)} files",
                    "match": name, "readers": sorted({r["model"] for x in recs for r in x["readers"]}),
                    "fork_paths": paths})
    return out


def shadow_drift(inv: dict) -> list:
    """Codex reads AGENTS.override.md instead of AGENTS.md in the same directory (only one file per
    directory, first non-empty wins). An audited AGENTS.md beside a non-empty override never reaches
    Codex, so its rules are dead for that reader and any fix made there does nothing."""
    out = []
    for f in inv["files"]:
        if os.path.basename(f["path"]) != "AGENTS.md":
            continue
        if not any(r.get("via") == "codex" for r in f.get("readers", [])):
            continue
        ov = os.path.join(os.path.dirname(f["path"]), "AGENTS.override.md")
        try:
            live = os.path.isfile(ov) and open(ov, encoding="utf-8", errors="replace").read().strip()
        except Exception:
            live = False
        if not live:
            continue
        out.append({"id": None, "path": f["path"], "line": 0, "surface": f.get("surface", "codex"),
                    "surface_label": f.get("surface_label", "Codex AGENTS.md"), "harness": "codex",
                    "owner": f.get("owner", "authored"), "always_loaded": True, "copies": len(f.get("copies", [])),
                    "rule": "drift-shadow", "group": "D", "category": "drift:shadow",
                    "pattern": "Drift - AGENTS.md shadowed by AGENTS.override.md",
                    "why": "Codex uses AGENTS.override.md in place of AGENTS.md in the same directory, so this "
                           "file is not loaded for Codex. Either the override is a forgotten temporary file or "
                           "these rules are dead for Codex readers.",
                    "sources": ["openai-codex-agents-md"], "confidence": "high", "action": "flag",
                    "cross_lab_note": None, "evidence": "AGENTS.override.md present and non-empty",
                    "match": "AGENTS.override.md", "readers": sorted({r["model"] for r in f["readers"]})})
    return out


def build_env(inv: dict, labs_file=LABS_FILE, guidance_manifest=None, vault_root=None, paths=None) -> dict:
    configured = []
    for f in inv["files"]:
        configured += [r["model"] for r in f["readers"]]
    cfg = inv["configs"]
    for h in cfg.get("hermes", {}).values():
        configured += [h.get("model")] + list((h.get("aux") or {}).values())
    for k in ("codex", "claude_code"):
        if cfg.get(k):
            configured.append(strip_model(cfg[k].get("model")))
    for v in (cfg.get("omp") or {}).get("roles", {}).values():
        configured.append(strip_model(v))
    catalog = []
    if guidance_manifest and os.path.exists(guidance_manifest):
        for s in jload(guidance_manifest)["sources"]:
            catalog += s.get("model_ids", [])
    env = {"labs": jload(labs_file), "configured": sorted({strip_model(m) for m in configured if m}),
           "catalog": sorted(set(catalog)), "vault_root": vault_root, "paths": paths or {"hermes": {}}}
    env["current_versions"] = current_versions(env["configured"] + env["catalog"])
    basenames = {}
    if vault_root and os.path.isdir(vault_root):
        for dp, dn, fn in os.walk(vault_root):
            dn[:] = [d for d in dn if not d.startswith(".")]
            for f in fn:
                basenames[f] = True
                basenames[os.path.splitext(f)[0]] = True
            for d in dn:
                basenames[d] = True
    env["vault_basenames"] = basenames
    return env


def git_provenance(path: str, line: int) -> str | None:
    if line <= 0:
        return None
    d = os.path.dirname(path)
    try:
        top = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None
        r = subprocess.run(["git", "-C", d, "blame", "--porcelain", "-L", f"{line},{line}", "--", path],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout:
            return None
        commit = r.stdout.split()[0][:8]
        t = re.search(r"^author-time (\d+)", r.stdout, re.M)
        when = dt.datetime.fromtimestamp(int(t.group(1))).date().isoformat() if t else "?"
        return f"{commit} {when}"
    except Exception:
        return None


def scan(inventory_file: str, out_file: str, rules_file=RULES_FILE, labs_file=LABS_FILE,
         guidance_manifest=None, vault_root=None, surfaces_file=None, blame=True) -> dict:
    inv = jload(inventory_file)
    rules = jload(rules_file)["rules"]
    paths = {"hermes": {}}
    if surfaces_file:
        c = jload(surfaces_file).get("configs", {})
        paths = {"hermes": {k: expand(v) for k, v in (c.get("hermes") or {}).items()},
                 **{k: expand(v) for k, v in c.items() if isinstance(v, str)}}
    env = build_env(inv, labs_file, guidance_manifest, vault_root, paths)
    vault_surfaces = set((jload(surfaces_file).get("vault_link_surfaces", []) if surfaces_file else []))
    env["deprecated_paths"] = jload(surfaces_file).get("deprecated_paths", []) if surfaces_file else []
    cands = []
    for rec in inv["files"]:
        rec["vault_links"] = rec["surface"] in vault_surfaces
        cands += scan_file(rec, rules)
        cands += drift_file(rec, env)
    cands += config_drift(inv, env)
    cands += fork_drift(inv)
    cands += shadow_drift(inv)
    rank = {"high": 0, "medium": 1, "low": 2}
    cands.sort(key=lambda c: (rank[c["confidence"]], not c["always_loaded"], c["owner"] != "authored", c["path"], c["line"]))
    for n, c in enumerate(cands, 1):
        c["id"] = f"C{n:04d}"
        if blame and c["owner"] == "authored" and c["confidence"] != "low":
            c["provenance"] = git_provenance(c["path"], c["line"])
    res = {"generated_on": today(), "env": {k: env[k] for k in ("configured", "catalog", "current_versions")},
           "counts": summarize(cands), "candidates": cands}
    res["env"]["current_versions"] = {k: ".".join(map(str, v)) for k, v in env["current_versions"].items()}
    jdump(res, out_file)
    return res


def summarize(cands: list) -> dict:
    by = {}
    for c in cands:
        for key in (f"category:{c['category']}", f"confidence:{c['confidence']}", f"owner:{c['owner'].split(':')[0]}",
                    f"surface:{c['surface']}"):
            by[key] = by.get(key, 0) + 1
    return dict(sorted(by.items()))


# --------------------------------------------------------------------- report


def redactor(denylist: list):
    rules = [
        (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
        (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
        (re.compile(re.escape(HOME)), "~"),
        (re.compile(r"/Users/[\w.-]+"), "~"),
        (re.compile(r"\[\[[^\]]+\]\]"), "[[<note>]]"),
    ]
    terms = sorted({t for t in denylist if t.strip()}, key=len, reverse=True)
    term_rx = re.compile("|".join(rf"\b{re.escape(t)}\b" for t in terms), re.I) if terms else None

    def red(s: str) -> str:
        if s is None:
            return s
        for rx, rep in rules:
            s = rx.sub(rep, s)
        if term_rx:
            s = term_rx.sub("<redacted>", s)
        return s
    return red


def public_location(c: dict, alias: dict) -> str:
    p = c["path"]
    for prefix, label in sorted(alias.items(), key=lambda kv: -len(kv[0])):
        ep = expand(prefix)
        if p.startswith(ep):
            rest = p[len(ep):].lstrip("/")
            return f"{label}/{rest}" if rest else label
    return c["surface_label"]


def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def render_report(triage_file: str, cands_file: str, out_dir: str, public_cfg: dict | None = None) -> dict:
    """triage.json: {assumptions, inventory_summary, findings:[{cid|custom, verdict, confidence, action,
    why, replacement, public_evidence, probe}], edits:[{finding, path, old, new}], top:[fid...]}"""
    tri = jload(triage_file)
    cands = {c["id"]: c for c in jload(cands_file)["candidates"]}
    public_cfg = public_cfg or {}
    red = redactor(public_cfg.get("denylist", []))
    alias = public_cfg.get("path_alias", {})
    rows = []
    for f in tri["findings"]:
        base = dict(cands.get(f.get("cid"), {})) if f.get("cid") else {}
        base.update({k: v for k, v in f.items() if v is not None})
        rows.append(base)
    rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (rank.get(r.get("confidence"), 3), r.get("fid", "")))
    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def table(public: bool) -> list:
        lines = ["| ID | Location | Pattern | Evidence | Why stale | Conf | Action |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            loc = (public_location(r, alias) if public else r["path"]) + (f":{r['line']}" if r.get("line") else "")
            ev = r.get("public_evidence") if public else r.get("evidence")
            ev = red(ev) if public else ev
            why = red(r["why"]) if public else r["why"]
            if r.get("cross_lab_note"):
                why += f" ({r['cross_lab_note']})"
            lines.append(f"| {r['fid']} | `{md_escape(red(loc) if public else loc)}` | {md_escape(r['pattern'])} | "
                         f"{md_escape(ev)} | {md_escape(why)} | {r['confidence']} | {r['action']} |")
        return lines

    edits = tri.get("edits", [])
    diff_priv, diff_pub, edit_errors = [], [], []
    for e in edits:
        src = expand(e["path"])
        try:
            text = open(src, encoding="utf-8").read()
        except Exception as ex:
            edit_errors.append(f"{e['finding']}: cannot read ({ex})")
            continue
        if text.count(e["old"]) != 1:
            edit_errors.append(f"{e['finding']}: old text matches {text.count(e['old'])} times in {src}")
            continue
        new = text.replace(e["old"], e["new"], 1)
        d = list(difflib.unified_diff(text.splitlines(True), new.splitlines(True),
                                      fromfile=f"a/{src}", tofile=f"b/{src}", n=1))
        header = [f"# {e['finding']}: {e.get('summary','')}\n"]
        diff_priv += header + d
        if e.get("public", True):
            loc = public_location({"path": src, "surface_label": "file"}, alias)
            pd = list(difflib.unified_diff(red(text).splitlines(True), red(new).splitlines(True),
                                           fromfile=f"a/{loc}", tofile=f"b/{loc}", n=1))
            diff_pub += header + pd
    (out / "proposed.private.diff").write_text("".join(diff_priv), encoding="utf-8")
    (out / "proposed.public.diff").write_text("".join(diff_pub), encoding="utf-8")

    def doc(public: bool) -> str:
        a = tri["assumptions"]
        s = [f"# Prompt audit dry run - {tri.get('date', today())}{' (public-safe)' if public else ' (private)'}", ""]
        s += ["Method: Anthropic's `/claude-api prompt-audit` (anthropics/skills, Apache-2.0), extended to multi-lab "
              "targets, multi-harness surfaces and a drift pass. Read-only: nothing was applied.", ""]
        s += ["## Assumptions", ""] + [f"- {red(x) if public else x}" for x in a] + [""]
        s += ["## Inventory", ""] + [f"- {red(x) if public else x}" for x in tri["inventory_summary"]] + [""]
        s += ["## Summary", ""] + [red(tri["summary"]) if public else tri["summary"], ""]
        s += ["### Counts by category (reviewed findings)", "", "| Category | Findings |", "|---|---|"]
        s += [f"| {k} | {v} |" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])] + [""]
        if tri.get("machine_counts"):
            s += ["### Collector candidates before review", ""] + [f"- {x}" for x in tri["machine_counts"]] + [""]
        s += ["## Top 5", ""]
        for fid in tri.get("top", [])[:5]:
            r = next(x for x in rows if x["fid"] == fid)
            s.append(f"{fid}. {red(r.get('headline', r['pattern'])) if public else r.get('headline', r['pattern'])}")
        s += ["", "## Findings", ""] + table(public) + [""]
        probes = [r for r in rows if r.get("probe")]
        if probes:
            s += ["## Before/after probes for risky removals", ""]
            for r in probes:
                s += [f"### {r['fid']}", "", red(r["probe"]) if public else r["probe"], ""]
        if tri.get("keep_notes"):
            s += ["## Keep notes (matched a signal, left alone)", ""]
            s += [f"- {red(x) if public else x}" for x in tri["keep_notes"]] + [""]
        s += ["## Proposed diff", "", f"See `proposed.{'public' if public else 'private'}.diff` "
              f"({len(edits)} hunks, one per finding). Nothing has been applied.", ""]
        if edit_errors and not public:
            s += ["Edit errors:", ""] + [f"- {x}" for x in edit_errors] + [""]
        s += ["## Sources", ""] + [f"- {x}" for x in tri.get("sources", [])] + [""]
        return "\n".join(s)

    (out / "report.private.md").write_text(doc(False), encoding="utf-8")
    (out / "report.public.md").write_text(doc(True), encoding="utf-8")
    return {"rows": len(rows), "edits": len(edits), "edit_errors": edit_errors, "counts": counts}


def leakcheck(paths: list, denylist: list) -> list:
    bad = []
    rx = [re.compile(rf"\b{re.escape(t)}\b", re.I) for t in denylist if t.strip()]
    rx += [re.compile(r"/Users/[\w.-]+"), re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)]
    for p in paths:
        text = open(p, encoding="utf-8").read()
        for r in rx:
            for m in r.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                bad.append(f"{p}:{line}: {r.pattern[:40]}")
    return bad


# ------------------------------------------------------------------------ cli


def main(argv=None):
    ap = argparse.ArgumentParser(prog="prompt_audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-guidance"); f.add_argument("--cache", required=True)
    i = sub.add_parser("inventory"); i.add_argument("--surfaces", required=True); i.add_argument("--out", required=True)
    s = sub.add_parser("scan"); s.add_argument("--inventory", required=True); s.add_argument("--out", required=True)
    s.add_argument("--guidance"); s.add_argument("--vault"); s.add_argument("--surfaces"); s.add_argument("--no-blame", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--triage", required=True); r.add_argument("--candidates", required=True)
    r.add_argument("--out", required=True); r.add_argument("--public-config")
    l = sub.add_parser("leakcheck"); l.add_argument("files", nargs="+"); l.add_argument("--public-config", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fetch-guidance":
        m = fetch_guidance(a.cache)
        for s_ in m["sources"]:
            print(f"{s_['status']:5} {s_['lab']:10} {s_['id']:32} {s_.get('bytes', s_.get('error',''))}"
                  f"{' STALE-VERIFY' if s_['verified_stale'] else ''}")
    elif a.cmd == "inventory":
        inv = inventory(a.surfaces)
        jdump(inv, a.out)
        n_dup = sum(len(x["copies"]) - 1 for x in inv["files"])
        print(f"{len(inv['files'])} unique files ({n_dup} duplicate copies folded)")
        for s_ in inv["skipped"]:
            print(f"skipped {s_['path']}: {s_['reason']} (download it, or list it in the report assumptions)")
    elif a.cmd == "scan":
        res = scan(a.inventory, a.out, guidance_manifest=a.guidance, vault_root=a.vault, surfaces_file=a.surfaces,
                   blame=not a.no_blame)
        print(f"{len(res['candidates'])} candidates")
        print(json.dumps(res["counts"], indent=1))
    elif a.cmd == "report":
        pc = jload(a.public_config) if a.public_config else {}
        print(json.dumps(render_report(a.triage, a.candidates, a.out, pc), indent=1))
    elif a.cmd == "leakcheck":
        bad = leakcheck(a.files, jload(a.public_config).get("denylist", []))
        print("\n".join(bad) if bad else "leakcheck: clean")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
