# AGENTS.md — homensai-install-modull

<!-- HSAI-AI-CONTEXT-CACHING-POLICY:BEGIN -->
## Mandatory AI preflight and token-cost optimization (owner instruction, 2026-11-12, updated with measurements)

Before substantive work, map the **complete tracked project structure** and active state; read README, root/nested AGENTS and CLAUDE rules, architecture/status/handoff/decisions, manifests, dependencies, CI, tests, configuration and relevant sources **end-to-end**. Distinguish current files from archives/generated/vendor/sensitive content. Reuse a concise, version-aware project context map; refresh changing facts. Never blindly paste an entire repository, log or JSON into a model prompt; trim tool output.

**Prompt caching is the main cost lever and is always on** (owner, 12.11.2026). A cache read costs 0.05× of input on Claude Opus 5.5 / Sonnet 5.5; a write costs 1.25× (5 min) or 2× (1 h), so caching pays only where the same prefix is read again.

- **Claude Code (subscription or API key):** user settings `~/.claude/settings.json` contain `"promptCacheTtl": "1h"` and `"subagentPromptCacheTtl": "1h"` (or env `CLAUDE_CODE_PROMPT_CACHE_TTL=1h`, `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h`; recreate them in temporary cloud containers). Without them subagents use 5 min and any wait longer than 5 min rewrites the agent's whole context (Rent, 12.11: 61 % of all cache writes). Do not switch model, effort or fast mode mid-session; continue a running agent (SendMessage) or fork the current one instead of starting a fresh agent; run long commands in the background; keep agent reports short.
- **Direct Claude Messages API:** order `tools → system → messages`; put shared stable material (code, specs, examples) first with `cache_control` at its end and the changing question after it; no timestamps/IDs/unsorted JSON in the prefix; max 4 breakpoints, 20-block lookback, model minimum length (512 tokens on the 5.x models). Policy measured 12.11: **1 task → no cache marks** (a lone 1 h write cost ×0.74 of the old way); **2+ tasks starting together → 5 min marks + one `max_tokens: 0` pre-warm** before the parallel calls (parallel requests cannot read an entry still being written); **3+ queued tasks or multi-round continuations → 1 h**. Verify `cache_creation_input_tokens` / `cache_read_input_tokens`; remove marks that are never read.
- **Every report on API work compares with cache against the old way:** "with cache $X, old way $Y, saved $Z, ×K" (old way = all input tokens at the base price). Measured 12.11: 3 tasks sharing one context ×3.75–5.17 per task, ×2.24 for the job including the pre-warm.
- **Other providers:** keep the same stable-prefix layout; enable caching only as their current docs describe. If a repo has no qualifying API calls, record **"API prompt caching not applicable"** and optimize context retrieval instead. Never cache or expose secrets or confidential user data unnecessarily; provider retention, privacy and safety rules outrank optimization.

Before finishing, report inspected sources, caching opportunities/implementation, real metrics vs estimates, tests and limitations. Follow existing repo safety/branch/release rules.

Canonical policy: https://github.com/HomenSAI/homensai-core/blob/main/docs/AI_CONTEXT_EFFICIENCY_POLICY.md
<!-- HSAI-AI-CONTEXT-CACHING-POLICY:END -->
