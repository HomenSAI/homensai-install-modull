# Аудит продуктовых исходников JobPilot и Rent — 25.10.2026

**Карточка:** HIM-006. **Автор:** Serhii Khomenko. **Вид проверки:** чтение исходников и сверка GitHub refs, без запуска продуктов.

Аудит фиксирует доступные ветки, контракт данных и служб и границы следующей интеграции с общим установщиком. Требования HIM-006 по инвентаризации источников выполнены с явно указанной недоступностью локальных native-веток JobPilot. Это не приёмка установки, обновления, восстановления, отката, Windows-пакета или продуктового релиза. Канонический TODO обновляется отдельно.

## 1. Точные источники и границы проверки

| Продукт / источник | Зафиксированный SHA | Значение для этого аудита |
|---|---|---|
| JobPilot, `dev/onboarding-autopilot-080` | `43d079c477d3e6489c1960e59a84c4f539d8d37b` | Целевой продуктовый исходник; все ссылки JobPilot ниже закреплены за этим commit. |
| JobPilot, `main` | `84d85def25074dbf6a55e31f149b5640e9081464` | Отдельная ветка; не подменяет выбранный dev-источник. |
| JobPilot, `claude/busy-fermi-no1567` | `e8a4b2166156f454233c22d302e1f528948e2140` | Отдельная работа над cockpit; не новый installer baseline. |
| Rent, `main`, исходный снимок аудита | `843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0` | Контрактный baseline: `app/ctl/*`, архив, мигратор, `app/product.py` и документация прочитаны на этом commit. |
| Rent, `main`, повторно наблюдённый head | `a456f69480006b3a67558485368609b2d05874be` | Ветка продвинулась во время аудита; отношение к baseline проверено отдельно. Новая продуктовая приёмка не выполнена. |

GitHub compare для JobPilot dev → cockpit возвращает **diverged, ahead 4, behind 469**, merge base `bf0b295933051ec0f053c751563e100680b56042`. Изменения на стороне cockpit относительно общего предка: `cockpit/manifest.json`, `cockpit/snapshot.example.json`, `docs/COCKPIT.md`. Более новый по времени commit в этой ветке не является продолжением выбранного installer-источника. [Сравнение JobPilot][jobpilot-compare]

Rent `a456f694…` находится **на 8 commit впереди**, без расхождения с `843e3ae…`. В diff не менялись просмотренные `app/ctl/*`, `app/product.py`, `scripts/migrate.py`, `app/archive/restore.py`, `app/archive/password.py`, `docs/INSTALLER_CONTRACT.md`, `docs/INSTALL_CONCEPT.md`, `docs/BACKUP.md`. При этом добавлена `migrations/settlements/0036_investor_currency_accruals.sql` и изменён бизнес-код. Следовательно, описанные контракты не изменились в просмотренном diff, но набор миграций и байты следующего payload уже другие; считать новый head проверенным релизом нельзя. [Сравнение Rent][rent-compare]

Проверено происхождение **26 текстовых снимков**: их Git blob SHA совпадает с деревьями указанных commit после удаления ровно одного дополнительного конечного LF, внесённого транспортом сохранения. Иного нормализующего преобразования не применялось. Локальные снимки поэтому не объявляются побайтовыми release-артефактами. Это проверка соответствия просмотренного текста закреплённым источникам, а не проверка цифровой подписи поставщика.

В рамках аудита не выполнялись installer-скрипты, SQL, продуктовые тесты, сборка, Docker, systemctl, WinSW, обращения к рабочим приложениям или пользовательским данным. Порты **8810, 8811, 8812, 50378** не использовались. Чтение охватывает перечисленные в разделе 8 точки интеграции; это не полный аудит безопасности двух репозиториев.

## 2. Недоступные native-исходники JobPilot и риск build2

Продуктовый handoff прямо сообщает, что следующие worktree локальные, находятся под `.venv/worktrees/` и не опубликованы в GitHub. В этом окружении их исходники и готовые пакеты недоступны. SHA ниже — сведения handoff, а не независимо проверенные локальные checkout. [Handoff JobPilot][j-handoff]

