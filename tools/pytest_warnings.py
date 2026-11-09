"""Run pytest with visible warnings and a machine-readable diagnostic report."""

from __future__ import annotations

import gc
import json
import sys
import tracemalloc
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest


class WarningRecorder:
    def __init__(self, root: Path, fixtures: Path) -> None:
        self.root = root.resolve()
        self.fixtures = fixtures.resolve()
        self.findings: list[dict[str, str]] = []

    def normalize(self, text: str) -> str:
        return text.replace(str(self.fixtures), "<fixtures>").replace(str(self.root), "<repo>")

    def in_project(self, path: Path) -> bool:
        return path.is_relative_to(self.root) and not path.is_relative_to(
            Path(sys.prefix).resolve()
        )

    def site(self, filename: str, lineno: int) -> dict[str, str]:
        path = Path(filename).resolve()
        source = ""
        if self.in_project(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            source = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            filename = path.relative_to(self.root).as_posix()
        else:
            filename = self.normalize(str(path))
        return {"path": filename, "source": source}

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_teardown(self) -> Iterator[None]:
        try:
            yield
        finally:
            # Attribute cyclic resource finalizers to the test that created them.
            gc.collect()

    @pytest.hookimpl
    def pytest_warning_recorded(
        self, warning_message: warnings.WarningMessage, when: str, nodeid: str
    ) -> None:
        site = self.site(warning_message.filename, warning_message.lineno)
        provenance: dict[str, object] = {"phase": when, "site": site}
        if issubclass(warning_message.category, ResourceWarning):
            allocation = tracemalloc.get_object_traceback(warning_message.source)
            if allocation is not None:
                project_allocation = [
                    self.site(frame.filename, frame.lineno)
                    for frame in allocation
                    if self.in_project(Path(frame.filename).resolve())
                ]
                provenance["allocation"] = project_allocation
                if project_allocation:
                    provenance["site"] = project_allocation[-1]
        category = warning_message.category
        self.findings.append(
            {
                "tool": "pytest",
                "rule": f"{category.__module__}.{category.__qualname__}",
                "path": nodeid,
                "message": self.normalize(str(warning_message.message)),
                "source": json.dumps(provenance, sort_keys=True),
            }
        )


def main() -> int:
    report = Path(sys.argv[1])
    fixtures = report.parent / "fixtures"
    recorder = WarningRecorder(Path.cwd(), fixtures)
    sys.path.insert(0, str(recorder.root))
    tracemalloc.start(16)
    status = pytest.main(
        [
            "-W",
            "always",
            "-q",
            "tests",
            f"--basetemp={fixtures}",
            "-o",
            f"cache_dir={report.parent / 'cache'}",
        ],
        plugins=[recorder],
    )
    report.write_text(json.dumps(recorder.findings, indent=2) + "\n", encoding="utf-8")
    return int(status)


if __name__ == "__main__":
    sys.exit(main())
