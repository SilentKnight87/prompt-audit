# Reports and leak checking

Users render reviewed findings and proposed edits, then check public artifacts for private terms.

## Sub-features

- `report` writes private and public Markdown plus proposed diffs from synthetic triage.
- `redaction` removes a synthetic private marker from the public report.
- `leakcheck` accepts clean artifacts and rejects a deliberate synthetic leak.

## How to get to it (user POV)

Run `python3 scripts/prompt_audit.py report --help` or `python3 scripts/prompt_audit.py leakcheck --help`.
Report takes `--triage`, `--candidates`, `--out`, and `--public-config`.
Leakcheck takes artifact paths and `--public-config`.

## Driving it with the CLI helper

Preconditions: Run the doctor. The helper creates triage from its real scan candidate.

- Run `python3 .agents/skills/verify-prompt-audit/scripts/drive.py`.
- Inspect `report/report.private.md` for the synthetic private marker and `report/report.public.md` for its redaction.
- Inspect `report/proposed.public.diff` for removal of the generated timestamp, and `inputs/AGENTS.md` for the unchanged original.
- Inspect `transcript.jsonl` for clean leakcheck exit 0 and deliberately leaky artifact exit 1.

## Gotchas

Report produces proposed edits only. It never applies them.
Do not publish the private report or raw inventory from a real audit. All committed fixtures must remain synthetic.
