# Публичный API HomenS.AI Install Module

Автор: Serhii Khomenko / HomenS.AI. Контракт API `1.0`, пакет `0.1.0`.

**Статус: рабочая версия разработки.** Этот документ описывает существующий
фасад и проверяемые ограничения. Наличие метода не означает приёмку Windows,
Docker, production signing или установщика на чистой системе. Фактические
возможности возвращает `capabilities()`, результаты — ToDo и receipts.

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
Bootstrap/разработчик создаёт `NativeEngine(verifier=..., secret_provider=...)`
и передаёт его в `InstallerAPI(..., engine=engine)`.

```python
from homensai_install.engine import NativeEngine  # bootstrap integration, не ProductAdapter
from homensai_install.trust import load_trust_config

verifier = load_trust_config("pinned-public-keys.json", allow_development=False)
engine = NativeEngine(verifier=verifier)
api = InstallerAPI("/absolute/private/installer-state", engine=engine)
```

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

```python
api = InstallerAPI(state_dir, engine=engine, prerequisites=coordinator)
capabilities = api.prerequisites_capabilities()
identity = api.prerequisites_identity()  # product/target/catalog/owner/manifest digest
terms = api.prerequisites_license(component_id)
plan = api.prerequisites_plan(choices, locale="ru", offline=True,
                              local_artifacts=verified_local_files)
# Host UI показывает plan и связанные условия. accepted_refs берутся из
# явных действий пользователя, а не из одного наличия plan_digest.
state = api.prerequisites_start(operation_id, plan, consent_refs=accepted_refs)
```

`operation_id` соответствует общему API ID: `[a-z][a-z0-9_-]{0,79}`.
Повтор `start` с тем же ID и планом читает сохранённый результат; другая операция
не обходит активный claim. Выполнение синхронное: это фасад существующего
coordinator, дополнительный executor и второй журнал эффектов не создаются.

| Метод | Поведение |
| --- | --- |
| `prerequisites_status(id)` | Историческое состояние без команд/discover |
| `prerequisites_events(id, after_sequence=0, limit=100)` | События и snapshot |
| `prerequisites_attempts(id, component_id, after=0, limit=100)` | Сохранённые pins и результаты прежних попыток |
| `prerequisites_recheck(id)` | Только probes и journal; ни download, ни installer |
| `prerequisites_resume(id, consent_refs=None)` | Явное продолжение допустимых pending действий |
| `prerequisites_choose_boundary(id, "now" / "later")` | Запись выбора reboot/relogin; сам host переход не выполняется |
| `prerequisites_cancel(id)` | Запрос отмены на безопасной границе |
| `prerequisites_replace_plan(id, new_plan, consent_refs)` | Новый review при изменившихся условиях; неизвестный эффект не стирается |

`READY` означает готовность выбранных prerequisites данной операции, а не
установленное приложение. Перед product install bootstrap повторяет discover
и строит новый reviewed product plan. GUI auto-flow, его неблокирующая host
интеграция, Windows elevation/session providers и production catalog остаются
отдельными непринятыми работами. Эти новые методы сейчас доступны SDK;
standalone CLI/HTTP не принимают команды, adapters или каталог из запросов.
Полный platform contract и ограничения: `docs/PREREQUISITES.md`.

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

Пакет не импортирует adapter по URL и не выдаёт ему admin-права.
Автоматическая регистрация executable protocol в lifecycle engine пока
не реализована; текущий native backend использует явные продуктовые команды
manifest v2 через проверенный runtime. Общий lifecycle не копируется в продукт.

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
