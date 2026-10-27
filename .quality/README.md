# Quality baseline

Run `./scripts/quality` with the Python environment from `AGENTS.md`.
CI uses the same pinned tools and command on Python 3.11.
The product's documented Python requirement remains unchanged.

`baseline.json` records findings from main at `9320fc8f451656d722317800d5188150e2a52870`.
The generator checks a temporary `git archive` of that commit using this branch's quality config.
It does not copy current product files into the archive. New tooling and tests have no baseline allowances.

Each fingerprint hashes the tool, rule, repository-relative path, message, and stripped source line.
Line numbers are excluded so inserting lines above an old finding does not create a new finding.
A multiset preserves duplicate counts, so another copy of an old violation still fails.
Replacing an old violation with a different violation fails even when the total count stays equal.
Formatting findings use the complete file digest because Ruff reports formatting at file level.
Editing an unformatted legacy file requires formatting it before it can pass.
Pytest failures use the test ID and assertion message. Collection and setup errors always fail.

Ruff owns Python linting. Its rules cover unused code, ineffective exception handlers, unused suppressions,
and the fleet Python rule families. Pyright checks existing modules in basic mode and the new tooling,
verification helper, and tooling tests in strict mode. Vulture checks unused code at 80% confidence.
Inline `noqa`, type-ignore, and Pyright override comments also count as findings, including effective suppressions.

The baseline also binds the config and dependency pins by SHA-256. A config change requires a fresh
evaluation of the accepted base, rather than silently retaining allowances from another rule set.
To reproduce it for review, run:

```sh
PYTHON="$PWD/.venv/bin/python" ./scripts/quality --baseline-ref 9320fc8f451656d722317800d5188150e2a52870
```

This is a maintenance command, not a way to approve current-branch debt.
Never generate a baseline from the feature branch or add baseline entries to make a change pass.
Review changes to the config, generator, and baseline together.
