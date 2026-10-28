# HomenS.AI Install Module

Общий модуль установки и обслуживания веб-приложений.
Автор: Serhii Khomenko / HomenS.AI. SDK `0.1.0`, API `1.0`.

**Версия для разработки: `production_ready=false`.** Модуль предоставляет
публичный Python SDK, CLI и локальный браузерный мастер на русском, английском
и немецком. Продукт подключается через подписанный manifest и адаптер;
копировать внутреннее ядро установщика в приложение не требуется.

## С чего начать

- [Публичный API](INSTALLER_API.md) — фабрика SDK, планы, согласия, операции и ошибки.
- [Руководство интеграции](docs/INTEGRATION_GUIDE.md) — подключение своего продукта.
- [Контракт ProductAdapter](docs/PRODUCT_ADAPTER_EXECUTION.md) — поддержанные действия,
  резервные копии и поведение при неопределённом исходе.
- [Сборка и инструменты](docs/BUILDING.md) — закреплённые версии и проверка зависимостей.
- [Эталонное приложение](reference/README.md) — веб-продукт с SQLite для проверки интеграции.

Машиночитаемое описание доступно в [api/discovery.json](api/discovery.json).
Документация, схемы, каталог ошибок и локализации входят в SDK wheel и доступны
без сети:

```sh
hinstall api describe --json
hinstall api schema manifest --json
hinstall init-adapter --help
```

Scaffold без настоящих artifacts и hashes создаёт явный DRAFT/BLOCKED.
Перед планированием вызывайте `capabilities(target)`: операция, перечисленная
в общем API, может быть недоступна конкретному backend.

## Текущие возможности

| Область | Реализация и ограничение |
|---|---|
| Публичный SDK / CLI / API | Строгие manifests, plan digest, согласия, журнал операций, идемпотентность и поиск незавершённой работы |
| Linux native, пользовательская сессия | Собственные subprocess, проверка PID и времени рождения процесса, loopback listener и readiness |
| ProductAdapter на Linux native/user | Новая установка и обновление с одним шагом миграции; start, stop, backup, restore, rollback, uninstall с сохранением данных |
| Браузерный мастер | ru/en/de, анализ среды, условия, review, пошаговый прогресс, обслуживание, ошибки и экспорт диагностики |
| Зависимости продукта | Подписанный каталог, auto/manual/recheck, проверяемая загрузка, локальный выбор файла, отдельные согласия и явное продолжение |
| Docker backend | Fresh install из уже имеющихся pinned images, start/stop, encrypted backup и uninstall с сохранением данных; полная приёмка ещё требуется |
| Службы | Отдельный systemd user port; общий `native/service` backend и Windows SCM не завершены |

Для зарегистрированного ProductAdapter `repair` и `reinstall`
пока возвращают отказ до продуктовых эффектов. Его возможности следует отличать
от legacy native и Docker backend. Docker update/restore/rollback/repair/reinstall,
получение образов и полная поддержка Windows остаются отдельными задачами.

## Сохранность данных и восстановление

Подпись Ed25519 и SHA-256 inventory привязаны к manifest, release, target и runtime.
Неизвестный или изменённый файл останавливает исполнение. Браузер не выбирает
произвольную команду, executable или environment.

Миграции существующей установки требуют настоящего зашифрованного архива,
который проверяется фактическим decrypt/restore. Перед явным restore или rollback
создаётся отдельный полный архив текущего состояния. Проверка связывает его
с установкой, операцией, исходным выпуском и выбранным архивом восстановления.

Потерянный ответ адаптера не разрешает повторить изменение. Продолжение исходной
операции сначала запрашивает `operation_status`; UNKNOWN и отсутствующий ledger
сохраняют отказ. После частичного replacement автоматический повтор запрещён,
а safety archive сохраняется для отдельного восстановления. После удаления код
восстановления выбирается по проверенной квитанции исходной операции и прежнему
журналу адаптера. Новое восстановление заново останавливает запись и создаёт
полную копию текущих данных. Обновление также
не выполняет автоматический разрушительный откат при ошибке health.

Диагностика остаётся на компьютере пользователя. Экспорт требует явного действия;
raw secrets и traceback не включаются в публичные поля ошибок.

## Поддержка платформ и выпуск

Первая целевая матрица: **Windows 11 x64** и **Ubuntu 24.04 LTS x86_64**, native
и Docker, каждый вариант на ru/en/de. Начальная область — `user`; service profiles
требуют самостоятельной интеграции. Другие дистрибутивы, Windows Server и ARM64
в эту матрицу не входят. Подробности: [TARGET_MATRIX.md](docs/TARGET_MATRIX.md).

Наличие исходного кода, успешного source suite или отдельной DEV-сборки не означает
полной приёмки этой матрицы. Перед производственным выпуском остаются обязательными:

- чистые целевые ОС без developer tools и полные сценарии GUI;
- реальные Docker и service lifecycle, reboot/relogin и сохранность после сбоев;
- интеграции продуктов, включая JobPilot и Rent;
- production trust, подписи издателя, утверждённые условия продукта и лицензии
  всех распространяемых компонентов, включая runtime.

DEV builder не выпускает production artifact. Проверка лицензий находится в
`tools/check_licenses.py`; доступность публичного исходника или название лицензии
сами по себе не доказывают право распространять точную сборку.

## Разработка

Версии и SHA-256 зависимостей закреплены в `requirements-build.lock`.
Устанавливайте их в отдельное virtual environment. Для соответствующей lock-файлу
версии CPython основные команды на Linux:

```sh
python -m pip --isolated install --require-hashes --only-binary=:all: -r requirements-build.lock
python -m pip --isolated check
python tools/check_build_lock.py --installed
python -m pytest -q tests
python -m pytest -q integration_tests
python -m build --wheel --no-isolation
```

Интеграционные проверки используют синтетические данные, собственные процессы
и loopback. Они требуют среды, которая разрешает эти действия.
Проверки установленного SDK запускаются отдельно от checkout; сборочные инструменты
для них также должны находиться вне дерева исходников.

[GitHub Actions](https://github.com/HomenSAI/homensai-install-modull/actions)
проверяет Linux source/integration, переносимую часть на Windows и установленные
SDK/reference сценарии Linux. Название каждого job указывает границы проверки;
Windows portable job не выполняет установку служб или приёмку Windows 11.

| Каталог | Назначение |
|---|---|
| `homensai_install/` | SDK, engine, доверие, GUI и диагностика |
| `api/` | Публичные схемы и OpenAPI |
| `shared/` | DataHome, архивы, update plan и генераторы |
| `reference/` | Эталонный веб-продукт, SQLite и профили версий |
| `packaging/` | DEV builder и проверки установленного пакета |
| `tests/`, `integration_tests/` | Source и реальные process/loopback проверки |
| `tools/` | Сохранение изменений, проверка лицензий, build lock и receipts |
| `windows/`, `linux/`, `docker/` | Платформенные компоненты и генераторы |

## Продолжение разработки

Для работы ИИ начальная инструкция — [AI_START_HERE.md](AI_START_HERE.md).
Архитектура и критерии реализации сохраняются в
[каноническом задании](HomenSAI_Universal_Installer_PROMPT_RU.txt) и
[списке задач](HomenSAI_Universal_Installer_TODO_RU.txt).
Исторические отчёты не подтверждают готовность новой сборки; текущие результаты
нужно проверять по точной ревизии и artifact.

© 2026 Serhii Khomenko · HomenS.AI
