# Публичный API HomenS.AI Install Module

Автор: Serhii Khomenko / HomenS.AI. Контракт API `1.0`, пакет `0.1.0`.

**Статус: рабочая версия разработки.** Этот документ описывает существующий
фасад и проверяемые ограничения. Наличие метода не означает приёмку Windows,
Docker, production signing или установщика на чистой системе. Фактические
возможности возвращает `capabilities()`, результаты — ToDo и receipts.
Реестр обязательных невыполненных проверок — `docs/VERIFICATION_BACKLOG.md`
и `docs/verification-status.json`. `NOT RUN` означает неподтверждённую готовность;
частичный PASS не закрывает полный G1 или release gate.

## Начало подключения

Подключайте закреплённую версию пакета `homensai-install-module` из проверенного
wheel/вашего разрешённого источника. Не импортируйте `shared.*` из продукта.
Старый namespace внутри поставки является внутренней реализацией.

Для разработки из этого checkout, с уже установленными build-зависимостями:

```sh
python -m build --wheel --no-isolation
python -m pip install --no-deps dist/homensai_install_module-0.1.0-py3-none-any.whl
hinstall api describe --json
hinstall api schema manifest --json
```

`--no-deps` не устанавливает необязательный криптографический backend. Для
подписи/проверки Ed25519 и зашифрованных архивов он должен быть подготовлен
из разрешённых закреплённых artifacts. Extra `[security]` описывает диапазон
совместимости; он не является готовым release/offline lock с hashes.

SDK использует Python 3.12+, фактическую проверенную версию указывают receipts.
Пользовательские frozen installers должны включать свой runtime; требование
Python для разработки SDK не является требованием к конечному пользователю.

```python
from pathlib import Path
from homensai_install.api.v1 import InstallerAPI

api = InstallerAPI(Path.home() / ".local/state/my-product-installer")
print(api.describe())
checked = api.validate("my-product.manifest.json")
print(checked["normalized_manifest"])
```

`state_dir` — отдельный приватный каталог владельца установки, вне payload.
На POSIX нужны права `0700`, база журнала — `0600`; symlink/reparse paths
отклоняются. API не создаёт каталог при `describe`, `schema` или `validate`.

## Версии и нормализация

| Версия | Назначение |
| --- | --- |
| `api_version=1.0` | Структура публичных вызовов/ответов |
| `module_version=0.1.0` | Версия установленного пакета |
| `adapter_protocol_version=1.0` | Контракт продуктовых hooks |
| `source_schema_version=1/2` | Исходный manifest |
| `model_version=1` | Внутренняя нормализованная модель публичного фасада |
| `error_catalog_version=1.0.0` | Офлайн-справочник ошибок |

Существующие v1/v2 валидируются настоящим `shared.manifest`. Нормализация
возвращает независимую копию и исходный digest. У legacy v1 нет достаточных
native paths/runtime/hooks: `native_complete=false` и явное предупреждение.
Модуль не подставляет выдуманные пути и не обещает native execution из v1.

`validate` проверяет структуру без запуска argv/hooks и сети. JSON имеет
ограничения размера/глубины/количества узлов; дубли ключей и NaN запрещены.
JSON schemas поставляются в wheel и доступны через `api.schema(name)`.
`api/schemas` и `api/discovery.json` в репозитории — проверяемые экспорты
поставляемых схем/метаданных, а не независимая альтернативная спецификация.

## Target, options и доверие

Target имеет поля:

```json
{"os":"linux","arch":"x64","mode":"native","scope":"user"}
```

Значения `windows/linux`, `x64/arm64`, `native/docker`, `user/service`
описывают пространство контракта. `capabilities(target)` определяет реально
доступное сочетание на текущем хосте. Поддержка target не выводится только
из его присутствия в JSON Schema.

У текущего `NativeEngine` публичные options включают:

- `install_root`: абсолютный безопасный корень своей установки;
- `release`: идентификатор конкретного выпуска;
- `modes`: выбранные manifest modes;
- `payload_dir`, `inventory_file`, `signature_file`: локальные проверяемые artifacts;
- `runtime_executable`: путь runtime внутри payload;
- `work_dir`: отдельный каталог recovery вне DataHome;
- `eula_digest`: SHA-256 показанного текста условий;
- `eula_locale`: `ru/en/de`; digest должен совпасть с подписанным `legal/<locale>.txt`;
- `backup_password_ref`: ссылка на защищённый provider/имя переменной, не пароль;
- `secret_refs`: отображение объявленных в manifest имён секретов в provider references;
- `archive_path`: архив для backup/restore/rollback;
- `acknowledge_data_loss`: явный выбор snapshot для late rollback.

Точные обязательные поля зависят от action. Неизвестный option и флаг
`verified=true` отклоняются. Доверие не задаётся произвольными options.
Доверенный bootstrap использует публичную фабрику `InstallerAPI.from_bootstrap`.
Она создаёт существующий общий router и проверяющий подпись backend; продукту
не требуется импортировать внутренний engine.

