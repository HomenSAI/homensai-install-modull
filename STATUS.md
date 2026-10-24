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

24.10.2026 — **шаг 4: тексты служб и транзакция update/rollback на портах**.
Добавлены `shared/services.py`, `shared/update_plan.py`, тесты и
[docs/UPDATE.md](docs/UPDATE.md) с API, обязанностями адаптеров и точным stdout.
Манифест v2 поддерживает необязательный `winsw.version/sha256`: Windows-рендер
требует pin и успешную чистую проверку байтов до выдачи XML. Генераторы выбирают
режимы с mutual_exclusion, сортируют after, экранируют XML/argv/unit, задают
штатные delay/max/window и loopback-настройки, используют только имена секретов.

UpdatePlan выполняет stop → archive → stage → backup перед каждой миграцией →
migrate → pointer → start → health → post_update. При ошибке: stop → отдельная
обязательная before-rollback → restore (если исходный archive подтверждён) →
старый pointer → start → health. Ошибка before-rollback запрещает restore.
Состояние без env/команд/текстов исключений сохраняется атомарно в update.json
из манифеста; OS operation.lock сериализует управление. Возобновление повторяет
устойчивые ключи шагов; порты обязаны иметь durable идемпотентность. Старые
выпуски/runtime не удаляются; завершённый state автоматически не очищается.

Полный локальный pytest на Windows/Python 3.14 через прежний WorkspaceTemps:
код 0, **`566 passed, 3 skipped in 105.21s (0:01:45)`**. Сеть заблокирована
fixture, сторонние plugins/кэш/bytecode отключены; ACL не менялись. Пропуски
те же: два symlink-теста без привилегий Windows и один тест Windows-пути на POSIX.
Новый набор до последних 21 сценария: `126 passed in 93.05s (0:01:33)`, код 0.
Проверены ошибки всех фаз, симуляции crash до/после эффектов и checkpoints,
resume новым экземпляром фейка с дисковым ledger, contention, обязательная
before-rollback, pin, escaping, retention и отсутствие синтетических секретов.

**Установщики по-прежнему не готовы.** Нет реальных портов служб/БД/шифрования,
запуска команд/миграций, фактического bind/HTTP health, реального digest WinSW
или native/Docker-приёмки. Restart использует семантику SCM/systemd, не точный
скользящий надзор Rent; heartbeat/alerts не реализованы. Exactly-once внешних
эффектов зависит от durable портов; реальный kill/power-loss не тестировался.
ZIP шага 3 не заменяет шифрованный backup-порт. Основания — сообщение Rent и
локально записанный handoff JobPilot; внешний код в шаге 4 не читался/не копировался.
Службы, сеть, внешние MCP, commit/push/merge/deploy, изменение Git metadata,
конфигурации агентов и безопасности не выполнялись.

24.10.2026 — **шаг 5: локальный stdlib CLI**, [контракт и точный вывод](docs/CLI.md).
Добавлен `shared/ctl.py`: validate, plan-install (только списки каталогов/файлов),
render-docker, render-services, archive-create/verify/restore, update-plan-status.
Все операции используют существующие библиотеки. Для статуса добавлен публичный
`UpdatePlan.status()` без run/mkdir/lock/портов. Явные заглушки UpdatePorts/Docker
поднимают понятную ошибку; команд исполнения служб и Docker нет. Русские сообщения,
один JSON-объект с `--json`, документированные коды 0/1/2. Произвольные argv,
исключения ОС, содержимое/имена/хэши архивируемых файлов не печатаются.

Входные/выходные пути проверяются до доступа; общий checked_path дополнен
`ntpath.isreserved` для устройств Windows/недопустимых символов. Запрещены
ссылки/reparse, обходы, перезапись результатов рендера и пересечения restore
с любыми живыми корнями/app/releases, даже пустыми. v1 требует явных путей
адаптера без чтения env; v2 Docker по-прежнему отвергается. Windows XML требует
байтов и pin WinSW; план показывает имена XML без проверки будущего артефакта.

