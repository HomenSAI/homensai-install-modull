# HomenS.AI Install Module

Общий подключаемый модуль установки и обслуживания веб-приложений.
Автор: Serhii Khomenko / HomenS.AI. SDK `0.1.0`, API `1.0`.

**Статус: версия разработки.** На Linux x64 проверен настоящий frozen DEV
установщик с собственным Python runtime и эталонным веб-приложением с SQLite.
Windows native, полный Docker lifecycle и автоматический запуск через службу ОС
пока не приняты. Production release не готов.

## Два основных документа

- [Полный промпт реализации](HomenSAI_Universal_Installer_PROMPT_RU.txt).
- [Канонический ToDo: 106 задач и текущие доказательства](HomenSAI_Universal_Installer_TODO_RU.txt).

Рабочая ветка: `codex/universal-installer-20261025`. Три обязательных языка
мастера и обслуживания: **русский, английский, немецкий**.

## Подключение к своему проекту

Начинайте с [INSTALLER_API.md](INSTALLER_API.md) и
[руководства интеграции](docs/INTEGRATION_GUIDE.md).
Машиночитаемое описание: [api/discovery.json](api/discovery.json).
API, схемы, каталог ошибок и локализации поставляются внутри SDK wheel и
доступны офлайн; копировать внутреннее ядро в продукт не требуется.

```sh
hinstall api describe --json
hinstall api schema manifest --json
hinstall init-adapter --help
```

Проверяйте `capabilities(target)` перед планированием. Наличие имени действия
в API не означает, что конкретный backend уже исполняет его.
Scaffold без настоящих artifacts/hashes создаёт явный DRAFT/BLOCKED, а не
подставляет фиктивные пригодные к выпуску значения.

## Что реализовано

- Публичные SDK/CLI/API, строгие manifests, plan digest, согласия, журнал операций,
  идемпотентность и поиск незавершённых операций после закрытия браузера.
- Linux user-session lifecycle с настоящими subprocess, ownership PID/birth/pidfd,
  проверкой владельца слушающего порта и readiness приложения.
- Защищённый локальный web-мастер: ru/en/de, анализ среды, условия, review,
  общий и пошаговый прогресс, обслуживание, ошибки, статистика и support export.
- Ed25519 + SHA-256 inventory, привязанный к manifest/release/target/runtime.
  Неизвестные или изменённые файлы останавливают исполнение.
- Согласованные зашифрованные архивы AES-GCM/scrypt, сохранное удаление,
  проверка recovery payload и восстановление на принятом Linux сценарии.
- Каталог из 43 ошибок на трёх языках, SQLite diagnostics и ограниченный fallback.
- Безопасный downloader с hashes/size/host policy, offline cache; readonly
  prerequisites/manual recheck. Автоматическая установка ОС-зависимостей ещё открыта.
- Подписанный executable ProductAdapter protocol, генератор тонкого подключения,
  Git checkpoint/resume tooling, точные build locks и блокирующая проверка лицензий.

Область каждого доказательства, остаточные работы и ограничения приведены в
[фактическом статусе](docs/IMPLEMENTATION_STATUS.md) и
[журнале](docs/WORK_LOG.md). Development signature не заменяет production
подпись издателя и не делает сборку готовой к распространению.

## Разработка и продолжение

[BUILDING.md](docs/BUILDING.md) описывает закреплённые инструменты сборки,
изолированную среду, SHA-256 wheels и границы CI.
[RESUME.md](docs/RESUME.md) и [work-state.json](docs/work-state.json) задают
следующий шаг после обрыва. [CHECKPOINTS.md](docs/CHECKPOINTS.md) описывает
проверяемое сохранение одного commit в доступные remotes без force push.

| Каталог | Назначение |
|---|---|
| `homensai_install/` | Публичный SDK, engine, доверие, GUI, диагностика |
| `api/` | Проверяемые экспорты публичных схем и OpenAPI |
| `shared/` | Сохранённое внутреннее ядро: DataHome, архивы, update plan, generators |
| `reference/` | Реальный эталонный веб-продукт, SQLite, профили v1/v2/v3 |
| `packaging/` | DEV builder, браузерные сценарии, locks, audit evidence |
| `tests/` | Source tests с запрещённой сетью |
| `integration_tests/` | Отдельные реальные процессы и loopback HTTP |
| `tools/` | Checkpoint, license gate, проверка build lock и receipts |
| `windows/`, `linux/`, `docker/` | Исходные платформенные компоненты и генераторы |

История исходного каркаса сохранена в `STATUS.md`, `docs/origin/` и прежних
платформенных документах. Старые отчёты не являются приёмкой новых сборок.

© 2026 Serhii Khomenko · HomenS.AI