```python
from homensai_install.api.v1 import InstallerAPI

api = InstallerAPI.from_bootstrap(
    "/absolute/private/installer-state",
    trust_config="pinned-public-keys.json",
    allow_development=False,
)
```

Фабрика принимает только локальные настройки доверенного хоста: `trust_config`,
`allow_development`, `development_runtime`, `docker_cli_config`, `secret_provider`
и `locale`. Они не добавлены в plan options или HTTP contract. Development runtime
требует явного `allow_development=True`. CLI использует эту же фабрику; SDK сам
не читает секреты из окружения, provider при необходимости передаёт bootstrap.

Создание SDK читает закреплённые публичные ключи, но не проверяет продуктовый
payload. `validate` проверяет структуру, `discover` читает inventory, `plan`
сохраняет только собственный журнал плана. Ни один из этих методов не исполняет
продуктовые hooks. Подпись и полный набор байтов payload проверяет общий engine
непосредственно перед исполнением. Успешный plan не означает проверенный release.
Без `trust_config` планирование доступно, но исполнение не получает доверенного
verifier. Отдельная проверка подписанных условий до показа пользователю, как в
scaffold, по-прежнему требуется; фабрика её не заменяет.

Минимальный installed-package contract воспроизводится офлайн:

```sh
python packaging/check_installed_sdk.py \
  --work-dir /absolute/new-external-sdk-check \
  --receipt /absolute/sdk-check-receipt.json
```

Нужны уже подготовленные `pip`, `setuptools` и `cryptography`. Harness собирает
и устанавливает wheel в отдельный venv вне checkout, сверяет все package files,
вызывает публичный SDK и установленный `hinstall`, проверяет явный отказ для
недоступных действий и сохранение плана после пересоздания SDK. Клиент запрещает
чтение checkout, сеть и дочерние процессы; настоящий discovery выполняет только
временный bind выделенного loopback-порта. В ограниченном sandbox этот bind
может требовать разрешения среды, его нельзя подменять фиктивным discover.

Подписанный development fixture содержит настоящий собранный reference wheel
и явно неисполняемый runtime placeholder. Проверка подтверждает планирование и
отсутствие продуктовых эффектов; Python 3.14 runtime, install/update/restore/rollback,
Docker, Windows, service, GUI и production release этим результатом не приняты.
`plan` сохраняет собственный journal, поэтому «нет продуктовых эффектов» не означает
отсутствие записей состояния SDK. Receipt содержит source baseline и точные hashes.

### Явный выбор backend

По умолчанию facade использует `BackendRouter`: `native/user` направляется в
`NativeEngine`; Docker и scope `service` без регистрации возвращают
`CAPABILITY_UNSUPPORTED`. Отсутствующий backend не подменяется другим режимом.
Доверенный bootstrap может зарегистрировать `BackendRouter(native=...,
docker=..., native_service=...)`. Регистрация platform port не даёт capability,
пока его lifecycle engine не реализован и не прошёл свои проверки.

Router сохраняет mode/scope/OS/arch в reviewed плане; execute/resume выбирают
backend по этому сохранённому target. Старые native/user планы CP-005 совместимы;
для старого прямого Docker SDK плана нужен прежний explicit engine или новый
просмотр плана. Идемпотентность и resource claim остаются в общем `InstallerAPI`.

CLI и GUI принимают `--docker-cli-config` только при доверенном локальном
bootstrap. JSON имеет ровно `executable`, `sha256`, `endpoint`, `config_dir`:
абсолютный закреплённый executable, его SHA-256, явный локальный daemon endpoint
и приватный пустой config directory. HTTP не принимает эти поля и не выбирает
исполняемый код. CLI не ищет/не запускает daemon и не меняет Docker context.
Неверный переданный config завершает bootstrap структурированной ошибкой.

GUI дополнительно требует `--docker-payload-options`: локальный JSON с
`payload_dir`, `inventory_file`, `signature_file`, `release`, `runtime_executable`
и необязательным `product_version` для Docker target. Общие DataHome/secret refs
остаются отдельными options. `/terms/{locale}?mode=docker` проверяет условия
из этого подписанного Docker payload; переключение режима сбрасывает обзор и
согласие UI. Без этих artifact bindings мастер не предлагает Docker install.
Это подключение backend; live Docker/Windows acceptance описывается отдельно.

### Подготовка зависимостей через тот же SDK

Доверенный bootstrap может передать
`InstallerAPI(state_dir, prerequisites=coordinator)`, где `coordinator` —
`PrerequisiteCoordinator` с `SignedPrerequisiteCatalog`, фиксированными platform
adapters, downloader и read-only discover. Произвольный dict/callback вместо
coordinator не принимается. По умолчанию автоматический установщик зависимостей
не создаётся: нужны настоящие pins, тексты условий, publisher/platform policy.
Тот же явный coordinator принимает публичная factory
`InstallerAPI.from_bootstrap(state_dir, prerequisites=coordinator, ...)`;
это доверенная настройка приложения-хоста, не поле HTTP/manifest/options.

