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

<!-- HSAI-NANOGPT-MODELS:BEGIN -->
## NanoGPT models (owner decision 13.11.2026; measured on 12-13.11.2026)

Use NanoGPT (monthly subscription endpoint `https://nano-gpt.com/api/subscription/v1/chat/completions`, key from the user environment
`NANOGPT_API_KEY`, never printed or committed). **Use `z-ai/glm-5.3:thinking` as much as possible for everything** - it counts x1
against the subscription; the other two count x2 and are used only rarely:

| Use | Model | Subscription multiplier | reasoning_effort | max_tokens |
|---|---|---|---|---|
| **Everything by default**: code, code review, error/log diagnosis, tests, texts, translations | `z-ai/glm-5.3:thinking` | **x1** | high | 100000 |
| Rarely: a second opinion on a hard problem, or after GLM-5.3 failed 5 times | `deepseek/deepseek-v4-pro-0813:thinking` | x2 | high | 100000 |
| Rarely: C code ONLY (e.g. a Lua->C port) | `moonshotai/kimi-k2.7-code` | x2 | not sent | 65536 (model ceiling) |

Rules:
- At most **2 requests at once per model** (total <= 8); stream responses; on 429/503 wait `Retry-After` and retry.
- Answer limit 100 000 tokens. An EMPTY answer (stream cut, or the model spent the budget thinking) is re-sent up to **5 times** in
  total, only then stop. Never pin a provider (`X-Provider` is billed pay-as-you-go outside the subscription).
- Every complex problem goes to GLM-5.3 with a full dossier; Claude writes its own answer first (sealed) and compares.
- Model output is a proposal: nothing is accepted without our own tests and a Claude review. No secrets or personal data in prompts.
- Not used (measured weaker or unreliable): GLM-5.2, Nemotron 3 Ultra, Qwen3 Coder, DeepSeek V4.1 flash-1017, MiniMax M3, Devstral 2.

Why (12-13.11.2026 benchmark on real project tasks): GLM-5.3 was first in error diagnosis (found bugs and causes the others and Claude
missed), code review, our own functions graded by our unit tests (53 %), long tasks and test generation (74/75 cases consistent), and
it costs x1. DeepSeek V4 Pro was the most reliable second opinion; Kimi K2.7 Code wrote the best C port (20 000-case differential
check: 0 failures) but is weak on Python. Both cost x2.
<!-- HSAI-NANOGPT-MODELS:END -->

## Коды ошибок — только из базы HomenSAI/ERROR-DATABASE
Решение владельца 15.11.2026: единственный источник кодов ошибок — https://github.com/HomenSAI/ERROR-DATABASE (читать `AGENTS.md`, `ARCHITECTURE.md` §3.1, §7, §9, §12, `namespaces.json`, `CHANGELOG.md` базы). Подробная инструкция: [docs/CONNECT-PRODUCT.md](https://github.com/HomenSAI/ERROR-DATABASE/blob/main/docs/CONNECT-PRODUCT.md).
- Пространство кодов для этого продукта **ещё не заведено**: запросить в базе новое пространство (следующий свободный `number` в `namespaces.json`); коды локально не придумывать и не вводить «временные».
- Буквенный код `<ПРОСТРАНСТВО>-<ОБЛАСТЬ>-NNN` — основной, не переиспользуется; `key` — машинное имя; `aliases` — прежние коды продукта. Цифровой код `PPP-AAA-NNN` (9 цифр): PPP — номер продукта (COMMON 001, CORE 002, RENT 003, JP 004, VPN 005, SEC 006), AAA — номер области, NNN — номер из буквенного кода (`VPN-PAIR-007` → `005-001-007`).
- Новый код добавляется только в базе (`codes/<NS>/<AREA>.json`): тексты ru/uk/en/de (title, cause, action + escalation, level, audience, retryable, help, since) понятны без технических знаний и без адресов, IP, ключей, путей, внутренней логики и ПДн; `python tools/validate.py` = 0 ошибок, `python -m pytest -q`, `python tools/gen_constants.py`, запись в CHANGELOG базы; перед коммитом в базу `git pull`.
- В продукт кладутся файлы выпуска `errors.<NS>.json`, `errors.COMMON.json`, `errors.manifest.json` и `errors.lock` (SHA-256), как в Homensai-Rent; коды вшиваются при сборке, сеть не нужна; сборка и тесты сверяют хэш с `errors.lock`.
- Интерфейс показывает текст ошибки, буквенный код, цифровой код («Цифровой код 005-001-007») и ссылку `https://homensai.github.io/ERROR-DATABASE/errors/<КОД>`; прежний код — в скобках как «прежний код».
- Тесты продукта: каждый выдаваемый код есть в базе, цифровой код совпадает с базой, `errors.lock` совпадает, цифровой код виден в интерфейсе. Самодельные коды запрещены и ловятся тестом.