| Локальная ветка / папка | SHA из handoff | Статус в этом аудите |
|---|---|---|
| `windows-native-package-20261023` | `94adc3d84f1ff53fd588b65eff871db6c4d47299` | Исходники недоступны; перенос исправлений в общий модуль не подтверждён. |
| `windows-native-runtime-20261023` | `86412bb5bbc7ef149f90ec3358283ce19c526685` | Исходники и managed LibreOffice payload недоступны. |
| `windows-native-audit-20261023` | `3975038db606579a9fbc47dfca89a2b7a227d2ac` | Первичные локальные отчёты и тестовое окружение недоступны. |

Сохранённое в handoff состояние **build2: FAIL rollback availability в 3 из 16 metadata-сценариев**. Размер Setup — **22 985 872 байта**, SHA-256 — `94c6f09c4d5ed8231d50bacf81eb78bd10276f7e0c12dac405c4d05d5846b51a`. Прошедшие другие сценарии установки, двух пользователей и обрывов публикации этого отказа не отменяют. В build2 отсутствуют последующий source guard и native PDF.

Позднейшее исправление package описано как source-only snapshot из 15 файлов, SHA-256 `9117c4df3d858efa1e6dd1f829fcdad29ba6c1357c015281e5ab56395c63821b`; затронуты `cold_update.py`, `update_transaction.py`, добавлен `update_legacy_guard.py`. Runtime-подготовка R4C/R6C описана отдельно: 9 файлов, SHA-256 `359b8fb251dbc4b3ea375b4cfde1de119b5797a8b94e53bc88b88e80a862f7c0`. Указанные там успешные проверки относятся к исходникам и среде разработки. Мы их не повторяли; они не доказывают исправление frozen Setup build2.

На момент handoff **build3 не запускался**. В доступных здесь источниках новая принятая сборка не предъявлена. Следующая native-приёмка требует конкретного нового payload/Setup с зафиксированным SHA, воспроизведения всех 16 metadata-сценариев и повторной проверки rollback, PDF, чистой Windows, прав и жизненного цикла именно этого payload. Лицензии, CRT redistribution, подпись и delivery sidecar остаются отдельными проверками; этот аудит их не закрывает.

## 3. JobPilot: подтверждённый контракт

### 3.1. Runtime, Docker и постоянные данные

В выбранном dev-источнике есть действующие исходники Docker-установки: `install.sh`, `install.ps1`, `Dockerfile`, `deploy/compose.yml`. Их наличие не подтверждает доступность неопубликованного native runtime. Скрипты проверяют существующую установку/instance и переводят обновление на отдельный workflow с проверенным полным архивом. Вызов старого installer-скрипта из нового адаптера означал бы запуск ещё одного lifecycle-контроллера с собственными эффектами. [J-install-sh][j-install-sh], [J-install-ps1][j-install-ps1]

Состав Compose: `app`, изолированный `render` и необязательный `worker` с профилем. `app` запускает `python -m backend.main`, внешний worker — `python -m backend.worker`; режим worker может быть inline/external. Render выполняет Word → PDF через LibreOffice и Unix socket, работает без сети и без DataHome/secret/archive mounts. У app/worker общие постоянные корни; перед резервированием и восстановлением нужно учитывать оба процесса и host bridge, а не только HTTP-сервис. [Compose][j-compose], [Dockerfile][j-dockerfile], [DB restore][j-restore-db]

`backend/platform/data_home.py` задаёт шесть корней: **`data`, `documents`, `configuration`, `secrets`, `history`, `installation`**. Основная БД — `data/platform.sqlite3`. Bootstrap `.env` содержит параметры привязки установки, путей и порта; runtime-настройки вынесены в `configuration/runtime.env`, ключи — в `secrets`. В Docker они отображаются в `/data`, `/documents`, `/configuration`, `/run/secrets`, `/backups`, `/installation`. `installation` у рабочего приложения подключён только для чтения. DataHome должен переживать замену программы и находиться вне её каталога. [DataHome][j-data-home], [Полный архив][j-portable], [Compose][j-compose]

Настройки поддерживают текущий installation master key и предыдущие ключи ротации. Это материал, необходимый для расшифровки сохранённых секретов; новый пустой ключ не заменяет утраченный прежний. Перенос обязан проверить фактическую расшифровку, а не только существование файла. `InstallationValidator` проверяет обязательные корни, SQLite snapshot, совместимость миграций, связи БД и `secrets.verify_all`. [Config][j-config], [Полный архив][j-portable]

### 3.2. Полный архив, восстановление и миграции