Зарегистрированный SDK наследует общий `ExecutionDomain` coordinator. Он
исключает конфликтующее product/prerequisite исполнение до enqueue и сохраняет
незавершённое владение после исчезновения процесса. Несколько trusted stores
могут получить один явный `execution_domain`; разные product API state directories
не обходят его. Настройка, границы гарантии и recovery-контракт:
[`docs/EXECUTION_LEASE.md`](docs/EXECUTION_LEASE.md). У SDK без регистрации/domain
остаются прежние локальные product claims.

```python
api = InstallerAPI(state_dir, engine=engine, prerequisites=coordinator)
capabilities = api.prerequisites_capabilities()
identity = api.prerequisites_identity()  # product/target/catalog/owner + verified payload binding
terms = api.prerequisites_license(component_id)
plan = api.prerequisites_plan(choices, locale="ru", offline=True,
                              local_artifacts=verified_local_files)
# Host UI показывает plan и связанные условия. accepted_refs берутся из
# явных действий пользователя, а не из одного наличия plan_digest.
state = api.prerequisites_start(operation_id, plan, consent_refs=accepted_refs)
```

Для зарегистрированного coordinator `capabilities.requirements` содержит 1–128
проверенных требований. Каждая запись имеет ровно поля `component_id`, `kind`,
`required`, `minimum_version`, `maximum_version`, `scope`. Обе границы всегда
присутствуют как строка либо `null`; scope равен `host`, `controller` или `product`.
Это отдельная проекция signed catalog: она не выполняет discover, не создаёт
операцию и не меняет сохранённый plan v1, его digest или consent refs.

Ограничения версий включительные. `null` означает отсутствие соответствующей
границы; две `null` — отсутствие ограничения версии. Поддерживается только
`v?MAJOR.MINOR[.PATCH][+build]`: отсутствующий PATCH равен нулю, префикс `v` и
build suffix не участвуют в числовом сравнении. Prerelease и PEP 440 этим
контрактом не поддерживаются. Равные числовые границы задают точную версию.

UI соединяет требования с `plan.components` по `component_id` после точного
совпадения `product_id`, `owner_id`, `target` и `catalog_digest`. Найденная версия
берётся из `plan.components[].detected_version`, результат проверки — из
`initial_status`/`initial_ready`, затем из серверного status/checks. UI не
сравнивает версии и не вычисляет readiness самостоятельно. Нестроковую,
некорректную или скрытую найденную версию следует показывать как неопределённую;
строки выводятся как обычный текст. В ответе старого сервера поле `requirements`
может отсутствовать: это означает недоступность сведений о требованиях, а не
разрешение любой версии. Старые клиенты могут игнорировать новую проекцию.

`operation_id` соответствует общему API ID: `[a-z][a-z0-9_-]{0,79}`.
Повтор `start` с тем же ID и планом читает сохранённый результат; другая операция
не обходит активный claim. По умолчанию `start` и `resume` синхронны. Долгоживущий
host может явно передать `background=True`: общий coordinator исполняется в
ограниченном фоновом потоке с тем же journal, claim и OS lock. Второй журнал
эффектов и самостоятельная машина состояний не создаются.

| Метод | Поведение |
| --- | --- |
| `prerequisites_status(id)` | Историческое состояние без команд/discover |
| `prerequisites_busy()` | Bool: в registered store есть claim или controller не доказанно свободен; чужие ID не возвращаются |
| `prerequisites_operations_list(unfinished_only=True, limit=100, after_id=None)` | Сводки операций exact owner/product/target/catalog; `next_cursor` для следующей страницы |
| `prerequisites_events(id, after_sequence=0, limit=100)` | События и snapshot |
| `prerequisites_attempts(id, component_id, after=0, limit=100)` | Сохранённые pins и результаты прежних попыток |
| `prerequisites_recheck(id)` | Только probes и journal; ни download, ни installer |
| `prerequisites_resume(id, consent_refs=None, background=False)` | Явное продолжение допустимых pending действий; фон только по opt-in |
| `prerequisites_choose_boundary(id, "now" / "later")` | Запись выбора reboot/relogin; сам host переход не выполняется |
| `prerequisites_cancel(id)` | Запрос отмены на безопасной границе |
| `prerequisites_replace_plan(id, new_plan, consent_refs)` | Новый review при изменившихся условиях; неизвестный эффект не стирается |

Фоновый `start` сохраняет план, подтверждения, claim и запрос запуска **до ACK**;
ответ содержит прежний `operation_id`, `plan_digest`, `catalog_digest` и
`execution={mode, state, active}`. Медленный fresh discover, загрузка и installer
выполняются после регистрации в потоке. ACK подтверждает принятую очередь;
он не подтверждает свежесть плана или готовность зависимостей. Перед первым
эффектом план точно сравнивается с заново построенным; незавершённая проверка
сохраняется и обязательна также после обрыва и явного `resume`.

