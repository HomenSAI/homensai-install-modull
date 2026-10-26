# Работа над HomenSAI Installer

Общение, документы и коммиты — на русском. Автор продукта — Serhii Khomenko.
Прочитать CLAUDE.md, docs/RESUME.md, docs/work-state.json, канонические
HomenSAI_Universal_Installer_PROMPT_RU.txt и HomenSAI_Universal_Installer_TODO_RU.txt,
docs/VERIFICATION_BACKLOG.md и docs/verification-status.json до реализации.

Для явно запрошенного 12-часового запуска следовать docs/SOL61_12H_PLAN_RU.md
и SOL61_12H_START_PROMPT_RU.txt. Три агента всего: основной координатор,
core_lifecycle, platform_acceptance. Все gpt-6.1-sol / high; два дочерних
агента без дальнейшей делегации. Обычный вопрос не запускает 12-часовую работу.
Координатор пишет общие status/docs/API exports; дочерние агенты имеют отдельные
worktree и согласованные файлы. Интеграция и push — последовательно координатором.

Цикл: реализовать → проверить → обновить HIM/ACC и handoff → checkpoint →
GitHub → независимо проверить ref/tree. Использовать tools/checkpoint.py
с явным allowlist. Не добавлять секреты, личные базы, .venv, build/dist и ключи.
Не делать force push или push в main. Исторический PASS не распространяется
на новый artifact. NOT RUN/BLOCKED/FAIL не означает готовность.

Старые указания CP023 о shutdown, остановке соседней сессии и запрете новых
задач были разовыми и не действуют в новом запуске. Не выключать компьютер
и не повторять исторические команды. Не менять чужие процессы, данные и порты
8810/8811/8812. Полная готовность требует доказательств всех G0–G5.