`backend.portable` имеет `create/verify/restore`. `create` требует `--offline`: остановку app и worker должен обеспечить вызывающий установщик. Параметр подтверждает намерение вызывающего кода, но сам по себе не является доказательством отсутствия writers. Архив принимает все шесть обязательных корней; SQLite сохраняется через snapshot API без копирования рабочих WAL/SHM как готовой БД. Защита по умолчанию — шифрование AES-GCM с ключом, полученным через Scrypt. Открытый архив допускается только с двумя явными подтверждениями. Пароль читается из stdin/терминала. [Полный архив][j-portable], [Формат архива][j-portable-format]

`backend.restore_home.activate` готовит **новый** DataHome в приватном соседнем каталоге: проверяет архив, перепривязывает пути, применяет миграции к кандидату, проверяет SQLite и checkpoint, устанавливает restore gate, переносит host requests из исполняемого пути в карантин. Публикация выполняется атомарным rename без замены существующей папки. Повтор разрешён только для той же ещё удерживаемой активации с совпавшими параметрами и хешами конфигурации. Службы эта функция не запускает. [Restore Home][j-restore-home]

DB-only recovery — отдельный контракт. `backend.restore_database.restore` использует exclusive product lease, приватного кандидата и журнал публикации, привязывает resume к исходному backup и конфигурации, сохраняет старые байты и проверяет секреты/оригиналы. Поле `rollback_database_readable` отдельно отражает читаемость сохранённой старой БД; наличие сохранённого файла не равнозначно здоровому rollback. Такой restore не обещает вернуть отсутствующие документы из DB-only backup. [DB restore][j-restore-db]

После восстановления удерживаются старые jobs, webhooks, sessions, archive requests и продуктовые автоматические действия. `restore_gate` разделяет подтверждение установки (`pending`) и подтверждение фоновой работы (`automation_pending`). Адаптер не должен автоматически снимать оба gate после успешного health check или общего согласия на распаковку. В частности, неопределённый исход внешней отправки нельзя превращать в повторную отправку при resume. [Restore gate][j-restore-gate], [Restore Home][j-restore-home], [Handoff][j-handoff]

SQL-мигратор проверяет нумерацию и checksum уже применённых общих/модульных миграций, запрещает неизвестные применённые миграции. Продуктовая схема и её проверки принадлежат JobPilot. Общий модуль должен обеспечить предварительный verified backup и доказанную остановку writers, а затем вызвать явно объявленный migration hook. Файл `backend/platform/installation.py` отвечает за диагностические отчёты/события установки; его название не означает наличие универсального installer lifecycle. [Миграции][j-migrations], [Installation diagnostics][j-installation]

### 3.3. Ограничения переноса в общий модуль

Compose публикует HTTP только на `127.0.0.1`; app/worker используют ограничения процесса, read-only root и restart policy. Однако `stop_grace_period: 30s` не даёт общего установщику доказательства чистой остановки до backup. Нужны отдельные результаты quiesce и проверки всех принадлежащих установке writers. Render и его шрифты/LibreOffice входят в runtime-контракт и не могут исчезнуть при сведении продукта к одному сервису. [Compose][j-compose], [Dockerfile][j-dockerfile]

Dockerfile фиксирует digest Python base и устанавливает Python-зависимости с `--require-hashes`; `UV_IMAGE` указан tag, а LibreOffice/fonts устанавливаются через apt без полного фиксированного набора package versions. Compose строит локальные image tags. Поэтому эти исходники сами по себе не образуют готовую подписанную offline-поставку с полным перечнем образов и digest. Следующий payload должен содержать собственный проверяемый inventory; вывод о принятой reproducible/release-сборке здесь не делается.

## 4. Rent: подтверждённый контракт

### 4.1. Два режима и разные границы runtime

Автономный режим использует три компонента: web — `python -m app.web`, bot — `python -m app.bot --scheduler-managed`, scheduler — `python -m app.scheduler`. Web по контракту доступен только на loopback, порт по умолчанию 8820. Порядок запуска: подготовка схемы → web → bot → scheduler; остановка в обратном порядке. [Installer contract][r-contract], [Services][r-services], [Worker][r-worker]

Platform-режим запускает `python -m app.core_server` и использует отдельную интеграцию `ServerProduct` из vendored platform. `app/product.py` объявляет продуктовые extensions, handlers, routes, migrations и archive dirs. Контракт фиксирует platform dependency 4.47.0 и синтетические данные до приёмки. **Автономный и platform-режимы нельзя одновременно направлять на одну БД и один Telegram token.** Их нельзя без проверки представлять двумя взаимозаменяемыми вариантами одних и тех же трёх сервисов. [Installer contract][r-contract], [Product][r-product]