| `execution.state` | Действие потребителя |
| --- | --- |
| `queued`, `running` | Продолжать polling, даже если snapshot пока `AWAITING_USER`; доступен cancel |
| `finished`, `failed`, `superseded`, `idle` | Поток завершил этот запрос либо запрос заменён; обработать `status`, `error`, `required_action` |
| `recovery_required` | Нет локальной очереди и controller lock доказанно свободен; требуется явное восстановление |
| `unknown` | Другой процесс может держать controller lock либо lock недоступен; `active=None`, повторить status и проверить контроллер |

При доказанном прерывании snapshot показывает `RECOVERY_REQUIRED` и исходный
`recorded_status`; чтение не меняет journal. Свободный lock **не доказывает**,
что внешний installer завершился: `INSTALLING`/`UNKNOWN` и точные receipts
остаются до read-only reconciliation. Повтор `start`, `status`, `events`,
`operations_list` и открытие нового API не запускают сохранённую операцию.
При ошибке создания потока или непредвиденном падении worker сохраняется
доступная для восстановления запись; `resume` требуется явно.

`operations_list` возвращает только сводки, без плана, consent refs, локальных
путей, steps/receipts и events. Identity-фильтр применяется **до** `limit`
(1–100); `after_id` — последний ID предыдущей страницы, `next_cursor=None`
означает конец. По умолчанию `READY`/`CANCELED` исключены. Каталог другой версии
не показывает старую операцию, но её активный claim продолжает запрещать новую:
для восстановления bootstrap должен открыть прежний подписанный каталог и
тот же original owner/state directory, а не освобождать claim принудительно.

Один prerequisite store получает не более одного локального worker; разные
`InstallerAPI.state_dir` не обходят это ограничение. OS lock и durable claim
остаются авторитетны между процессами. Поток живёт в процессе хоста и не
восстанавливается автоматически после его выхода. Короткоживущий CLI должен
использовать синхронный режим. Cancel возвращает сохранённый запрос без ожидания
установщика; остановка происходит на безопасной границе, без убийства процесса
и удаления общих компонентов. `recheck` пока синхронен и ничего не устанавливает.

`READY` означает готовность выбранных prerequisites данной операции, а не
установленное приложение. Перед product install bootstrap повторяет discover
и строит новый reviewed product plan. HTTP и CLI передают вызовы явно
зарегистрированному coordinator; production catalog/registry по умолчанию
не создаётся. GUI auto-flow, Windows elevation/session providers и реальные
platform installers остаются отдельными непринятыми работами.
Полный platform contract и ограничения: `docs/PREREQUISITES.md`.

### HTTP transport prerequisites

Доверенный host передаёт настроенный `api` в `WizardSession(api, manifest,
options, verifier=verifier)` и `create_server(session)`. Конструктор сравнивает
product/manifest/target и полный подписанный payload с coordinator.
`prerequisites_identity().payload_binding` содержит неизменяемые
`manifest_digest`, `target`, `release`, `runtime_executable`, `files_digest`.
Это проверенные при создании каталога байты, без абсолютных путей; идентичный
перенесённый payload допустим. Другой подписанный выпуск с тем же manifest — нет.
Owner и catalog digest закрепляются в сессии. Status/cancel не перечитывают
повреждённый или исчезнувший source bundle; исполнение всё равно перепроверяет
его в coordinator перед эффектами.

HTTP endpoints начинаются с `/api/v1/prerequisites/`: `capabilities`,
`licenses/{component_id}`, `plans`, `operations`, `operations/{id}` и
`events`, `attempts/{component_id}`, `cancel`, `resume`, `recheck`, `boundary`,
`replace-plan` под выбранной операцией. Сохраняются loopback Host/Origin/token
и bounded JSON проверки. Точные schemas находятся в OpenAPI.

`POST plans` принимает mode/locale/choices/offline и необязательный
`local_uploads` (artifact ID → verified opaque upload ID текущей HTTP-сессии). Сервер хранит до 16
неизменяемых планов и выдаёт opaque `plan_id` и заранее выделенный `operation_id`.
`POST operations` принимает этот plan ID/digest, явные consent refs,
`accepted_plan=true`, `accepted_terms=true` и digest показанных signed terms.
Полный plan, catalog/trust/argv/URL и local-artifact paths из HTTP запрещены.
Истёкший или потерянный до старта план строится заново без продуктовых эффектов.
После start полный план и согласия сохранены в coordinator; потерянный ACK
восстанавливается по тому же operation ID/list. Повтор start не является resume.

Start/resume используют `background=True`; recheck синхронно выполняет только
probes и журнал. Долгие проверки не держат общий session lock: status/cancel
доступны во время ожидания installer. Новый product submit/resume блокируется
при незавершённых prerequisites или ещё активном/неизвестном controller.
Guard сначала вызывает read-only `prerequisites_busy()`: старый catalog claim,
скрытый из exact-bound list, тоже блокирует product plan/submit. Probe возвращает
только bool зарегистрированного private store, без чужого operation ID или плана.
Начало подготовки и новое наблюдение READY аннулируют product review на сервере.
После READY нужен новый product plan и новое подтверждение; transport не
вызывает product submit автоматически. Это сессионный gate; глобальная
координация с произвольными внешними контроллерами им не объявляется.
Для зарегистрированного domain SDK дополнительно захватывает общую durable
execution lease до enqueue и повторно сверяет reviewed inventory после захвата.
Это закрывает промежуток между сессионным probe и исполнением для участников
того же domain. Неизвестный внешний effect после обрыва сохраняет владение;
отмена сама по себе не служит доказательством безопасного завершения.

