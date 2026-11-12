# Указания (homensai-install-modull)
- Общение, документы и коммиты — на русском. Автор — Serhii Khomenko.
- Владелец на Windows без инструментов разработки: проверки — в Docker.
- Не объявлять установщик готовым без тестов install/update/restore/rollback.
- Секреты в репозиторий не добавлять. Пуш в main — только по разрешению владельца в сессии.

<!-- HSAI-CLAUDE-CONTEXT-PREFLIGHT:BEGIN -->
## Required preflight: project understanding and maximum justified token savings (2026-11-12)
Read **all of AGENTS.md** and its AI context/caching policy before substantive work. Inventory the entire project and current state; fully trace task-relevant code/docs; reuse a compact context map. Audit stable context, tool definitions and recurring documents before choosing a cache strategy. In direct Claude Messages API calls use supported `cache_control` (automatic or stable-prefix breakpoints, TTL 5m by default), then verify cache reads/writes, costs, latency and answer quality. Avoid duplicate retrieval and oversized prompts. Claude Code subscription limits are **not** changed by API cache settings. Respect every existing project safety and release rule. Full policy: https://github.com/HomenSAI/homensai-core/blob/main/docs/AI_CONTEXT_EFFICIENCY_POLICY.md
<!-- HSAI-CLAUDE-CONTEXT-PREFLIGHT:END -->
