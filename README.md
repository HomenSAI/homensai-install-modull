# HomenS.AI Install Module

Общий подключаемый модуль установки и обслуживания веб-приложений.
Автор: Serhii Khomenko / HomenS.AI. SDK `0.1.0`, API `1.0`.

**Статус: версия разработки; `production_ready=false`.**
Актуальная контрольная точка и текущие доказательства: [RESUME](docs/RESUME.md),
[IMPLEMENTATION_STATUS](docs/IMPLEMENTATION_STATUS.md).

Следующая сводка интеграции относится к историческому **CP-20261025-008**.
В общий SDK интегрированы Docker install/uninstall, исполняемый systemd user port
и coordinator подготовки зависимостей. Их source-проверки пройдены; настоящая
приёмка Docker, OS services и Windows остаётся открытой. Frozen DEV-установщик
Linux с собственным Python и reference SQLite-приложением проверен на
исторических artifacts CP-004/005; эти результаты не переносятся на новую сборку.

## Продолжить на другом компьютере

[Запуск с нуля](docs/START_HERE_RU.md) ·
[План SOL 6.1 High на 12 часов / 3 агента](docs/SOL61_12H_PLAN_RU.md) ·
[Готовое стартовое задание](SOL61_12H_START_PROMPT_RU.txt).
Это материалы следующего рабочего запуска; полная приёмка продукта ещё открыта.

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

## Targets и текущие проверки

Первая матрица разработки — **Windows 11 x64** и **Ubuntu 24.04 LTS x86_64**,
на каждой ОС native и Docker, во всех четырёх вариантах ru/en/de. Начальный
публичный scope — `user`; отдельные service profiles требуют собственной
интеграции и приёмки. Windows Server, другие дистрибутивы и ARM64 не входят
в первую матрицу. Подробности и доступные среды: [TARGET_MATRIX.md](docs/TARGET_MATRIX.md).

Проверки CP-008 на Linux / Python 3.12.14:

| Область | Результат |
|---|---|
| [Полный source suite](packaging/acceptance-evidence/source-20261025-cp008.json) | **1856 PASS, 1 skip, 10 subtests**, 70.24 s |
| Реальные Linux integration tests | **49 PASS, 5 subtests**, 43.23 s |
| SDK wheel, установленный вне checkout | **44 Python files, 18 resources**, 5 offline CLI вызовов, 0 network attempts |

Эти проверки имеют разный scope. Они не заменяют clean-host, frozen CP-008,
Windows, live Docker или systemd acceptance. Точные границы и исторические
результаты — в [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md);
побайтовая проверка SDK — в [receipt CP-008](packaging/acceptance-evidence/sdk-installed-20261025-cp008.json).

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
- [Prerequisite coordinator](docs/PREREQUISITES.md): signed catalogue, dependency
  order, отдельные согласия, auto/manual/recheck, проверенный downloader/cache и
  фиксированные adapters без shell; durable reboot/relogin и explicit resume.
  Он подключается явно через тот же SDK. Production registry, GUI auto-flow,
  реальные OS package installs и reboot/relogin acceptance ещё не завершены.
- [DockerEngine](docs/DOCKER_ENGINE.md) через общий BackendRouter: fresh install
  из заранее имеющихся pinned images, start/stop, encrypted backup и uninstall
  с сохранением данных, образов и signed recovery. Неизвестный RPC требует
  reconciliation; чужие ресурсы не присваиваются. Профиль ограничен локальным
  rootful Linux daemon и проверкой host/process identity; доступа к socket или
  членства в docker group недостаточно для её подтверждения. Получение/import
  образов, update/restore/rollback/repair/reinstall и live acceptance открыты.
- [Systemd user port](docs/SERVICE_MANAGER.md): регистрация, enable/disable,
  start/stop/status/uninstall собственных units, общий lifecycle lock и durable
  receipts. Native/user отказывает при зарегистрированных или незавершённых
  service resources. Общий `native/service` backend, Windows SCM и настоящие
  logout/reboot/autostart проверки пока не реализованы или не приняты.
- Подписанный executable ProductAdapter protocol, генератор тонкого подключения,
  Git checkpoint/resume tooling, точные build locks и отдельный fail-closed
  checker `tools/check_licenses.py`. Его обязательное включение в production pipeline
  ещё открыто; DEV builder не выдаёт production artifacts.

Область каждого доказательства, остаточные работы и ограничения приведены в
[фактическом статусе](docs/IMPLEMENTATION_STATUS.md) и
[журнале](docs/WORK_LOG.md). Development signature не заменяет production
подпись издателя и не делает сборку готовой к распространению.

## Интерфейс на трёх языках

Исторические снимки Linux DEV-мастера CP-004/005: [русский](docs/screenshots/wizard-ru.png),
[English](docs/screenshots/wizard-en.png), [Deutsch](docs/screenshots/wizard-de.png).
Это результаты проверенных действий с эталонным приложением; они не означают
завершённую приёмку Windows или Docker.

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
