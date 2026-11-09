---
name: verify-prompt-audit
description: Verify prompt-audit's Python CLIs using synthetic instruction files and session metadata. Use after changes to collection, scanning, reports, redaction, or runtime probes.
---

# Verify prompt-audit

## Launch

Run from the repository root with Python 3.11 or later for the verification tools.
The product CLIs remain stdlib tools. There is no server, port, login, or UI to launch.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-quality.txt
.venv/bin/python .agents/skills/verify-prompt-audit/scripts/drive.py doctor
```

The doctor must print `doctor: ready`. It checks both real CLI help commands and registry JSON.

## Doctor

Rerun the doctor command when a drive fails before a journey starts.
It reads repository files and runs `--help`; it does not read personal harness directories or contact the network.
Confirm the printed checkout is the worktree being verified.

## Drive

Read the [feature map](features/README.md), then run the portable journeys:

```sh
.venv/bin/python .agents/skills/verify-prompt-audit/scripts/drive.py
PYTHON="$PWD/.venv/bin/python" ./scripts/quality
```

The helper invokes `scripts/prompt_audit.py` and `scripts/runtime_probe.py` as subprocesses.
It verifies inventory, scan, private and public reports, proposed diffs, input immutability,
positive and negative leak checking, session counts, cache shares, and gap buckets.
All harness directories are explicitly set to isolated synthetic locations.

For a change to guidance fetching, also run the optional online journey:

```sh
.venv/bin/python .agents/skills/verify-prompt-audit/scripts/drive.py guidance
```

This contacts the public sources in `references/labs.json`. It needs network access but no credentials.
The CLI can exit zero when sources fail; the helper checks every manifest status and fails on unavailable sources.
Keep the manifest and report external failures separately. Do not replace sources or weaken the check to get a pass.
CI runs the portable journeys only.

## Evidence

Each drive prints its unique `.verify/all-*` or `.verify/guidance-*` directory.
Inspect `transcript.jsonl` for commands, stdout, stderr, and actual and expected exit codes.
The deliberate leak must exit 1. `result.json` records the final verdict.
Inspect the reports and proposed diff alongside `audit-assertions.json` and `runtime-cache.json`.
The helper verifies the source is unchanged and preserves a copy under `inputs/`.
These are real CLI invocations. The portable journey does not claim a live guidance fetch or an agent's judgment pass.

For fleet proof, record the quality command and drive helper with `fleet-evidence run` on the final committed revision.
Attach the drive directory as an artifact. Use `fleet-verify` for the independent verifier and exact-revision gate.

## Cleanup

The helper removes only its own temporary fixture directory, including after a failed journey.
It starts no persistent process. Logs, reports, manifests, and result files remain in `.verify/`.
Confirm the printed evidence directory still exists after the helper exits.
Each run gets a distinct directory, so separate verifiers can run concurrently.
Never remove another verifier's evidence or point a journey at personal session directories.

## Helpers

`scripts/drive.py` is executable. Invoke it directly when `python3` is the prepared Python interpreter,
or use the explicit `.venv/bin/python` commands above.
Use `doctor` for a read-only readiness check, no argument for all portable features, and `guidance` for the online fetch.
Update the feature map with changed entry points. Use `maintain-verification-skill` when revisiting map drift.
