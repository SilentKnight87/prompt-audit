# Guidance fetch

Users cache current public lab documents and inspect per-source success, age, hashes, and extracted model IDs.

## Sub-features

- `fetch` downloads every registered source through the real CLI.
- `manifest` records a status for every source, including failures.
- `integrity` retains nonempty fetched text matching each manifest digest.

## How to get to it (user POV)

Run `python3 scripts/prompt_audit.py fetch-guidance --cache <directory>`.
The source registry is `references/labs.json`.

## Driving it with the CLI helper

Preconditions: Public internet access is required. No personal configuration or credentials are used.

- Run `python3 .agents/skills/verify-prompt-audit/scripts/drive.py guidance`.
- Inspect `guidance/manifest.json`, the cached source files, and `transcript.jsonl` in the printed evidence directory.
- Require every registered source to have `status: ok` and a matching nonempty cache digest.
- If a source is unavailable, retain the failed manifest and report the source IDs. The helper fails even if the CLI exits zero.

## Gotchas

CI intentionally skips this journey because public documentation hosts can fail or move independently.
The portable drive does not verify live guidance. Pytest covers fetch errors and model extraction at the existing opener boundary.
