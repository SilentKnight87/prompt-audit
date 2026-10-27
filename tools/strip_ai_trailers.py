#!/usr/bin/env python3
"""Remove AI attribution lines while preserving human co-authors and message bytes."""

from __future__ import annotations

import re
import sys
from pathlib import Path

AI = re.compile(r"\b(?:Claude|Codex|Cursor|Copilot|Gemini|GPT)(?:\b|\d)", re.IGNORECASE)
COAUTHOR = re.compile(r"^\s*Co-Authored-By:\s*(.*)", re.IGNORECASE)
GENERATED = re.compile(r"^\s*(?:[>*_`🤖]+\s*)?Generated with\b", re.IGNORECASE)


def strip_attribution(message: bytes) -> bytes:
    kept: list[bytes] = []
    for line in message.splitlines(keepends=True):
        text = line.decode("utf-8", errors="replace")
        author = COAUTHOR.match(text)
        if GENERATED.match(text) or (author and AI.search(author.group(1))):
            continue
        kept.append(line)
    return b"".join(kept)


def main() -> None:
    path = Path(sys.argv[1])
    path.write_bytes(strip_attribution(path.read_bytes()))


if __name__ == "__main__":
    main()