Полный локальный pytest на Windows/Python 3.14, pytest 9.1.1 через прежний
WorkspaceTemps: код 0, **`663 passed, 4 skipped in 79.07s (0:01:19)`**.
Все прежние и новые тесты включены; новые вызывают `main(argv)`, оба примера,
dry-run/рендер/ZIP round-trip/статус/ошибки/отсутствие синтетических секретов.
Сеть запрещена общей fixture, процессы — новыми тестами; кэш, bytecode и
сторонние plugins отключены. Три symlink-теста пропущены без привилегий Windows,
один Windows-на-POSIX неприменим. Reparse-тест без привилегий выполнен.

**Установщики по-прежнему не готовы.** CLI не останавливает писателей и не
сериализует ZIP-операции: это обязанность вызывающей стороны. ZIP не выполняет
`backup.encrypted: true` и не используется как backup-порт UpdatePlan. Рендер
не гарантирует реальную установку/запуск; возможен частичный результат при I/O
ошибке. Метаданные чужой ОС при перекрёстном рендере не проверяются. Статус —
чтение checkpoint, не запуск/возобновление update. Нет реальных адаптеров,
шифрования, миграций, Docker/native install/update/restore/rollback-приёмки.
Docker-проверка по CLAUDE.md не выполнялась из-за прямого запрета задания на
внешние службы. Внешние исходники, реальные credentials/auth caches, .env,
секреты и личные документы не читались. Внешние MCP, сеть, commit/push/merge/
deploy, изменение Git metadata, конфигурации агентов и безопасности не выполнялись.

24.10.2026 — **шаг 6: настоящие локальные FS-порты UpdatePlan**,
[API, сценарии, ограничения и вывод pytest](docs/E2E.md).
Добавлены `shared/adapters_fs.py` и `tests/test_e2e_fs.py`: полный доверенный
SHA-256 inventory payload и runtime pins, staging/publish в releases/app-<ver>,
атомарный JSON pointer, ZIP-порты через shared.archive, отдельная обязательная
before-rollback, повторяемое восстановление живых корней с сохранением текущих
update.json/pointer/operation.lock и durable receipts вне архивируемых корней.
Старые выпуски не удаляются. Существующая атомарная запись update.json сохранена.

OS lock дополнен метаданными held/released и stale detection только после
захвата OS lock, без отбора живого lock по возрасту/PID. На Windows блокируется
байт вне содержимого файла: прежний byte-0 препятствовал полному архивированию
operation.lock. Старый и новый Windows-протоколы нельзя использовать одновременно.

E2E явно дополняет публичный JobPilot v1 до тестового v2, создаёт локальный
fresh layout, выполняет ZIP/update и rollback после ошибки health-фейка;
проверяет новый before-rollback, восстановление данных/pointer, каждый checkpoint
и прямую/обратную фазу до/после crash, повтор с новыми FS-адаптерами, внутренние
границы публикации/restore, сохранение старого payload и отсутствие синтетических
env-секретов во всех записанных файлах/ZIP. Службы, миграции, health и hooks —
только внедрённые фейки. CLI-demo пропущен: явная адаптация v1 и durable фейки
не являются маленьким расширением CLI.

**Готовых установщиков по-прежнему нет.** ZIP не зашифрован: FS-порты по умолчанию
отказывают и требуют `allow_unencrypted=True` для локального эксперимента.
Требование v2 `backup.encrypted: true` этим не выполнено. Нет реальных служб,
миграций/HTTP, шифрования/ACL/DPAPI и Docker/native-приёмки. Crash моделируется
BaseException, не kill/power-loss. Restore нескольких корней не атомарен целиком:
после сбоя нужен resume при остановленных писателях. Полнота/совместимость реальных
Python/wheel-зависимостей не проверена; staging/scratch не очищаются автоматически.
Проверки локальные на Windows с прежним WorkspaceTemps, без изменения ACL.
Внешние исходники, реальные credentials/auth caches, .env, секреты и личные
документы не читались. Внешние MCP/службы, subprocess, сеть, commit/push/merge/deploy,
изменения Git metadata, конфигурации агентов и безопасности не выполнялись.

