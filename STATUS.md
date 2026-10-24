# Состояние

24.10.2026 — создан каркас. Следующее: выбрать источник кода (обобщение
install.ps1/install.sh/Dockerfile JobPilot), описать манифест продукта, тесты.

24.10.2026 — аудит Rent: [docs/AUDIT-RENT-2026-10-24.md](docs/AUDIT-RENT-2026-10-24.md). Вывод: готовых Docker/Windows-установщиков в Rent нет; основа — JobPilot (setup.ps1, DataHome, полный архив). Переносимы отдельные механизмы Rent (проверка WinSW, backup-before-migration).

24.10.2026 — на шаге 1 реализованы: [манифест v1](docs/MANIFEST.md),
`shared/manifest.schema.json`, stdlib-валидатор `shared/manifest.py`, пример
`examples/jobpilot.manifest.json`, шаблоны `docker/Dockerfile.template`,
`docker/compose.template.yml` и рендер `shared/render_docker.py`.
Все шесть разрешённых исходников JobPilot прочитаны; источник, SHA-256 и границы
обобщения записаны в [docs/PROVENANCE.md](docs/PROVENANCE.md).

Тексты задают непривилегированный UID/GID 10001, read-only корень через Compose,
один том `/data`, публикацию порта на 127.0.0.1, HTTP healthcheck и restart policy.
Миграция и требование архива только декларируются; валидатор и рендер не
исполняют команды продукта, не читают базы/секреты и не обращаются к сети.

Фактически выполненные проверки на Windows:

- `python -B -m pytest --version`: Python 3.15 не содержит pytest
  (`No module named pytest`). Установка зависимостей не выполнялась.
- `py -3.14 -B -m pytest --version`: `pytest 9.1.1`.
- `docker version --format '{{.Server.Version}}'`: код 1,
  `permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`.
  Сборка и запуск контейнеров не выполнялись.
- Обычный полный запуск `py -3.14 -B -m pytest -q -p no:cacheprovider --basetemp .test-tmp`
  завершился с кодом 1: `PermissionError: [WinError 5]` при доступе к `.test-tmp`
  (включая завершение сессии pytest). Этот запуск не считается успешным.
- Изолированная проверка без файловых сценариев, с отключённым tmpdir plugin:
  `77 passed, 12 deselected in 0.70s`.
- Полный запуск после добавления последних сценариев через временный pytest-plugin
  `WorkspaceTemps` (команда приведена в `docs/MANIFEST.md`): код 0,
  **`94 passed in 3.96s`**. Plugin предоставляет `tmp_path` внутри
  `build/manifest-pytest/<uuid>` через обычный `Path.mkdir()`. ACL и настройки
  безопасности не менялись; все файловые сценарии включены. Сеть запрещена
  autouse fixture; сторонние pytest plugins, bytecode и кэш отключены.
- `py -3.14 -B -m shared.manifest examples/jobpilot.manifest.json`: код 0.
- `py -3.14 -B -m shared.render_docker examples/jobpilot.manifest.json --output-dir build/jobpilot-review`:
  код 0; сформированы и прочитаны `Dockerfile` и `compose.yml`.

**Готовых установщиков по-прежнему нет.** Dockerfile/Compose проверены текстовыми
тестами; реальная сборка, соответствие образа digest, запуск с read-only корнем,
install/update/restore/rollback, Windows/native, ACL/DPAPI и восстановление полного
архива не проверены. Команда миграции и порт в примере демонстрационные;
зависимости и адаптер JobPilot DataHome не поставляются.

Следующее: согласовать адаптер продукта, подготовить неизменяемый payload,
реализовать полный проверяемый архив и транзакцию миграции/отката, затем провести
Docker и native-приёмку. Коммит, push, deploy и изменение внешних исходников
в этом шаге не выполнялись.

24.10.2026 — **шаг 2: контракт манифеста v2**, по
[сообщению Rent](docs/REQUESTS-RENT-2026-10-24.md), без чтения/переноса его кода.
`shared/manifest.schema.json` и `shared/manifest.py` принимают v1 без изменений
и v2 с runtime (lock Windows/Linux, wheels/SHA-256, offline), внешними путями,
компонентами/heartbeat/restart, loopback-only web, дескрипторами секретов,
режимами и взаимным исключением, миграциями/backup/restore, staged update,
обязательной копией before-rollback и хуками. Проверяются ссылки и циклы
`after`, зависимости внутри режимов, запрет data/backup/services/config/runtime
в app/releases, неизвестные и содержащие значения секретов поля.