При `durable_byte_progress=true` snapshot может содержать `download_progress`:
измеренные байты и событие `prerequisite_download` сохраняются атомарно.
100% подтверждает SHA-256 подготовленного файла, а не dependency READY.
Сохранённые байты не являются offset для resume; курсор events продвигается
по последнему полученному событию. Контракт: `docs/PREREQUISITES_PROGRESS.md`.
`gui_auto_flow=true` в discovery описывает реализованный условный JavaScript-flow.
HTTP capabilities подтверждают его только для явно зарегистрированного target;
без coordinator или для другого mode возвращается `gui_auto_flow=false`.
Это доступность исходного кода, а не browser/OS acceptance: `production_ready=false`.
Мастер разделяет dependency READY и product success, требует новые product
preflight/terms/review после подготовки. GUI contract: `docs/PREREQUISITES_GUI.md`.
Полный transport contract, ограничения и тестовые scopes:
`docs/PREREQUISITES_HTTP.md`.

### Явный локальный prerequisite-файл в мастере

При capability `local_artifact_upload=true` browser сначала создаёт allocation
через `POST /api/v1/prerequisites/uploads` с server `plan_id`, `plan_digest`,
`component_id`, `artifact_id`. Ожидаемые size/SHA-256 берутся из signed plan.
Затем `POST uploads/{upload_id}/content` принимает exact `application/octet-stream`
и ровно подписанный `Content-Length`; лимит HTTP-сессии — 16 записей и 4 GiB.
Host/Origin/session checks выполняются до чтения тела. Имя browser файла,
произвольный путь, команда, owner/domain/trust не принимаются.

`GET uploads/{upload_id}` читает состояние; `POST uploads/{upload_id}/cancel`
с `{}` отменяет ещё не закреплённый файл. Verified staged bytes означают только
успешную проверку размера/hash/повторного чтения. Они не запускают installer.
Новый `POST plans` с `local_uploads={artifact_id: upload_id}` создаёт новый SDK
план и новые согласия; исполнение использует прежние coordinator и lease.

HTTP `plan.local_artifacts` содержит opaque upload IDs, внутренний SDK plan —
приватные staging paths. `plan_digest` относится к внутреннему плану и
проверяется сервером по `plan_id`; browser не пересчитывает его по проекции.
SDK/CLI сохраняют прежний контракт. Cancel не удаляет bytes, уже удерживаемые
планом/операцией. Reload не исполняет ничего; после новой серверной сессии
незапущенные файлы выбираются заново, durable operation использует прежний plan.
Автоматическая межсессионная очистка retained/abandoned staging пока отсутствует.
Подробности и границы: `docs/PREREQUISITES_LOCAL_ARTIFACTS.md`.

### CLI transport prerequisites

Доверенный Python host вызывает `homensai_install.cli.main(argv, api=api)`.
Флаги/JSON не создают coordinator и не выбирают динамический import/runner.
Обычный CLI без регистрации честно сообщает unsupported. `start` и `resume`
синхронны; фон, погибающий при выходе CLI, не создаётся. Семейство
`prerequisites` покрывает plan/start/status/operations/events/attempts,
resume/recheck/cancel/choose-boundary/replace-plan и metadata/licenses.
Raw SDK plan или точный success envelope сохранённого CLI вывода проходят
строгую проверку coordinator; этот CLI review artifact не принимается по HTTP.

Для start/resume/recheck/status/events exit `0` требует READY без ошибки,
`1` означает failure/recovery, `3` — paused/not ready, `4` — canceled;
структурированная ошибка даёт `2`. Metadata и принятый control request могут
вернуть `0` как результат запроса, не как готовность зависимостей.
Примеры и полный контракт: `docs/PREREQUISITES_CLI.md`.

`engine` и `trust` — bootstrap integration API версии разработки; продуктовый
adapter не должен обращаться к их внутренним классам. Обычный потребитель
получает уже настроенный экземпляр facade. Без доверенного verifier выполнение
нового payload запрещено. Закрытые ключи не включаются в поставку.

Для изолированной разработки предусмотрены явные `--allow-development` и
`--development-runtime`; production-ready из такой проверки не следует.

## Основные методы

