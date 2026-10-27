from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from tools.quality import Diagnostic, collect, collect_pytest, command, counts, diagnostic


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


def test_pytest_warning_is_a_structured_diagnostic(quality_project: Path) -> None:
    (quality_project / "tests/test_sample.py").write_text(
        "import warnings\n\n\n"
        "def test_sample():\n"
        '    warnings.warn("synthetic warning", UserWarning, stacklevel=1)\n'
    )
    warnings = collect_pytest(quality_project)
    assert len(warnings) == 1
    assert warnings[0].rule == "builtins.UserWarning"
    assert warnings[0].path == "tests/test_sample.py::test_sample"
    assert warnings[0].message == "synthetic warning"
    assert "tests/test_sample.py" in warnings[0].source
    assert counts(warnings) - counts([]) == {warnings[0].fingerprint: 1}


def test_pytest_failure_cannot_be_baselined(quality_project: Path) -> None:
    (quality_project / "tests/test_sample.py").write_text(
        "import warnings\n\n\n"
        "def test_sample():\n"
        '    warnings.warn("existing warning", UserWarning, stacklevel=1)\n'
        '    assert False, "synthetic failure"\n'
    )
    with pytest.raises(RuntimeError, match="Pytest failed"):
        collect_pytest(quality_project)


def test_warning_allowance_preserves_visibility_and_duplicate_counts(
    quality_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = quality_project / "tests/test_sample.py"
    sample.write_text(
        "import warnings\n\n\n"
        "def test_sample():\n"
        "    for _ in range(1):\n"
        '        warnings.warn("existing warning", UserWarning, stacklevel=1)\n'
    )
    accepted = collect_pytest(quality_project)
    sample.write_text("\n\n" + sample.read_text())
    repeated = collect_pytest(quality_project)
    assert counts(accepted) == counts(repeated)
    assert "UserWarning: existing warning" in capsys.readouterr().out
    sample.write_text("\n" + sample.read_text().replace("range(1)", "range(2)"))
    assert counts(collect_pytest(quality_project)) - counts(accepted) == {
        accepted[0].fingerprint: 1
    }


def test_cyclic_resource_warning_uses_allocation_provenance(quality_project: Path) -> None:
    sample = quality_project / "tests/test_sample.py"
    sample.write_text(
        "def test_sample(tmp_path):\n"
        '    stream = open(tmp_path / "fixture.txt", "w")\n'
        "    stream.cycle = stream\n"
    )
    first = collect_pytest(quality_project)
    sample.write_text("\n\n" + sample.read_text())
    second = collect_pytest(quality_project)
    assert len(first) == 1
    assert first[0].rule == "builtins.ResourceWarning"
    assert first[0].path == "tests/test_sample.py::test_sample"
    provenance = json.loads(first[0].source)
    assert provenance["site"] == {
        "path": "tests/test_sample.py",
        "source": 'stream = open(tmp_path / "fixture.txt", "w")',
    }
    assert provenance["allocation"]
    assert counts(first) == counts(second)