`rent.toml` содержит пути и несекретные настройки: app/python/data/db/backups/services/releases и web. Постоянные данные, конфигурация, Python runtime и служебные каталоги находятся вне `app` и `releases/app-*`; `services/active.json` выбирает активный код. Для общего модуля это исходная схема для явного преобразования, а не право произвольно присвоить существующую установку. [Config][r-config], [Install concept][r-concept]

### 4.2. Секреты, архивы и миграции

`BACKUP_PASSWORD` должен быть одинаковым в окружении учётной записи службы и администратора, вызывающего backup/restore/update. Реализация читает его из process environment и, на Windows, из `HKCU\Environment`. Это делает identity пользователя частью переносимого контракта. Пароль не должен попадать в argv, журналы и product payload. Отдельно сохраняются `RENT_BOT_TOKEN`, ключ шифрования полей и recovery key; пароль архива не заменяет ключ данных. [Backup][r-backup], [Password provider][r-password], [Installer contract][r-contract]

Нужно различать три объёма данных:

| Механизм Rent | Что сохраняет | Чего не обещает |
|---|---|---|
| `scripts/migrate.py backup` / `app.ctl backup` | Согласованный SQLite snapshot в ZIP AES-256 с manifest/checksum. | Документы, конфигурацию, runtime, ключи и всю установку. |
| Бизнес-автоархив `app.archive` | БД, Excel, счета/реестры за выбранное окно, остальные активные зарегистрированные вложения `Data/<storage_key>`. | Незарегистрированные/удалённые файлы, счета за пределами окна, всю DataHome; ключи и пароль не включаются. |
| Будущий полный архив общего установщика | Должен явно охватывать все постоянные product roots и необходимые секреты через согласованный защищённый способ. | Пока не реализованным adapter mapping нельзя объявлять полный перенос Rent. |

Двойная ZIP-упаковка бизнес-архива скрывает имена вложений внутри зашифрованного внешнего файла. Manifest может содержать предупреждения о недоступных/изменённых файлах: проверка целостности архива не доказывает полноту исходного хозяйства. `app.archive.restore(..., data_dir=...)` восстанавливает включённые вложения только в **новую** папку; без `data_dir` восстанавливает только БД. Архив мигратора содержит лишь БД. [Backup][r-backup], [Archive restore][r-restore], [Migration runner][r-migrations]

Перед изменением БД restore проверяет пароль, checksum и SQLite, затем делает защитную копию. Повреждённую исходную БД и её хвосты реализация может сохранить рядом как corrupt evidence. При наличии `data_dir` файлы сначала записываются в новую папку, затем меняется БД; обработка исключений очищает новую папку. В прочитанном entrypoint нет общего durable протокола атомарной публикации пары «БД + папка» при hard kill. Поэтому повторно использовать его как готовый crash-safe full-restore без дополнительного wrapper/receipt нельзя. [Archive restore][r-restore]

Миграции forward-only, с проверкой checksum применённого SQL; откат — восстановление backup, а не произвольный обратный SQL. Функция `app.ctl.operations.prepare_database`, вызываемая контроллером, резервирует существующую БД **перед каждой следующей миграцией**. CLI `scripts/migrate.py up` делает backup перед своим набором pending migrations — это другой уровень группировки. Будущий hook должен явно выбрать требуемую гранулярность, закрепить полный migration plan digest и сохранить receipts. `--no-backup` обозначен продуктом как тестовый режим и не становится допустимым production-путём общего установщика. [Operations][r-operations], [Migration runner][r-migrations]

### 4.3. Lifecycle и службы

Windows-контроллер использует WinSW 2.12.0 x64; заданный в конфигурации/default SHA-256 проверяется перед выполнением wrapper. Default SHA: `05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da`. Service IDs фиксированы: `HomensaiRent-Web`, `HomensaiRent-Bot`, `HomensaiRent-Scheduler`. Это отличается от installation-scoped ownership общего модуля: адаптер обязан описать миграцию/отказ при конфликте, а не переименовать или присвоить найденную чужую службу. Проверка WinSW binary hash сама по себе не устанавливает ownership существующего SCM service. [Services][r-services], [Config][r-config]

