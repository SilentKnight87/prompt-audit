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
Pytest exit failures, collection errors, setup errors, and stderr always fail before baseline comparison.
They cannot become accepted findings. Warnings remain visible in pytest's summary and are also collected
through its warning hook as structured diagnostics, with the qualified warning category, test node ID,
message, phase, and emitting source. Resource warnings also include project allocation provenance from
tracemalloc. Allocation source identifies a resource warning even when cyclic collection emits it from
teardown. Environment frames are excluded from that provenance. Source lines replace line numbers;
repository and fixture-root paths are normalized. Warning categories and test IDs are never generalized.
The same multiset rule rejects another occurrence of an existing warning.

The initial baseline omitted resource warnings because pytest's default filters hid them. This baseline
correction records 73 resource warnings actually reproduced while 89 tests passed in an unchanged archive
of the accepted base. It does not approve feature-branch debt or baseline test failures. Two independent
archive runs produced identical baseline bytes, and Python 3.11 and 3.14 matched the warning fingerprints.
No warnings from the new tooling or its tests have allowances.

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
