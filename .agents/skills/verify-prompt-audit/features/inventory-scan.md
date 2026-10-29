# Inventory and scan

Users discover instruction files and collect candidate findings for review without editing those files.

## Sub-features

- `inventory` discovers an explicit synthetic instruction file and its static reader.
- `scan` detects a volatile timestamp in always-loaded instructions.
- `read-only` leaves the source bytes unchanged.

## How to get to it (user POV)

Run `python3 scripts/prompt_audit.py inventory --help` or `python3 scripts/prompt_audit.py scan --help`.
Supply `--surfaces` and `--out` to inventory. Supply `--inventory`, `--surfaces`, and `--out` to scan.

## Driving it with the CLI helper

Preconditions: Run the doctor from the skill. No personal configuration is needed.

- Run `python3 .agents/skills/verify-prompt-audit/scripts/drive.py`.
- Inspect `inventory.json` for one synthetic file and `candidates.json` for a `cache-timestamp` candidate.
- Inspect `audit-assertions.json` for `input_unchanged: true` and the input copy under `inputs/`.
- Inspect `transcript.jsonl` for the exact inventory and scan commands, both exiting zero.

## Gotchas

Candidates need an agent's review before becoming findings. The helper supplies a synthetic triage decision for the report journey.
The portable scan uses `--no-blame` and no live guidance manifest. Git provenance and live catalog drift are not claimed by this journey.