Linux-ветка `app/ctl/services.py` **генерирует unit-файлы для ручного systemctl**. Автоматические команды управления в ней отклоняются; установленный/запущенный сервис из факта наличия template не следует. Template использует system scope, `User=rent`, `Group=rent`, `TimeoutStopSec=30`; explicit `SendSIGKILL=no` в нём нет. Это иной контракт, чем текущий developer port общего модуля для systemd user. Его нельзя выдать за готовую замену без решения об identity, правах, quiescence и путях. [Services][r-services]

`Controller.update` выполняет stop → before-update DB backup → staging/import checks → миграции → active pointer → start/health. При неудаче после переключения предусмотрена обязательная before-rollback копия текущей БД; если её создать не удалось, откат не продолжается. Restore оставляет сервисы остановленными. Health ожидает loopback `/healthz`, состояние схемы/данных/heartbeat; wrapper учитывает три отказа за десять минут и блокирует автоматические повторные запуски. [Operations][r-operations], [Worker][r-worker], [Installer contract][r-contract]

Существующий lock Rent — каталог `services/operation.lock`, а update/crash records имеют собственный формат. `worker.mark_stopping` записывает состояние, но не доказывает отсутствие живых дочерних writers. При переносе общий lifecycle lock, журнал, manager evidence и receipts должны остаться единым управляющим контуром; нельзя просто вложить `Controller.update/start/restore` в аналогичную операцию нового engine и считать блокировки/rollback согласованными.

**В просмотренном installer contract и `app/ctl/*` нет реализации Docker-установки Rent.** Указание в JobPilot handoff переиспользовать полный installer Rent после проверки совместимости — направление работы, а не подтверждённая готовность Docker backend Rent. Docker-упаковка Rent требует отдельного product descriptor, runtime/images, layouts, probe и приёмки. [Installer contract][r-contract], [Services][r-services], [Handoff JobPilot][j-handoff]

## 5. Следующий тонкий ProductAdapter: границы и обязательные данные

Опорный интерфейс общего модуля уже существует: `ProductAdapter.capabilities()` и `invoke(hook, request)`, JSON protocol `1.0` в `homensai_install/adapters.py`. Точка чтения общего репозитория — `db4b9f43d7e41e9e1e3a830e938926ebb4ce2675`; сами protocol/model происходят из baseline `a04c615b8b511710cf0035a27a239cbfb129db93`. Ниже — **требования к следующей продуктовой реализации**, а не сообщение о зарегистрированных JobPilot/Rent adapters. [SDK model](../homensai_install/api/model.py), [Executable adapter](../homensai_install/adapters.py)

`ExecutableProductAdapter` проверяет signed descriptor и payload inventory, не выполняет hooks при plan/validate, ограничивает JSON/timeout и отдельно хранит outcome. Текущая execution-реализация допускает **Linux native/user**; Windows execution и вызов такого adapter внутри Docker этим кодом не реализованы. Существование hooks не включает их автоматически во все lifecycle backend. Ожидаемый transport для остальных режимов должен быть отдельным проверяемым port с честной capability, без fallback на непроверенный host Python или shell.

| Часть контракта | JobPilot | Rent | Обязанность общего модуля |
|---|---|---|---|
| Descriptor и payload | Точный product release, app/worker/render, зависимости PDF, SQL и runtime. | Явный autonomous либо platform flavor; свой runtime, SQL, зависимости и entrypoints. | Проверить подпись/байты/target до execution; не определять продукт по имени каталога. |
| Logical roots | Шесть DataHome roots; основной SQLite и дополнительные root mappings только явно. | DB, data attachments, config, backup history, service state; код/runtime отдельно. | Проверять ownership, пересечения, ссылки и свободное место; не угадывать пути и не удалять user roots. |
| Secrets | Текущий/предыдущие master keys, runtime secret references, отдельный archive password. | Archive password service/admin identity, field/recovery keys, bot token. | Передавать только объявленные секреты защищённым каналом; исключить их из argv/log/plan и release inventory. |
| Writers и stop | App, внешний worker, host bridge; render как отдельная runtime dependency. | Web, bot, scheduler либо platform process; запрет параллельных flavor. | Удерживать один root lease; проверять owned PID/cgroup/container/service evidence и quiescence перед backup. |
| Полный backup | Product validation шести roots, decryptability и checkpoint. | Новое полное покрытие roots поверх DB/business archive, включая защищённое сохранение ключей/config. | Обязательная проверка полученного архива и durable receipt до destructive steps. |
| Restore / post-restore | Новая private candidate, quarantine host queue, restore gate и отдельная automation approval. | Согласованная DB/files candidate; без автоматического Telegram/сcheduler replay. | Публиковать только проверенный результат; оставлять неизвестный исход на reconciliation. |
| Health/readiness | Schema/checkpoint/secret decryptability и доступность render; health endpoint не равен permission на automation. | Schema/data/heartbeats/archive prerequisites и выбранный flavor. | Показывать structured факты и причины недоступности; не считать `Running` достаточным. |

