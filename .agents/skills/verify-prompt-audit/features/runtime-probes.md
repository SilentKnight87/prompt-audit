# Runtime probes

Users inspect local session model counts or token-weighted cached-input shares without sending session data anywhere.

## Sub-features

- `models` reports session counts by model and effort.
- `cache` reports two synthetic requests with an 80% cached-input share.
- `gaps` assigns the second synthetic request to the under-five-minute bucket.

## How to get to it (user POV)

Run `python3 scripts/runtime_probe.py --help`.
Pass `--claude-dir`, `--codex-dir`, and `--last` explicitly. Add `--cache` for usage analysis.

## Driving it with the CLI helper

Preconditions: Run the doctor. The helper creates synthetic Claude session JSONL and an empty Codex location.

- Run `python3 .agents/skills/verify-prompt-audit/scripts/drive.py`.
- Inspect `runtime-default.json` for one synthetic Claude session and no Codex sessions.
- Inspect `runtime-cache.json` for two requests, `cached_input_share: 0.8`, one unbucketed request, and one `<5m` request.
- Inspect both actual probe invocations and exit codes in `transcript.jsonl`.

## Gotchas

The default CLI reads real harness directories unless both paths are overridden.
This journey covers Claude usage and an empty Codex neighbor. The pytest suite covers Codex events, malformed metadata, deduplication, and other gap buckets.
Cached-input share is not a cache hit rate or retention guarantee.