Добавлен [вымышленный пример Rent v2](examples/rent.manifest.json);
[JobPilot v1](examples/jobpilot.manifest.json) сохранён. Ручная миграция и
ограничения описаны в [docs/MANIFEST.md](docs/MANIFEST.md), происхождение —
в [docs/PROVENANCE.md](docs/PROVENANCE.md). Docker-рендер явно отклоняет v2:
шаблоны шага 1 не исполняют многокомпонентный контракт.

Проверки шага 2 на Windows/Python 3.14 (pytest 9.1.1):

- Обычный полный pytest с `--basetemp build/step2-pytest`: код 1,
  `331 passed, 14 errors in 6.66s`, отсутствовал родитель `build/` (`WinError 3`).
- После создания `build/`, полный pytest с `--basetemp build/step2-stock-pytest`:
  код 1, `PermissionError: [WinError 5]` в tmpdir/sessionfinish. ACL не менялись.
- Полный pytest через прежний документированный `WorkspaceTemps`/`tmp_path`:
  промежуточно `347 passed in 3.70s`, итоговый код 0,
  **`349 passed in 3.22s`**. Файловые тесты включены, сеть заблокирована,
  кэш/bytecode/сторонние plugins отключены. Точный stdout и команда — в MANIFEST.
- CLI-валидация обоих примеров: код 0 для JobPilot v1 и Rent v2.

Это проверка контракта и текстовых Docker-заготовок. Фактические SHA-256,
полнота lock/wheels, symlink/junction и цели каталогов не проверяются; данные
примера — заглушки. Docker и службы в шаге 2 не запускались по ограничениям
задания. **Установщики и install/update/restore/rollback по-прежнему не готовы**;
хуки, секреты, шифрование, backup-before-rollback, выбор режимов и надзор
сейчас только задекларированы. Commit/push/merge/deploy не выполнялись.

24.10.2026 — **шаг 3: DataHome и полный проверяемый ZIP**,
[контракт и точный вывод тестов](docs/ARCHIVE.md). Добавлены stdlib-модули
`shared/datahome.py` (постоянные пути v2, явный адаптер v1, проверка реальных
предков, запрет пересечений и ссылок, только план переноса) и `shared/archive.py`
(все обычные файлы/пустые каталоги всех постоянных корней, ZIP64/SHA-256,
проверка полного состава, восстановление в отдельный пустой каталог с повторным
чтением записанных файлов). Архив не публикуется при нехватке места,
нечитаемом/изменившемся источнике или ошибке проверки. Результаты не
перезаписываются, обход ZIP-путей запрещён. Продуктовые имена в коде не зашиты.

Четыре разрешённых исходника JobPilot прочитаны read-only, их SHA-256 и
точные границы обобщения добавлены в [PROVENANCE](docs/PROVENANCE.md).
Код написан заново; внешние файлы не изменялись и не исполнялись. Реальные
данные, секреты, auth caches и личные документы не читались.

Полный pytest на Windows/Python 3.14 через документированный `WorkspaceTemps`
(все тесты, сеть запрещена, сторонние plugins/кэш/bytecode отключены): код 0,
**`419 passed, 3 skipped in 16.16s`**. Пропуски: два реальных symlink-теста
без привилегий Windows и один тест путей Windows на POSIX. Проверки reparse
points/специальных файлов без повышенных прав выполнены. Базовый штатный
pytest дал `335 passed, 14 errors in 8.72s` из-за отсутствующего `build/`;
промежуточный новый набор — `1 failed, 401 passed, 3 skipped in 11.25s`
из-за неверного ожидания числа файлов в тесте (исправлено). Эти запуски
не считаются успешными. `git diff --check` — код 0. Docker Engine недоступен
(permission denied), сборка/запуск контейнеров не проводились; обязательная
контейнерная приёмка остаётся невыполненной.

**Готовых установщиков нет.** ZIP не зашифрован и не выполняет декларацию
Rent `backup.encrypted: true`; адаптер шифрования/ключей отсутствует.
Все писатели должны быть остановлены вызывающей стороной. Снимок живой БД,
блокировка операций, ACL/DPAPI, атрибуты/потоки ФС, исполнение хуков,
миграции, staged update, before-rollback и install/update/restore/rollback
как целая транзакция не реализованы и не приняты. Публикация архива требует
ФС с hard links; проверка Linux/junction/mount остаётся впереди. Реализованное
библиотечное восстановление в пустой каталог проверено локальными тестами.
Commit/push/merge/deploy, изменения Git metadata, конфигурации агентов,
ACL/настроек безопасности и внешних служб не выполнялись.
