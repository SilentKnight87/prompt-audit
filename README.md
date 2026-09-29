# prompt-audit

Anthropic's `/claude-api prompt-audit` is a great way to clean up instructions written for older Claude models. I run agents across several harnesses and labs, so I adapted it to work across all of them.

It audits your agent instructions (AGENTS.md, CLAUDE.md, SKILL.md, personas, rules) against **the current official docs and prompting guides from each lab**, and against **what your harnesses are actually configured to run**. Then it proposes fixes and applies the ones you approve.

- **Multi-lab:** guidance for Anthropic, OpenAI and Google is fetched at run time from their official docs (`references/labs.json`), never hardcoded. Add a lab by adding an entry.
- **Multi-harness:** Hermes Agent, Claude Code, Codex, OMP (Oh My Pi) and repo-level AGENTS.md/CLAUDE.md. Each file is judged against the models that actually read it.
- **Drift pass:** stale paths, retired locations, model IDs and effort settings that no longer match config, skill forks that diverged across profiles, and generated instruction blocks that got truncated.
- **Safe by default:** read-only until you approve. Public-safe report variant with a leak check, so you can share results without leaking paths or names.

## What it found on my own setup

First dry run, 443 instruction files across four harnesses:

- 1,054 raw flags, 8 real findings after review
- 7 of the 8 were **drift** (config and instructions disagreeing), only 1 was old-style prompt wording
- Top finding: an always-loaded memory block was being cut off mid-rule because its source outgrew the generator's budget

The old-wording stuff (ALL-CAPS, "think step by step") is the easy part. The expensive problems were instructions that quietly stopped matching reality.

## Install

It's a plain `SKILL.md` plus stdlib Python 3.9+ scripts, so it works in any harness that loads skills.

```bash
git clone https://github.com/SilentKnight87/prompt-audit
# Hermes Agent
ln -s "$PWD/prompt-audit" ~/.hermes/skills/software-development/prompt-audit
# Claude Code
ln -s "$PWD/prompt-audit" ~/.claude/skills/prompt-audit
# Codex
ln -s "$PWD/prompt-audit" ~/.agents/skills/prompt-audit
```

Then ask your agent: "run a prompt audit on my setup".

## Run it by hand

```bash
RUN=runs/$(date +%F)
mkdir -p $RUN
cp references/surfaces.example.json $RUN/surfaces.json     # edit to match your setup
cp references/public.example.json  $RUN/public.json        # names/paths to redact

python3 scripts/prompt_audit.py fetch-guidance --cache $RUN/guidance
python3 scripts/prompt_audit.py inventory --surfaces $RUN/surfaces.json --out $RUN/inventory.json
python3 scripts/prompt_audit.py scan --inventory $RUN/inventory.json --out $RUN/candidates.json \
    --guidance $RUN/guidance/manifest.json --surfaces $RUN/surfaces.json
python3 scripts/runtime_probe.py            # what models/effort your sessions actually ran
```

The scan produces candidates, not findings. The agent reviews each one (the judgment step in `SKILL.md`), writes `triage.json`, then:

```bash
python3 scripts/prompt_audit.py report --triage $RUN/triage.json --candidates $RUN/candidates.json \
    --out $RUN/report --public-config $RUN/public.json
python3 scripts/prompt_audit.py leakcheck $RUN/report/report.public.md --public-config $RUN/public.json
```

`runs/` is git-ignored because it contains your private paths.

## Tests

```bash
python3 -m pytest -q tests
```

## Credit

Method derived from Anthropic's prompt-audit guide in [anthropics/skills](https://github.com/anthropics/skills/blob/main/skills/claude-api/shared/prompt-audit.md) (Apache-2.0). See `NOTICE`.