Названия hooks сохраняются из существующего протокола:

1. **`describe`, `readiness`, `health`, `migration_plan`** возвращают ограниченный JSON без секретов. `capabilities()` получает статические данные из подписанного descriptor. План содержит точные текущую/целевую схемы, список SQL/checksum и prerequisite backup; никаких SQL-эффектов при plan.
2. **`quiesce`** выполняет только заявленный продуктовый переход к остановке работы и выдаёт проверяемые факты. Финальное отсутствие writers проверяет общий backend; строка `offline=true`, старый heartbeat или локальный флаг stopped достаточным доказательством не являются.
3. **`backup`** возвращает receipt с идентичностью операции, покрытием roots, digest архива и результатами product validation. Для Rent DB-only и business archive сохраняют собственные названия/ограничения; не выдаются за полный архив установки.
4. **`migration_apply`** требует `plan_digest` и `backup_receipt_id`, как уже требует transport. Продукт применяет только закреплённый план к выбранной БД/кандидату, фиксирует progress и доступный rollback input. Общий engine управляет порядком backup, переключения релиза и восстановления.
5. **`restore`, `post_restore`, `activate`** разделяют подготовку/проверку кандидата, его публикацию и разрешение пользовательской работы. Для JobPilot обязательны существующие restore/automation gates; для Rent нужен явный контракт удержания фоновых внешних эффектов после restore. Общее согласие на lifecycle operation не заменяет эти продуктовые подтверждения.
6. **`operation_status`** связывает durable product receipt с теми же `operation_id`, `idempotency_key`, request digest и payload/descriptor. Возвращает подтверждённое `applied`, `not_applied` либо неизвестный исход по схеме transport. Timeout/nonzero/malformed reply не разрешают автоматический повтор мутации. Пока продукт не умеет reconciliation, соответствующий hook не объявляется безопасно повторяемым.

Продуктовый adapter содержит product mapping, schema/archive/health hooks и связанные проверки. Регистрация служб, разрешения, payload switching, общий lock, consent, журнал lifecycle и backend routing остаются в общем установщике. `install.sh`, `install.ps1`, Rent `Controller.update` и отдельный GUI не становятся вложенными orchestrator. Действующее service SDK уже сериализует direct apply общим root lock; будущая engine-интеграция должна разделять lease, а не повторно брать тот же lock другим file descriptor. [Service manager](SERVICE_MANAGER.md)

## 6. Следующий проверяемый объём работ

Первый законченный adapter unit: подписанный synthetic descriptor и read-only `describe/readiness/migration_plan` для каждого выбранного product flavor, плюс явное перечисление roots, writers, secrets и недоступных capabilities. Эти данные позволяют согласовать интеграцию, не запуская продукты и не копируя их старые installer workflows.

Следующий unit — product-owned hooks на **изолированных синтетических данных** с неизвестным/прерванным исходом и receipt reconciliation: JobPilot full archive + retained restore gates; Rent полный roots mapping + DB/files candidate + encrypted secret recovery. До такого покрытия они остаются планом реализации. Новый Rent head с дополнительной миграцией требует нового inventory и проверки точного набора SQL; существующий snapshot не следует переименовывать в новый release.

Для допуска реального backend нужны проверки: первая установка и конфликт ownership; update с backup перед миграцией; interruption/resume; restore на новую машину с расшифровкой ключей; rollback с сохранением текущего состояния; отказ при живом writer/неизвестном stop; работа необязательных компонентов; разграничение двух установок/пользователей; uninstall без потери DataHome; отсутствие повторных внешних действий после restore. Linux/systemd, Windows/SCM и Docker принимаются в соответствующей ОС/runtime, на одном конкретном проверенном payload. Результаты unit/source tests не заменяют эту приёмку.

## 7. Итог по HIM-006

**Выполнено:** точные product refs, расхождение JobPilot cockpit, движение Rent main, карта постоянных данных/секретов/архивов/миграций/служб, недоступные native-источники, известный build2 rollback FAIL и контракт следующего тонкого adapter зафиксированы.

