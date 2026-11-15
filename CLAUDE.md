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

## Коды ошибок — только из базы HomenSAI/ERROR-DATABASE
Решение владельца 15.11.2026: единственный источник кодов ошибок — https://github.com/HomenSAI/ERROR-DATABASE (читать `AGENTS.md`, `ARCHITECTURE.md` §3.1, §7, §9, §12, `namespaces.json`, `CHANGELOG.md` базы). Подробная инструкция: [docs/CONNECT-PRODUCT.md](https://github.com/HomenSAI/ERROR-DATABASE/blob/main/docs/CONNECT-PRODUCT.md).
- Пространство кодов для этого продукта **ещё не заведено**: запросить в базе новое пространство (следующий свободный `number` в `namespaces.json`); коды локально не придумывать и не вводить «временные».
- Буквенный код `<ПРОСТРАНСТВО>-<ОБЛАСТЬ>-NNN` — основной, не переиспользуется; `key` — машинное имя; `aliases` — прежние коды продукта. Цифровой код `PPP-AAA-NNN` (9 цифр): PPP — номер продукта (COMMON 001, CORE 002, RENT 003, JP 004, VPN 005, SEC 006), AAA — номер области, NNN — номер из буквенного кода (`VPN-PAIR-007` → `005-001-007`).
- Новый код добавляется только в базе (`codes/<NS>/<AREA>.json`): тексты ru/uk/en/de (title, cause, action + escalation, level, audience, retryable, help, since) понятны без технических знаний и без адресов, IP, ключей, путей, внутренней логики и ПДн; `python tools/validate.py` = 0 ошибок, `python -m pytest -q`, `python tools/gen_constants.py`, запись в CHANGELOG базы; перед коммитом в базу `git pull`.
- В продукт кладутся файлы выпуска `errors.<NS>.json`, `errors.COMMON.json`, `errors.manifest.json` и `errors.lock` (SHA-256), как в Homensai-Rent; коды вшиваются при сборке, сеть не нужна; сборка и тесты сверяют хэш с `errors.lock`.
- Интерфейс показывает текст ошибки, буквенный код, цифровой код («Цифровой код 005-001-007») и ссылку `https://homensai.github.io/ERROR-DATABASE/errors/<КОД>`; прежний код — в скобках как «прежний код».
- Тесты продукта: каждый выдаваемый код есть в базе, цифровой код совпадает с базой, `errors.lock` совпадает, цифровой код виден в интерфейсе. Самодельные коды запрещены и ловятся тестом.
