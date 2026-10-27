#!/usr/bin/env python3
"""Run portable checks and reject diagnostic fingerprints absent from the accepted base."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".quality" / "baseline.json"
CONFIGS = ("pyproject.toml", "requirements-quality.txt")


@dataclass(frozen=True)
class Diagnostic:
    tool: str
    rule: str
    path: str
    message: str
    source: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def command(root: Path, args: list[str], allowed: tuple[int, ...]) -> str:
    run = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if run.returncode not in allowed or run.stderr.strip():
        detail = run.stderr or run.stdout
        message = f"{' '.join(args)} exited {run.returncode}\n{detail}"
        raise RuntimeError(message)
    return run.stdout


def diagnostic(root: Path, tool: str, rule: str, file: str, line: int, message: str) -> Diagnostic:
    path = Path(file)
    if not path.is_absolute():
        path = root / path
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    lines = path.read_text(encoding="utf-8").splitlines()
    source = lines[line - 1].strip() if line else ""
    return Diagnostic(tool, rule, relative, message.replace(str(root), "<repo>"), source)


def collect(root: Path) -> list[Diagnostic]:
    python = sys.executable
    raw: list[dict[str, Any]] = json.loads(
        command(root, [python, "-m", "ruff", "check", "--output-format=json", "."], (0, 1))
    )
    found = [
        diagnostic(
            root, "ruff", row["code"], row["filename"], row["location"]["row"], row["message"]
        )
        for row in raw
    ]

    formatted = command(root, [python, "-m", "ruff", "format", "--check", "."], (0, 1))
    for file in re.findall(r"^Would reformat: (.+)$", formatted, re.MULTILINE):
        digest = hashlib.sha256((root / file).read_bytes()).hexdigest()
        found.append(Diagnostic("format", "ruff-format", file, "File needs formatting", digest))

    types: dict[str, Any] = json.loads(
        command(root, [python, "-m", "pyright", "--pythonpath", python, "--outputjson"], (0, 1))
    )
    found.extend(
        diagnostic(
            root,
            "pyright",
            row.get("rule", "error"),
            row["file"],
            row["range"]["start"]["line"] + 1,
            row["message"],
        )
        for row in types["generalDiagnostics"]
        if row["severity"] in {"error", "warning"}
    )

    paths = [p for p in ("scripts", "tests", "tools", ".agents") if (root / p).exists()]
    unused = command(root, [python, "-m", "vulture", *paths, "--min-confidence", "80"], (0, 3))
    for line in unused.splitlines():
        match = re.fullmatch(r"(.+?):(\d+): (.+)", line)
        if match is None:
            message = f"Unrecognized Vulture output: {line}"
            raise ValueError(message)
        file, number, message = match.groups()
        found.append(diagnostic(root, "vulture", "unused", file, int(number), message))

    suppression = re.compile(r"#\s*(?:noqa\b|type:\s*ignore\b|pyright:|ruff:\s*noqa\b)", re.I)
    for directory in paths:
        for file in (root / directory).rglob("*.py"):
            found.extend(
                diagnostic(
                    root,
                    "policy",
                    "suppression",
                    str(file),
                    token.start[0],
                    "Inline check suppression",
                )
                for token in tokenize.generate_tokens(io.StringIO(file.read_text()).readline)
                if token.type == tokenize.COMMENT and suppression.search(token.string)
            )

    with tempfile.TemporaryDirectory(prefix="prompt-audit-pytest-") as temp:
        junit = Path(temp) / "tests.xml"
        output = command(
            root,
            [
                python,
                "-m",
                "pytest",
                "-W",
                "error",
                "-q",
                "tests",
                f"--junitxml={junit}",
                "-o",
                "cache_dir=" + temp,
            ],
            (0, 1),
        )
        print(output, end="")
        for case in ET.parse(junit).iter("testcase"):
            found.extend(
                Diagnostic(
                    "pytest",
                    "failure",
                    case.get("classname", "") + "::" + case.get("name", ""),
                    failure.get("message", "").replace(str(root), "<repo>"),
                    "",
                )
                for failure in case.findall("failure")
            )
            if case.find("error") is not None:
                message = f"Pytest setup or collection failed: {output}"
                raise RuntimeError(message)
    return found


def counts(findings: list[Diagnostic]) -> Counter[str]:
    return Counter(item.fingerprint for item in findings)


def config_digest() -> str:
    return hashlib.sha256(b"".join((ROOT / name).read_bytes() for name in CONFIGS)).hexdigest()


def write_baseline(ref: str) -> None:
    revision = command(ROOT, ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], (0,)).strip()
    archive = subprocess.run(
        ["git", "archive", revision], cwd=ROOT, capture_output=True, check=True
    ).stdout
    with tempfile.TemporaryDirectory(prefix="prompt-audit-base-") as temp:
        root = Path(temp).resolve()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(root, filter="data")
        for name in CONFIGS:
            shutil.copyfile(ROOT / name, root / name)
        for directory in ("tools", ".agents/skills/verify-prompt-audit/scripts"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        findings = collect(root)
    unique = {item.fingerprint: item for item in findings}
    payload = {
        "base_revision": revision,
        "config_sha256": config_digest(),
        "findings": [
            {"fingerprint": key, "count": count, **asdict(unique[key])}
            for key, count in sorted(counts(findings).items())
        ],
    }
    BASELINE.parent.mkdir(exist_ok=True)
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Baseline recorded {len(findings)} diagnostics from {revision}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", help="Regenerate only from an accepted base commit")
    args = parser.parse_args()
    if args.baseline_ref:
        write_baseline(args.baseline_ref)
        return 0
    baseline: dict[str, Any] = json.loads(BASELINE.read_text(encoding="utf-8"))
    if baseline["config_sha256"] != config_digest():
        message = "Quality config changed. Re-evaluate the accepted base before proceeding."
        raise ValueError(message)
    accepted = Counter({row["fingerprint"]: row["count"] for row in baseline["findings"]})
    findings = collect(ROOT)
    new = counts(findings) - accepted
    for item in {item.fingerprint: item for item in findings}.values():
        if item.fingerprint in new:
            print(f"NEW {item.tool} {item.path} {item.rule}: {item.message}")
    print(
        f"quality: {sum(new.values())} new, {sum((counts(findings) & accepted).values())} "
        f"existing diagnostics; base {baseline['base_revision']}"
    )
    return 1 if new else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"quality tool/config error: {error}", file=sys.stderr)
        sys.exit(2)
