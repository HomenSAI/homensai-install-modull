# Указания (homensai-install-modull)
- Общение, документы и коммиты — на русском. Автор — Serhii Khomenko.
- Владелец на Windows без инструментов разработки: проверки — в Docker.
- Не объявлять установщик готовым без тестов install/update/restore/rollback.
- Секреты в репозиторий не добавлять. Пуш в main — только по разрешению владельца в сессии.

<!-- HSAI-CLAUDE-CONTEXT-PREFLIGHT:BEGIN -->
## Required preflight and prompt caching (owner instruction, 2026-11-12, updated with measurements)
Read **all of AGENTS.md** and its AI context/caching policy before substantive work. Inventory the project and current state; fully trace task-relevant code/docs; reuse a compact context map; targeted reads, trimmed tool output (`head`/`tail`/`cut`), no pasting of whole repositories, logs or JSON.
**Prompt caching is the main cost lever, always on.** Claude Code: user settings `~/.claude/settings.json` must contain `"promptCacheTtl": "1h"` and `"subagentPromptCacheTtl": "1h"` (documented settings; subagents otherwise get 5 min and every wait > 5 min rewrites their whole context). Do not switch model, effort or fast mode mid-session; continue a running agent (SendMessage) or fork instead of starting a fresh one; long commands in the background. Direct Claude Messages API: stable shared material first with `cache_control` at the end of it, the changing question after; cache only when the prefix is read again (1 task: no marks; 2+ tasks starting together: 5m + one `max_tokens: 0` pre-warm; 3+ queued: 1h); every report states **with cache $X vs old way $Y, saved $Z, ×K** from `cache_creation_input_tokens` / `cache_read_input_tokens`. Respect every existing project safety and release rule. Full policy: https://github.com/HomenSAI/homensai-core/blob/main/docs/AI_CONTEXT_EFFICIENCY_POLICY.md
<!-- HSAI-CLAUDE-CONTEXT-PREFLIGHT:END -->
