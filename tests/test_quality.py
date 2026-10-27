from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from tools.quality import Diagnostic, collect, command, counts, diagnostic


def test_fingerprints_survive_line_shifts(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("import unused\n")
    original = diagnostic(tmp_path, "ruff", "F401", str(source), 1, "unused import")
    source.write_text("\n\nimport unused\n")
    shifted = diagnostic(tmp_path, "ruff", "F401", str(source), 3, "unused import")
    assert original.fingerprint == shifted.fingerprint


def test_equal_totals_do_not_hide_new_findings() -> None:
    old = Diagnostic("ruff", "F401", "old.py", "unused import", "import old")
    new = Diagnostic("ruff", "F401", "new.py", "unused import", "import new")
    assert list((counts([new]) - counts([old])).elements()) == [new.fingerprint]


def test_duplicate_findings_consume_separate_allowances() -> None:
    item = Diagnostic("ruff", "F401", "example.py", "unused import", "import old")
    assert counts([item, item]) - counts([item]) == {item.fingerprint: 1}


def test_tool_errors_never_become_baseline_findings(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exited 2"):
        command(tmp_path, [sys.executable, "-c", "raise SystemExit(2)"], (0, 1))
    with pytest.raises(RuntimeError, match="warning"):
        command(tmp_path, [sys.executable, "-c", "import sys; sys.stderr.write('warning')"], (0,))


@pytest.fixture
def quality_project(tmp_path: Path) -> Path:
    shutil.copyfile(
        Path(__file__).resolve().parent.parent / "pyproject.toml", tmp_path / "pyproject.toml"
    )
    for directory in ("scripts", "tests", "tools", ".agents/skills/verify-prompt-audit/scripts"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "tests/test_sample.py").write_text("def test_sample():\n    assert True\n")
    return tmp_path


def test_suppressions_after_comment_explanations_are_findings(quality_project: Path) -> None:
    (quality_project / "tools/sample.py").write_text(
        'literal = "# explanation # noqa: F401"\n\n\n'
        "def example():\n"
        "    try:  # explanation # noqa: SIM105\n"
        '        int("invalid")\n'
        "    except Exception:  # explanation # noqa: S110\n"
        "        pass\n"
    )
    findings = collect(quality_project)
    assert not [item for item in findings if item.rule in {"SIM105", "S110"}]
    assert [item.source for item in findings if item.rule == "suppression"] == [
        "try:  # explanation # noqa: SIM105",
        "except Exception:  # explanation # noqa: S110",
    ]


def test_pytest_warning_is_a_failure(quality_project: Path) -> None:
    (quality_project / "tests/test_sample.py").write_text(
        "import warnings\n\n\n"
        "def test_sample():\n"
        '    warnings.warn("synthetic warning", UserWarning, stacklevel=1)\n'
    )
    failures = [item for item in collect(quality_project) if item.tool == "pytest"]
    assert len(failures) == 1
    assert "UserWarning: synthetic warning" in failures[0].message