| Вызов | Результат и эффект |
| --- | --- |
| `describe()` | Версии/entrypoints/limits офлайн |
| `schema(name)` | Manifest/request/plan/operation/event/normalized schema |
| `capabilities(target)` | Реально доступные actions и причины ограничений |
| `validate(manifest)` | Source и normalized manifest; без effects |
| `discover(manifest, target, options)` | Ограниченный preflight и fingerprint; без install/hooks |
| `plan(action, manifest, target, inventory, options)` | Неизменяемый сохранённый план; запись только собственных метаданных |
| `get_plan(plan_id)` | Сохранённый план после повторной проверки digest |
| `submit(plan_id, plan_digest, consent_refs, idempotency_key=..., background=True)` | Принятая/существующая операция |
| `operations_list(product_id=None, unfinished_only=True, limit=100)` | Найти сохранённые операции после потери UI-сессии; только метаданные, без автоматического исполнения |
| `status(operation_id)` | Фактическое состояние и результат |
| `events(operation_id, after_sequence=0)` | Страница событий, snapshot и новый cursor |
| `cancel(operation_id)` | Cooperative cancel для живого worker; неактивное recovery-ожидание закрывается без отката эффектов |
| `resume(operation_id, decisions, background=True)` | Только явный engine recovery из допустимого состояния |
| `contract_test(manifest, target, options)` | Read-only проверка контракта; не installed-приёмка |

Actions: `build`, `install`, `update`, `modify_components`, `repair`, `reinstall`,
`uninstall`, `backup`, `restore`, `rollback`, `start`, `stop`. Недоступный action
возвращает `CAPABILITY_UNSUPPORTED`, а не фиктивный успех.

## План, согласия и повтор запроса

```python
target = {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"}
inventory = api.discover("product.json", target, options)
plan = api.plan("install", "product.json", target, inventory, options)

# UI сначала показывает действия/условия и получает явное согласие человека.
accepted_refs = [plan["review_consent_ref"], *plan["required_consents"]]
operation = api.submit(
    plan["plan_id"], plan["plan_digest"], accepted_refs,
    idempotency_key="unique-client-request-id", background=True,
)
```

Пример `accepted_refs` показывает формат уже полученного согласия. Нельзя
автоматически принимать EULA, автоматически отмечать checkbox или считать
знание digest разрешением на установку. GUI обязан отделять user choice от
построения плана. `required_consents` включает требования engine, а
`review_consent_ref=plan:<digest>` фиксирует рассмотренный план.

План сохраняется в приватном журнале как canonical JSON с SHA-256. Изменение
возвращённого dict не изменяет сохранённый план. Перед исполнением повторяется
discover; изменившиеся существенные факты дают `PLAN_STALE`. Проверка payload
не ограничивается планированием — engine повторяет её до effects.

Один idempotency key с тем же запросом возвращает ту же operation, в том числе
после перезапуска SDK и изменения состояния вследствие первой операции.
Тот же ключ с другим запросом даёт `OPERATION_CONFLICT`. Конкурирующие
операции одного install root сериализуются. Это не обещание exactly-once
произвольного внешнего hook; для него нужны receipts и product recovery.

Background worker живёт в процессе GUI/SDK. Закрытие этого процесса может
прервать исполнение; журнал отмечает оставшуюся операцию `RECOVERY_REQUIRED`
после обнаружения исчезновения owner. CLI submit/resume выполняются синхронно,
чтобы выход CLI не уничтожил только что запущенный daemon thread.

## Состояния, события и восстановление

Основные состояния: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`FAILED_ROLLED_BACK`, `CANCELED`, `RECOVERY_REQUIRED`, `AWAITING_USER`,
`REBOOT_REQUIRED`, `RELOGIN_REQUIRED`.

`FAILED_ROLLED_BACK` — неудачное обновление с успешным восстановлением.
Ожидание reboot или ручного действия не является `SUCCEEDED`.

События имеют `operation_id`, `event_id`, возрастающий `sequence`, `timestamp`
и `type`. Клиент дедуплицирует их по ID/sequence и сохраняет cursor.
`events` возвращает максимум 1000 записей; следующий вызов начинает после
`next_sequence`. Повтор страницы не запускает операцию заново.

После перезапуска bootstrap его случайный порт и браузерная sessionStorage
могут измениться. Клиент восстанавливает список из авторитетного journal через
`operations_list`, а затем по выбору человека читает `status`/`events` нужной
операции и вызывает явный `resume`. Ни список, ни чтение событий не возобновляют
effects. Исчезнувший owner для `RUNNING`/`QUEUED` отмечается
`RECOVERY_REQUIRED`; отсутствие каталога состояния даёт пустой список и не
создаёт файлов.

Ответ имеет форму `{"operations": [...], "has_more": false, "limit": 100}`.
Элемент содержит только `operation_id`, `plan_id`, `action`, `status`,
`product_id`, `updated_at`. Параметр `limit` принимает 1–200; список упорядочен
по времени обновления, затем ID. `has_more` сообщает о дополнительных записях;
cursor-пагинация этого списка пока не реализована. При `has_more=true` ответ
нельзя считать полной историей; можно сузить SDK-запрос по продукту, а доступ
к более чем 200 записям одного продукта остаётся ограничением этой версии.
Незавершёнными считаются нетерминальные статусы,
а также операция с `result.resume_required=true`, включая отменённую
незаконченную транзакцию. `unfinished_only=False` добавляет завершённые записи.
Для HTTP `GET /api/v1/operations` product ID берётся из startup manifest;
подмена продукта запросом не допускается. CLI использует
`hinstall operations --product-id rent --limit 100 --json`; `--all` включает
завершённые операции.

