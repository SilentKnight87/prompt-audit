# Fable 5.2 evaluation controls

Source checked 2026-11-09: https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5-2
Migration: https://platform.claude.com/docs/en/models/fable-5-2/migration-guide

Read current official guidance before applying model-specific advice. Vendor findings are hypotheses for local tests, not proof of local improvement.

- Compare medium and high on identical fixtures. Test xhigh only where measured benefit justifies it. Record served model, effective effort, harness version, tools, prompt revision, tokens, latency and human interventions.
- Test completion after safe transient errors separately from respecting deliberately denied actions. Preserve approval, privacy and destructive-action boundaries. Avoid repeat approval only when existing authorization covers the exact action.
- Review delegation mandates and accumulated caution before adding more instructions. Keep justified rules with scope and reason. Probe contested removals on scratch copies.
- Do not add blanket codebase-ownership language to user-owned repos. The vendor reports unsolicited changes even alongside scope restrictions.
- Stage changed instructions for fresh sessions. Preserved thinking can become invalid when system, tools or earlier turns change. Disk edits do not prove the running prompt changed. Adapter repair is separate from a prose audit.
- Separate infrastructure failures, missing results and quality failures. Never present an interrupted run as a final score or compare it directly to a complete historical arm. Inspect failed transcripts for rubric validity without silently changing the rubric after seeing results.
- Hold fixture, model, effort, tools and instructions constant for a controlled cross-harness test; document unavoidable differences. Evaluate native harness configurations separately.
- Publish sanitized fixtures and verified receipts only. Synthetic writing performance is not the user's voice approval. Prepared outreach is not sent outreach.

Minimum fixtures: stale/current decision reconciliation; safe retry; deliberate denial; false worker completion; missing evidence; publication approval; isolation proof; concise artifact completion. Use synthetic data for public tests.
