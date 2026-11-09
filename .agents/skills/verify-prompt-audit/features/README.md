# Prompt-audit verification map

Use the Python interpreter and doctor command in [the skill](../SKILL.md).
The helpers create synthetic files under a unique `.verify/` directory and remove scratch files after each run.
They never use the default personal harness directories.

Run `python3 .agents/skills/verify-prompt-audit/scripts/drive.py` for all portable features.
Capture `transcript.jsonl`, the generated artifacts, and `result.json` from its printed evidence directory.
A passing pytest run alone does not prove these CLI journeys.

- [Inventory and scan](inventory-scan.md) covers discovery, readers, and candidate generation.
- [Reports and leak checking](reports-leakcheck.md) covers triage rendering, proposed edits, and public redaction.
- [Runtime probes](runtime-probes.md) covers model counts and usage-only cache analysis.
- [Guidance fetch](guidance-fetch.md) covers the optional network journey and source manifests.

Report each unreachable entry point and its unmet precondition. A portable pass does not cover the online fetch.