Journal API хранится отдельно от DiagnosticStore. Если авторитетную запись
сохранить нельзя, API не выполняет новый опасный переход. Если engine потерял
управление после effects, ошибка не маскируется автоматическим повтором.
`resume` разрешён только при наличии реального проверяющего engine метода;
он не является простым повторным вызовом любого shell hook.

## Ошибки, диагностика и статистика

SDK выбрасывает `homensai_install.errors.InstallerError` со стабильным `code`.
`error.to_dict(locale="ru"|"en"|"de")` возвращает безопасное объяснение.
Произвольные argv/stdout/environment в публичные события не переносятся.

Доступны:

```python
api.errors_describe("PLAN_STALE")
api.errors_search("Docker", limit=20)
api.diagnostics_get(operation_id)
api.statistics_query(product_id="my-product")
# Явный экспорт после просмотра состава; это не скрытая телеметрия.
preview = api.diagnostics_export_preview(operation_id)
# Покажите пользователю preview; после подтверждения используйте его digest.
api.diagnostics_export("/private/report.json", operation_id=operation_id,
                       confirmed=True, expected_digest=preview["sha256"])
```

У diagnostic storage собственная политика fallback/retention. Его сбой не
превращает сбой установки в успех. Счётчики разделяют операции и попытки.

## CLI

```sh
hinstall api describe --json
hinstall api capabilities --os linux --arch x64 --mode native --scope user --json
hinstall validate --manifest product.json --json
hinstall discover --manifest product.json --options options.json --json
hinstall plan install --manifest product.json --options options.json --json
hinstall status op_example --json
hinstall events op_example --after-sequence 0 --json
hinstall errors --code PLAN_STALE --locale de --json
```

Вместо `op_example` нужен ID, реально выданный submit. Запрос submit требует
`--plan-id`, `--plan-digest`, `--idempotency-key` и повторяемый `--consent-ref`.
Параметры не показываются в parser error, чтобы случайно не раскрыть секрет.
CLI secret provider читает только явно указанное имя переменной окружения
или reference вида `env:NAME`. В options передаётся имя, значение не
добавляется в argv/журнал. GUI использует отдельный provider памяти сессии.

stdout содержит один JSON envelope: `api_version`, `request_id`, `result`
или `error`. Ошибки/неуспех имеют ненулевой exit code; человеческий `--help`
является отдельным режимом. Машинные потребители не разбирают русские строки.

`hinstall gui ...` передаёт аргументы локальному графическому bootstrap.
`--trust-config` задаётся при старте доверенным bootstrap, а не по HTTP.

## ProductAdapter и независимое подключение

`ProductAdapter` — protocol с `capabilities()` и `invoke(hook, request)`.
Для каждого объявленного hook требуется documented request/response,
precondition/apply/verify и допустимая компенсация. При миграции БД это
проверенный backup/restore, а не универсальное обратное SQL.

Реализация `homensai_install.adapters.ExecutableProductAdapter` предоставляет
явный signed-descriptor/JSON-subprocess protocol с ограниченными argv,
stdin/stdout/stderr, deadline, env allowlist и отдельным durable журналом
подтверждённых callbacks. Статический `capabilities()` не запускает процесс.
Неопределённый исход эффекта требует `reconcile`, а не blind retry. Execution
backend этой реализации пока поддерживает Linux; Windows требует отдельного
Job Object backend и приёмки. Версия/entrypoint доступны в discovery.

Пакет не импортирует adapter по URL и не выдаёт ему admin-права. Доверенный host
может явно включить `NativeEngine(verifier=signed_verifier,
product_adapter_descriptor="installer/adapter.json")`. Эта регистрация доступна
только для Linux/native/user и действий `start`, `stop`, `backup`; SDK по умолчанию
её не включает. Остальные действия при включённом adapter заранее отклоняются.
Без регистрации сохраняется прежний native путь manifest v2.

План закрепляет descriptor и verified release; перед эффектами binding проверяется
заново. Readiness обязана наблюдать нужные release/installation_id и свежий probe,
сохраняя проверки owned PID/listener/HTTP. Quiesce использует durable intent и
существующий adapter journal: UNKNOWN, потерянный ledger или отсутствующая
ожидаемая invocation не разрешают повтор. Backup остаётся настоящим полным
проверенным зашифрованным архивом прежнего ядра. Миграции, fresh-empty и полный
install/update/restore lifecycle через этот adapter ещё не реализованы.
Подробный контракт: `PRODUCT_ADAPTER_EXECUTION.md` в ресурсах установленного SDK
и [документ проекта](docs/PRODUCT_ADAPTER_EXECUTION.md). Общий lifecycle в продукт
не копируется; полная приёмка HIM-015/023 остаётся открытой.

`hinstall init-adapter --output-dir NEW_DIR --spec product-spec.json --json`
создаёт подключение из переданных реальных payload/runtime/wheels/locks,
вычисляя SHA-256. SDK-вызов —
`homensai_install.scaffold.scaffold_product(output_dir, spec)`.
При недостающих inputs выдаются `BLOCKED`, schema-invalid manifest DRAFT и
список недостающего; фиктивных хешей нет. Полный комплект остаётся
`REVIEW_REQUIRED`, неподписанным и `production_ready=false` до release gates.
Формат spec, descriptor, протокол callbacks и порядок независимого подключения
описаны в [руководстве интеграции](docs/INTEGRATION_GUIDE.md).

