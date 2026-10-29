# Working on prompt-audit

Keep the collector read-only. Do not change product behavior as part of verification or tooling work.
Use synthetic instructions and session metadata for fixtures. Keep private audit output under ignored `runs/` or `.verify/`.

## Proof

Use [.agents/skills/verify-prompt-audit/SKILL.md](.agents/skills/verify-prompt-audit/SKILL.md) to drive the real CLIs.
Run both the verify skill and `./scripts/quality` before claiming done.

Install the portable quality tools with Python 3.11 or later:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-quality.txt
PYTHON="$PWD/.venv/bin/python" ./scripts/quality
.venv/bin/python .agents/skills/verify-prompt-audit/scripts/drive.py
```

The quality command runs Ruff lint and formatting, Pyright, Vulture, and pytest.
Warnings and tool or config errors fail. The accepted base findings are documented in [.quality/README.md](.quality/README.md).
Do not add suppressions, weaken checks, or grow the baseline to pass a change.

Install this checkout's commit hook with `git config core.hooksPath .githooks`.
It strips AI `Co-Authored-By:` trailers and `Generated with` attribution lines while preserving human co-authors.
This command changes repository config, not global config. Git worktrees share repository config.
