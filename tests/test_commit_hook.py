from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.strip_ai_trailers import strip_attribution

ROOT = Path(__file__).resolve().parent.parent


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHON": sys.executable},
    ).stdout


@pytest.mark.parametrize(
    "identity",
    [
        "Claude Code <noreply@anthropic.com>",
        "Codex <codex@openai.com>",
        "Cursor Agent <cursoragent@cursor.com>",
        "GitHub Copilot <copilot@github.com>",
        "Gemini Code Assist <gemini-code-assist@google.com>",
        "GPT-4o <bot@example.invalid>",
        "Copilot[bot] <bot@example.invalid>",
    ],
)
def test_known_ai_identities_are_stripped(identity: str) -> None:
    message = f"Subject\n\nCo-Authored-By: {identity}\n".encode()
    assert strip_attribution(message) == b"Subject\n\n"


@pytest.mark.parametrize(
    "identity",
    [
        "Claude Martin <claude.martin@example.invalid>",
        "Gemini Patel <gemini.patel@example.invalid>",
        "Alex Reviewer <claude@example.invalid>",
        "Alex Reviewer <gemini@example.invalid>",
        "Claude Martin <claude.martin@anthropic.com>",
    ],
)
def test_human_identities_are_preserved(identity: str) -> None:
    message = f"Subject\n\nCo-Authored-By: {identity}\n".encode()
    assert strip_attribution(message) == message


def test_git_commit_strips_ai_and_preserves_human_lines(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Synthetic Author")
    git(tmp_path, "config", "user.email", "author@example.invalid")
    git(tmp_path, "config", "core.hooksPath", ".githooks")
    for file in (".githooks/commit-msg", "tools/strip_ai_trailers.py"):
        target = tmp_path / file
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(ROOT / file, target)
    message = tmp_path / "message.txt"
    original = (
        b"Verify attribution\r\n\r\nOrdinary body mentions GPT and Claude.\r\n"
        b"Co-Authored-By: Human Reviewer <human@example.invalid>\r\n"
        b"Co-Authored-By: Claude Martin <claude.martin@example.invalid>\r\n"
        b"Co-Authored-By: Gemini Patel <gemini.patel@example.invalid>\r\n"
        + b"".join(
            f"Co-Authored-By: {name} <bot@example.invalid>\r\n".encode()
            for name in ("Claude", "Codex", "Cursor", "Copilot", "Gemini", "GPT", "GPT6")
        )
        + b"Generated with a tool\r\n"
        + "🤖 Generated with Claude Code\r\n".encode()
    )
    message.write_bytes(original)
    subprocess.run(
        [str(tmp_path / ".githooks/commit-msg"), str(message)],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "PYTHON": sys.executable},
    )
    expected = (
        b"Verify attribution\r\n\r\nOrdinary body mentions GPT and Claude.\r\n"
        b"Co-Authored-By: Human Reviewer <human@example.invalid>\r\n"
        b"Co-Authored-By: Claude Martin <claude.martin@example.invalid>\r\n"
        b"Co-Authored-By: Gemini Patel <gemini.patel@example.invalid>\r\n"
    )
    assert message.read_bytes() == expected
    message.write_bytes(original)
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-F", str(message))
    committed = git(tmp_path, "log", "-1", "--format=%B")
    assert "Human Reviewer" in committed
    assert "Claude Martin <claude.martin@example.invalid>" in committed
    assert "Gemini Patel <gemini.patel@example.invalid>" in committed
    assert "Ordinary body mentions GPT and Claude." in committed
    assert "bot@example.invalid" not in committed
    assert "Generated with" not in committed