Приёмка нового продукта: установить wheel вне checkout ядра, прочитать этот
документ и schemas, описать продукт, выполнить read-only contract-test,
собрать/проверить реальные artifacts на заявленных targets и полный lifecycle.
Только read-only `contract_test` не закрывает HIM-064/083 полностью.

## HTTP transport и ограничения

[`api/openapi.yaml`](api/openapi.yaml) — OpenAPI 3.1; файл записан в JSON,
который является допустимым YAML. HTTP — локальный транспорт того же facade.
Для всех `/api/v1/*` нужен header `X-HInstall-Token` текущей bootstrap-сессии.
Controller дополнительно проверяет exact Host/Origin и не включает wildcard
CORS. Нет endpoint для подписания, build или произвольной elevated команды.

Wizard закрепляет manifest/ключи при старте; его HTTP-запросы передают выбор
mode/path/action/locale и согласия, а не произвольные manifests/trust roots.
Поэтому HTTP request schemas описаны отдельно от generic SDK request schema.
`POST /api/v1/secrets` принимает `archive_password` только в память защищённой
сессии; SDK получает ссылку `wizard_archive`. Значение не хранится в журнале
и не возвращается в ответе. После перезапуска пароль требуется заново.

JSON Schema ограничивает допустимую структуру, но не заменяет проверки
согласия, владения ресурсом, подписи, актуальности плана и capabilities.

## Проверки и честные ограничения

`tests/test_public_api.py` проверяет фасад с внедрённым ограниченным test engine:
планирование без effects, stale checks, согласия, повтор/конкурентный запрос,
durable journal, resource claims, безопасный cancel, события и ошибки CLI.
Это source/contract evidence, а не доказательство установленной Windows/Linux
системы. Настоящий native backend и GUI имеют отдельные tests/receipts.

До соответствующей installed-приёмки не заявляются все четыре готовых
установщика, server scope, системные prerequisites, production trust или
независимая интеграция JobPilot/Rent. Этот список должен обновляться вместе
с фактической реализацией и canonical ToDo.

### Отмена неактивной попытки

`cancel` атомарно различает живой RUNNING/QUEUED worker и неактивное
RECOVERY_REQUIRED/AWAITING_USER/REBOOT_REQUIRED/RELOGIN_REQUIRED. Для worker
ставится cooperative request. Неактивная попытка становится CANCELED, API claim
освобождается, но engine recovery receipts и уже выполненные изменения сохраняются.
Ответ содержит `resume_required=true`, `effects_undone=false`. Новый план отдельно
проверяет native/Docker recovery interlocks; отмена не разрешает обход partial install.
При гонке с уже поставленным в очередь resume отмена остаётся cooperative.
Если cancel вернул RUNNING/QUEUED, клиент продолжает получать events/status;
завершение операции этим ответом ещё не подтверждено. Проверка исчезновения
owner меняет journal только при совпадении ранее наблюдённого snapshot, включая
возрастающий sequence. Задержанный status не перезаписывает новую попытку даже
при resume в том же процессе.
Повторная отмена terminal operation не меняет историю и не создаёт второй event.

### Явная подготовка миграции в SDK

`InstallerAPI.prepare_migration(action, manifest, target=None, options=None)` —
отдельный явный SDK-вызов для `install`/`update`. Он делегирует только явно
зарегистрированному backend, который реализует `prepare_migration`. Общие
`discover`/`plan` не вызывают этот метод или продуктовые hooks. Backend без
подготовки возвращает `CAPABILITY_UNSUPPORTED`; fallback на другой target нет.
NativeEngine реализует подготовку только при явном trusted ProductAdapter opt-in
на Linux/native/user. Под общей lifecycle lock проверяется исходная установка,
вызывается подписанный read-only migration_plan и сохраняется immutable review.
Fresh preparation создаёт только installer ownership reservation и пустые каталоги,
без active release и продуктовых данных. Неизвестный/неразрешённый прежний журнал
требует reconciliation. Наличие review не означает готовность adapter install/update.

Результат поддерживающего backend: `migration_review_id`, `action`,
`product_plan` (from_version/to_version/steps/plan_digest), `from_release`,
`to_release`, `installation_id`, `binding_sha256`. Оба ID/digest — 64 hex-символа.
`options.release` должен совпасть с `to_release`; fresh имеет from_release=null,
update требует существующий from_release. При preparation старый review ID
передавать нельзя. Последующий общий `plan` принимает только opaque
`options.migration_review_id`, а не путь к review-файлу или callback.
Сам review не заменяет обязательные согласия перед lifecycle-исполнением.
CLI/HTTP подготовку не предоставляют автоматически; произвольный subprocess
через этот метод не выбирается. Доступность фактического backend — отдельный gate.