**Остаётся вне результата HIM-006:** получение локальных native worktree, принятие нового Setup, product-specific adapter implementation, полный Rent installation archive, Docker packaging Rent, live lifecycle acceptance, signing/licensing/security release gates. Эта недоступность не заполнена предположениями о готовности.

## 8. Проверенные источники

Все продуктовые ссылки ниже закреплены за полным commit SHA. Их содержимое читалось как исходный текст; команды из документов не выполнялись.

| JobPilot: `43d079c477d3e6489c1960e59a84c4f539d8d37b` | Роль |
|---|---|
| [Handoff 24.10][j-handoff] | Owner direction, local native refs, build2/source-only ограничения. |
| [Dockerfile][j-dockerfile] / [deploy/compose.yml][j-compose] | Runtime, компоненты, mounts и ограничения. |
| [install.sh][j-install-sh] / [install.ps1][j-install-ps1] | Существующие Docker install entrypoints и отказ fresh install поверх данных. |
| [data_home.py][j-data-home] / [config.py][j-config] | Пути, settings, master key references. |
| [portable.py][j-portable] / [portable_archive.py][j-portable-format] | Полное покрытие roots, product validation и формат архива. |
| [migrations.py][j-migrations] | Проверки и применение product SQL. |
| [restore_home.py][j-restore-home] / [restore_database.py][j-restore-db] / [restore_gate.py][j-restore-gate] | Candidate publication, DB-only recovery, подтверждения после restore. |
| [installation.py][j-installation] | Диагностический контракт установки. |

| Rent: `843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0` | Роль |
|---|---|
| [INSTALLER_CONTRACT.md][r-contract] / [INSTALL_CONCEPT.md][r-concept] | Границы переноса и product runtime. |
| [BACKUP.md][r-backup] | Объём архивов, секреты и восстановление вложений. |
| [app/product.py][r-product] | Platform flavor и product extensions. |
| [app/ctl/config.py][r-config] / [cli.py][r-cli] | Paths, active pointer и команды контроллера. |
| [app/ctl/operations.py][r-operations] | Lifecycle ordering, backups, rollback guard. |
| [app/ctl/services.py][r-services] / [worker.py][r-worker] | WinSW, systemd templates, wrappers и restart budget. |
| [scripts/migrate.py][r-migrations] | SQL/backup/restore runner. |
| [app/archive/restore.py][r-restore] / [password.py][r-password] | DB/files restoration и password provider. |

[jobpilot-compare]: https://github.com/HomenSAI/homensai-jobpilot-pro/compare/43d079c477d3e6489c1960e59a84c4f539d8d37b...e8a4b2166156f454233c22d302e1f528948e2140
[rent-compare]: https://github.com/HomenSAI/Homensai-Rent/compare/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0...a456f69480006b3a67558485368609b2d05874be
[j-handoff]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/docs/handoff/NEXT-AI-START-HERE-2026-10-24.md
[j-dockerfile]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/Dockerfile
[j-compose]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/deploy/compose.yml
[j-install-sh]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/install.sh
[j-install-ps1]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/install.ps1
[j-data-home]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/platform/data_home.py
[j-config]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/platform/config.py
[j-portable]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/portable.py
[j-portable-format]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/platform/portable_archive.py
[j-migrations]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/platform/migrations.py
[j-restore-home]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/restore_home.py
[j-restore-db]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/restore_database.py
[j-restore-gate]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/platform/restore_gate.py
[j-installation]: https://github.com/HomenSAI/homensai-jobpilot-pro/blob/43d079c477d3e6489c1960e59a84c4f539d8d37b/backend/platform/installation.py
[r-contract]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/docs/INSTALLER_CONTRACT.md
[r-concept]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/docs/INSTALL_CONCEPT.md
[r-backup]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/docs/BACKUP.md
[r-product]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/product.py
[r-config]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/ctl/config.py
[r-cli]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/ctl/cli.py
[r-operations]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/ctl/operations.py
[r-services]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/ctl/services.py
[r-worker]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/ctl/worker.py
[r-migrations]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/scripts/migrate.py
[r-restore]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/archive/restore.py
[r-password]: https://github.com/HomenSAI/Homensai-Rent/blob/843e3ae159a5d59a1e493b3fa6b1c0cf4a7d6ff0/app/archive/password.py
