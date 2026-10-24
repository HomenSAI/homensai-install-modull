# homensai-install-modull

Общий модуль установки HomenS.AI: Docker, Windows и Linux-установщики, которые
подключаются к любой программе семейства (JobPilot Pro, Rent и др.).

**Статус: каркас (0.0.1). Готовых установщиков в модуле ещё нет.** Это не
законченный пакет: в Rent нет Dockerfile/Setup.exe, а JobPilot-установщик и
native-ветки (NSIS, build2 — FAIL по откату) ещё не приняты. Ничего не
объявлять готовым без тестов.

## Структура

| Папка | Назначение |
|---|---|
| `docker/` | образ, compose, healthcheck, тома данных |
| `windows/` | PowerShell-установщик, лаунчер, DPAPI, ACL, позже Setup.exe |
| `linux/` | systemd/скрипты |
| `shared/` | общее: архив перед заменой, DataHome, миграции, откат, ключи |
| `tests/` | проверки установки/обновления/отката |
| `docs/` | требования, концепция; `docs/origin/` — материалы Rent (справочно) |

Цель интерфейса модуля: продукт задаёт манифест (имя, порт, папка данных,
команда миграций, healthcheck), модуль выполняет установку, обновление с
полным архивом, откат и удаление.

См. [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), [STATUS.md](STATUS.md).

© 2026 Serhii Khomenko · HomenS.AI