Полный локальный pytest шага 6: код 0, **`743 passed, 4 skipped in 804.23s (0:13:24)`**.
Включены все прежние тесты и 80 новых FS/E2E-проверок; три symlink-теста
пропущены без прав Windows, один Windows-на-POSIX неприменим. Точная команда,
stdout, промежуточные неудачные/успешные запуски и отдельная проверка последней
правки lock (`4 passed in 11.05s`) записаны в [E2E.md](docs/E2E.md).
Сеть запрещена fixture, subprocess — новым набором; кэш/bytecode/сторонние
plugins отключены. Docker-проверка по CLAUDE.md не выполнена: задание прямо
запрещает внешние службы. `git diff --check` и проверка новых файлов на
trailing whitespace — код 0.

24.10.2026 — **шаг 7: Compose-адаптер v1 и docker-plan**,
[API, ограничения, команда и точный stdout pytest](docs/DOCKER-ADAPTER.md).
Добавлены `shared/adapters_docker.py` и `tests/test_adapters_docker.py`:
фиксированные argv `docker compose -p <product> -f <file> up -d|stop|ps|logs`,
loopback-only HTTP health по URL манифеста, runner/fetcher как внедряемые callable.
По умолчанию возвращается план; эффекты требуют `execute=True` отдельно на
каждом вызове. Default runner — subprocess.run со списком argv, shell=False,
timeout, закрытым stdin и фиксированным окружением без наследования секретов;
COMPOSE_DISABLE_ENV_FILE=1 отключает автоматический `.env`. Ошибки и timeout
не включают stdout/stderr/исходные исключения. Fetcher без proxy/redirect,
успех только HTTP 200. Product ID/пути/действия валидируются; пути повторно
проверяются перед командой. CLI `docker-plan` выводит только dry-run текст/JSON
и не имеет `--execute`. Обновлены README.md и docs/CLI.md.

Адресная проверка адаптера вместе с прежним CLI: код 0,
`229 passed, 1 skipped in 7.98s`. Полный локальный pytest на Windows/Python 3.14
через прежний WorkspaceTemps: код 0,
**`875 passed, 4 skipped in 251.12s (0:04:11)`**. Включены все прежние и 132
новых сценария: каждый argv/execute-gate, ошибки/timeout команд, инъекции,
непередача синтетических env-секретов, health success/failure/timeout через
fetcher и CLI без процессов/HTTP/записи. Три symlink-пропуска связаны с правами
Windows, один Windows-на-POSIX неприменим. Общая fixture запрещает сеть,
новый набор — реальные процессы. Кэш, bytecode и сторонние plugins отключены;
ACL не менялись. `git diff --check` и проверка конечных пробелов — код 0.

**Реальный Docker, службы и HTTP не запускались. Приёмки нет; готовых
установщиков по-прежнему нет.** Адаптер принимает только v1, как render_docker;
полный UpdatePorts/FilesystemPorts v2 не подключён: нет durable receipts,
hooks, миграций, архивов/шифрования и транзакции Docker update/restore/rollback.
Нулевой код Compose/HTTP 200 не доказывает остановку всех писателей, владение
установкой или идентичность выпуска. YAML/образ не анализируются, несколько
установок одного product_id не разделены; реальные executable/daemon с
минимальным окружением и Docker/native install/update/restore/rollback не
проверены. Приёмка в Docker по CLAUDE.md не выполнена из-за прямого запрета
задания на внешние службы. Credentials/auth caches, .env, реальные секреты
и личные документы не читались. Внешние MCP, deploy, push, commit, merge,
изменения Git metadata, конфигурации агентов и безопасности не выполнялись.

24.10.2026 — **шаг 8b: чистый doctor и тексты Linux**.
Добавлены `shared/doctor.py`: проверка внедрённых фактов БД/схемы/записи/места,
heartbeat ≤ 180 с, архив ≤ расписание + сутки, три падения за 600 с;
структурированный report с русскими сообщениями и кодом 0/1, чистый лимитер
сигналов раз в час на компонент с внедрённым временем. Живой продукт не опрашивается,
stop и отправка уведомлений не выполняются. CLI: `doctor-eval --facts FILE`.

`shared/linux_sh.py` выдаёт только детерминированные тексты install/uninstall/status,
systemd units/override, создание непривилегированного пользователя и внешний
root-owned EnvironmentFile 0600 с комментариями только имён секретов.
CLI `render-linux` записывает тексты без исполнения; install.sh не запускает
службы, uninstall.sh сохраняет постоянные данные/секреты/учётную запись.
Контракты, ограничения и точный полный stdout: [DOCTOR](docs/DOCTOR.md),
[LINUX](docs/LINUX.md). Сценарии **сгенерированы, никогда не исполнялись**.

Полный локальный pytest на Windows/Python 3.14 через прежний WorkspaceTemps:
код 0, **`1019 passed, 4 skipped in 270.03s (0:04:30)`**; включены 144 новых
сценария, golden-тексты, hostile inputs и отсутствие синтетических значений
секретов. Сеть запрещена, в новом Linux-наборе запрещены процессы;
bytecode/кэш/сторонние plugins отключены, ACL не менялись.
**Готовых установщиков нет:** Linux/systemd, Docker, настоящие службы,
шифрование и install/update/restore/rollback не проверялись. Надзор и доставка
сигналов к ОС не подключены. Windows-генератор не изменялся. Внешние MCP,
commit/push/merge/deploy и изменения Git metadata/настроек не выполнялись.
24.10.2026 — **шаг 8: генератор текстов Windows PowerShell 5.1**,
[контракт, ограничения и проверки](docs/WINDOWS.md). Новый stdlib-модуль
`shared/windows_ps.py` формирует install/start/stop/status/uninstall/rollback,
отдельный secrets.ps1 и XML через существующий render_services. Добавлена
команда `ctl render-windows`. V2 требует явных mode и pin/байтов WinSW;
исходный Rent без pin и JobPilot v1 без native-контракта явно отклоняются.
Тексты задают внешние каталоги, ACL data/secrets только для владельца операции,
SYSTEM/Administrators, DPAPI CurrentUser/LocalMachine с запросом значений во
время будущего исполнения, проверку SHA-256 до использования WinSW,
delayed auto start и operation.lock с диапазоном Python UpdatePlan.

Семь сценариев прошли **только синтаксический разбор** настоящим Windows
PowerShell 5.1.22000.2538; сценарии не исполнялись. Rollback требует явных
доверенных портов продукта и отдельной проверенной before-rollback; портов
шифрованных копий/restore/activation/health в поставке нет. Доставка DPAPI
секретов службам, payload/runtime, миграции/hooks и crash-resume этого PS
сценария не реализованы. **Clean-Win11 install/update/restore/rollback-приёмка
отсутствует; готового Windows-установщика нет.** ACL/DPAPI/WinSW/SCM и Docker
не запускались. Commit/push/merge/deploy и внешние MCP не использовались;
doctor.py и linux/ этим шагом не изменялись.

Полный локальный pytest шага 8 через прежний WorkspaceTemps: код 0,
**`970 passed, 4 skipped in 388.69s (0:06:28)`**. Все прежние тесты и 95 новых
включены; три symlink-пропуска без прав Windows и один Windows-на-POSIX.
Точные команда/stdout, первая неудачная проверка ADS и адресный успешный запуск
записаны в [WINDOWS.md](docs/WINDOWS.md). Сеть/процессы в новых тестах запрещены,
кэш/bytecode/сторонние plugins отключены. `git diff --check` — код 0.
