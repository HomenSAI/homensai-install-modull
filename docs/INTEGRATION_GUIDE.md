# Подключение нового продукта к общему установщику

Новый проект подключает `homensai-install-module` и описывает собственные компоненты, пути, данные и продуктовые команды. Управление установкой, обновлением, архивами, откатом, службами и журналом остаётся в общем `InstallerAPI`/`NativeEngine`. Копировать этот lifecycle в каждый продукт не требуется.

Состояние этого интерфейса — development. Общий native backend сейчас принимает заранее собранный release. Он не устанавливает `vendor_wheels` во время инсталляции и не доказывает полноту runtime только наличием wheel/lock-файлов. Для готового продукта нужно собрать runtime и зависимости заранее, проверить их лицензии и выполнить install/update/restore/rollback на каждой целевой платформе.

## 1. Что читать при проектировании проекта

Точка входа для человека и помощника — `INSTALLER_API.md`; машинное описание — `api/discovery.json`, доступное также через `hinstall api describe --json`. Проверяются `module_version`, `api_version`, `manifest_schemas`, adapter protocol и фактические capabilities выбранного target. Не следует делать вывод о доступности Windows/Docker по одному наличию target в схеме.

Минимальный контракт подключения:

```python
from homensai_install import __version__
from homensai_install.api.v1 import InstallerAPI

assert __version__ == "0.1.0"  # Версия, которую проверил и закрепил проект.
api = InstallerAPI.from_bootstrap(
    "/private/installer-state",
    trust_config="/trusted/bootstrap/trust-config.json",
)
contract = api.describe()
assert contract["api_version"] == "1.0"
```

Публичный ключ берётся из доверенного bootstrap проекта. Он не принимается из загруженного продукта или HTTP-запроса. Версия и SHA-256 самого installer wheel закрепляются по фактическому артефакту. Указанный номер версии сам по себе не гарантирует наличие этой версии на PyPI: scaffold использует переданный локальный wheel и не обращается к package index.

## 2. Scaffold: сначала реальные входы

CLI:

```text
hinstall init-adapter --output-dir ./product-install --spec ./integration-spec.json --json
```

SDK:

```python
from homensai_install.scaffold import scaffold_product
report = scaffold_product("./product-install", spec)
```

Выходной каталог должен быть новым; существующие файлы проекта не перезаписываются. Ни runtime, ни wheel, ни продуктовые hooks при генерации не исполняются. Загрузка из интернета, установка пакетов и подпись релиза тоже не выполняются.

Без `--spec` создаётся явный черновик:

```text
hinstall init-adapter --output-dir ./product-install-draft --json
```

Результат имеет `status=BLOCKED`, `readiness=DRAFT`, `production_ready=false`; CLI возвращает `2`. Файл `product.manifest.draft.json` имеет `schema_version="DRAFT"` и не проходит production manifest validation. Недостающие хеши остаются `null`. Готовый `product.manifest.json`, payload и подписанный inventory не выдумываются.

### Формат spec

Пример структуры ниже следует заполнить реальными путями и командами своего продукта. Если этих файлов нет, генератор оставит результат заблокированным:

```json
{
  "product_id": "my-product",
  "product_name": "Название продукта",
  "release": "1.0.0",
  "target": {"os": "linux", "arch": "x64", "mode": "native", "scope": "user"},
  "payload_dir": "/build/my-product/linux-x64/payload",
  "runtime_executable": "runtime/python",
  "installer_wheel": "/build/installer/homensai_install_module-0.1.0-py3-none-any.whl",
  "runtime": {
    "python": "3.14",
    "lock_files": {"windows": "locks/windows.lock", "linux": "locks/linux.lock"},
    "vendor_wheels": ["vendor/my_product-1.0.0-py3-none-any.whl"]
  },
  "commands": {
    "web": ["python", "-m", "my_product.web"],
    "migrate": ["python", "-m", "my_product.migrate"],
    "backup": ["python", "-m", "my_product.snapshot"],
    "restore": ["python", "-m", "my_product.restore"],
    "health": ["python", "-m", "my_product.health"]
  },
  "web": {"host": "127.0.0.1", "port": 8820, "healthz": "/healthz"},
  "secrets": []
}
```

`runtime_executable`, lock-файлы, product wheels и `legal/ru.txt`, `legal/en.txt`, `legal/de.txt` должны существовать внутри payload. Существующая shared manifest v2 требует оба lock-файла и Python `3.14`; эти ограничения не скрываются генератором. Для другой модели runtime сначала изменяется и принимается общий manifest contract. Список `secrets` содержит определения имён/контекстов, значения секретов туда не записываются.

Вместо `runtime`/`commands`/`web` можно передать полный `manifest` v2. Генератор пересчитает хеши объявленных lock/wheel-файлов из реально переданного payload. При отсутствии явных `product_id` и `product_name` сохраняются значения исходного manifest. Переданный заранее хеш не считается доказательством совпадения файла.

Scaffold проверяет наличие обычного native runtime по формату ELF/PE, структуру wheel и его metadata, затем считает SHA-256 реальных байтов. Это не проверка исполнимости runtime, ABI, dependency closure или поведения приложения. Они остаются release blockers. Пример не означает, что модуль `my_product` уже установлен в runtime: это обязанность product build.

### Результат при наличии входов

`status=SCAFFOLDED`, `readiness=REVIEW_REQUIRED`, `production_ready=false` означают, что комплект создан для проверки. Выдаются:

- копия реального payload и `product.manifest.json`;
- `payload.inventory.unsigned.json`, связывающий полный набор файлов, manifest, target, release и runtime;
- копия installer wheel и `installer-wheel.pin.txt` с фактическим SHA-256;
- `integration-client.py`, использующий общий SDK через `from_bootstrap`;
- `integration.json`, копии `INSTALLER_API.md`, руководства, discovery, OpenAPI и схем;
- `product-adapter.draft.py`, который явно отвечает `PRODUCT_ADAPTER_NOT_IMPLEMENTED`;
- пустой `trust-config.example.json`, ToDo подключения и `integration-report.json`.

Пин одного installer wheel не является полным lock-файлом его security/dependency окружения. Полнота зависимостей, лицензии и target tests перечислены отдельно в `release_blockers`. Черновик adapter не включается в product payload автоматически и не даёт фиктивного успешного health/migration.

Publisher после review создаёт окончательные `payload.inventory.json`, `payload.signature.json` и доверенный `trust-config.json`. Unsigned inventory имеет `development=true`; production bootstrap его отвергнет. Для отдельного явного development-прогона `prepare_install(..., allow_development=True)` включает соответствующую политику доверенного verifier. Превращать development-артефакт в production простым изменением флага без приёмки нельзя.

## 3. Использование общего плана и мастера

Сгенерированный клиент не содержит собственной реализации установки. `prepare_install` проверяет version pin, загружает доверенный verifier, проверяет полный payload, связывает digest выбранной EULA с подписью и вызывает общий `api.plan`. Эта функция не принимает соглашение и не запускает установку. Создание engine теперь скрыто за публичной фабрикой `from_bootstrap`; отдельная предварительная проверка подписанных условий пока использует `load_trust_config`. Минимальный публичный validate/discover/plan contract не заменяет эту проверку: сам engine повторно проверяет payload перед исполнением, а успешный plan не доказывает его подлинность.

Вызов `submit_reviewed_plan` получает реальные consent refs от вызывающего UI и передаёт их в общий `api.submit` с idempotency key. Нужно показать человеку подписанные условия и весь план до этого вызова. Для пользовательского интерфейса предпочтительно подключать общий web-мастер `hinstall gui`; собственный UI работает с тем же API, а не с копией lifecycle.

После перезапуска UI сохранённые операции находятся через `operations_list`. Продолжение вызывает явный `resume`, а не повторный install с новым idempotency key. Данные и архивы обрабатывают общие engine/adapter ports; продукт описывает только собственные компоненты и бизнес-операции.

## 4. Executable ProductAdapter protocol 1.0

`homensai_install.adapters.ExecutableProductAdapter` реализует существующий `api.model.ProductAdapter`. Доверенный SDK/bootstrap явно регистрирует его через `InstallerAPI.from_bootstrap(..., product_adapter_descriptor="installer/adapter.json")` либо прямой trusted `NativeEngine` для Linux native/user single-step `install`/`update` и `start`, `stop`, `backup`, `restore`, `rollback`, `uninstall`, а также same-release/same-schema `repair` и `reinstall`. Без opt-in используется прежний manifest lifecycle. Браузер и HTTP options не включают регистрацию; автоматической регистрации нет. `InstallerAPI.validate`, `discover` и `plan` не запускают hooks: план читает только подписанный статический descriptor. Для update требуется explicit migration review; для restore/rollback — verified before-recovery snapshot и target hooks. Uninstall сохраняет данные и проверенный source adapter; последующий restore требует исходную квитанцию и ledger, свежий quiesce и полный before_restore. Repair/reinstall используют заново проверенный candidate, свежий quiesce с прежним ledger, полный before-maintenance archive и durable intents замены кода; пользовательские данные и схема не мигрируют. Повреждённый installed adapter не исполняется. Reinstall после completed uninstall сохраняет прежнюю identity и данные. Точные preconditions и границы отказа описаны в `PRODUCT_ADAPTER_EXECUTION.md`.

При прерывании restore/rollback храните исходный public operation ID и вызывайте
явный `resume`. Новый протокол допускает продолжение на границе целого
persistent root: уже заменённые данные повторно не копируются. Исходные журнал,
root receipt, candidate, safety archive и прежний ledger обязательны; source
hooks на частично восстановленной БД не запускаются. Полностью подготовленный
target с потерянным ACK принимается без повторной публикации только по исходным
stage intent и Ports receipt, после повторной проверки metadata, кода и safety
archive. Несовместимый существующий target отклоняется до hooks и замены данных.
Смешанное состояние внутри root и частичный target stage требуют отдельного восстановления. Новая
операция или отмена API claim не снимает native fence. Полный контракт и порядок
отдельного owned-process stop описаны в `INSTALLER_API.md`.

В текущей версии реальный execution backend адаптера поддерживает Linux native/user. Target Docker или service scope возвращает `CAPABILITY_UNSUPPORTED` до запуска; автоматического fallback на host execution нет. Windows execution также возвращает `CAPABILITY_UNSUPPORTED`: нужен отдельно принятый Job Object backend и Windows tests. Это не скрытая попытка выполнить `.cmd` через shell и не обещание уже пройденной Windows-приёмки.

### Подписанный descriptor

Descriptor, например `installer/adapter.json`, находится внутри полного signed inventory вместе с runtime и кодом продукта:

```json
{
  "schema_version": 1,
  "protocol_version": "1.0",
  "product_id": "my-product",
  "adapter_id": "product",
  "adapter_version": "1.0.0",
  "command": ["runtime/python", "-I", "-B", "installer/product_adapter.py"],
  "environment_allowlist": ["HOMENSAI_DATA_HOME", "HOMENSAI_CONFIG_HOME"],
  "capabilities": {
    "describe": {"mutating": false, "idempotency": "read_only"},
    "readiness": {"mutating": false, "idempotency": "read_only"},
    "migration_plan": {"mutating": false, "idempotency": "read_only"},
    "migration_apply": {"mutating": true, "idempotency": "application"},
    "operation_status": {"mutating": false, "idempotency": "read_only"}
  }
}
```

Команда фиксируется подписью. Её executable должен совпадать с `runtime_executable` подписанного inventory, находиться внутри payload и быть настоящим ELF executable на Linux. Внешний `python`, shell/shebang-замена runtime, пользовательский `verified=true` и произвольный callable вместо `SignedPayloadVerifier` не принимаются.

`capabilities()` повторно проверяет подпись/дерево и возвращает статическое описание из descriptor. Даже `describe` subprocess при этом не запускается. Динамический handshake — отдельный явный `invoke("describe", ...)`; продукт обязан вернуть совпадающие protocol version, product ID, adapter version и точный набор capabilities.

### Явный вызов

```python
from homensai_install.adapters import ExecutableProductAdapter

adapter = ExecutableProductAdapter(
    manifest, target, options,
    verifier=verifier,
    descriptor="installer/adapter.json",
    state_dir="/private/product-adapter-state",
    environment={"HOMENSAI_DATA_HOME": "/private/product-data"},
    timeout=30,
)

static_contract = adapter.capabilities()  # Не исполняет продуктовый код.
health = adapter.invoke("readiness", {
    "operation_id": "op_health_check",
    "idempotency_key": "health-20261025-001",
    "phase": "diagnostic",
    "data": {},
})
if not health["result"]["ready"]:
    raise RuntimeError("Продукт ещё не готов")
```

`phase` допускает `execute`, `resume`, `diagnostic`. `plan`, `validate`, `discover`, `preview` отвергаются; mutating hooks не допускаются в `diagnostic`. Продуктовый `migration_plan` — отдельная явно вызванная read-only операция, а не автоматический callback общего планировщика.

`data` — ограниченный JSON-объект, передаваемый через stdin. Его значения никогда не добавляются к argv. Перед любым выполнением повторно проверяются подпись, полный payload и descriptor. Каталог ledger находится отдельно от payload. Payload должен принадлежать текущему пользователю и не быть доступным другим пользователям для записи; ссылки, hardlinks и спецфайлы перед исполнением отвергаются.

### Транспорт и результаты

Один запуск получает один UTF-8 JSON-объект в stdin и возвращает один JSON-объект в stdout. Библиотека добавляет `protocol_version`, `product_id`, уникальный `request_id`, вычисленный `request_digest` и `hook`. Ответ должен точно повторить эти поля и содержать либо `status="ok"` с `result`, либо `status="error"` с `error`. Лишние поля, duplicate JSON keys, неверный ID/digest/version и неоднозначный результат отвергаются.

Границы: до 64 argv-элементов, суммарно 32 KiB аргументов; request/stdout до 256 KiB; stderr до 64 KiB; deadline от значения больше нуля до 60 секунд. Используются `shell=False`, абсолютный путь к подписанному executable, отдельная Linux process group и ограниченное окружение. PATH, HOME, loader-переменные и произвольные секреты родительского процесса не наследуются. Descriptor может объявить только допустимые env-имена; фактические значения передаёт доверенный вызывающий код. Значения окружения не пишутся в журнал и не подставляются в командную строку.

Для Python дополнительно задаются фиксированные `PYTHONNOUSERSITE=1` и `PYTHONDONTWRITEBYTECODE=1`, которые вызывающий код не может переопределить. Рекомендуемый подписанный argv содержит `-I -B` для явного isolated/no-bytecode запуска.

stdout разбирается только как строгий протокол; raw stderr отбрасывается, его содержимое в ошибки не переносится. Переполнение потока и timeout завершают process group. PID сохраняется до cleanup через `waitid(...WNOWAIT)`, чтобы повторное использование PID не адресовало сигнал другому процессу. Успешные результаты проходят whitelist-проверку и проверку redaction; известные переданные env-секреты не могут попасть в сохранённый ответ.

| Hook | Проверяемый result |
| --- | --- |
| `describe` | Точные protocol/product/adapter version и отсортированный список capabilities. |
| `health`, `readiness` | `ready: bool`, `checks: [{id, status, message_code?}]`; `ready=true` требует непустой список только `ok`. |
| `migration_plan` | `from_version`, `to_version`, уникальные steps с `id`, `backup_required`, `reversible`; `plan_digest` равен SHA-256 canonical JSON остальных полей. |
| `migration_apply` | `applied=true`, `receipt_id`, необязательный `state_version`; запрос содержит `plan_digest` и `backup_receipt_id`. |
| `quiesce` | `quiesced: bool`, `active_writers: int`; при `quiesced=true` писателей должно быть 0. |
| `backup` | `artifact_id`, SHA-256 указанного продуктом артефакта. Сам архив проверяет общий engine. |
| `restore`, `post_restore`, `activate` | `applied=true`, `receipt_id`, необязательный `state_version`. |
| `operation_status` | `outcome=applied/not_applied/unknown`, исходный `request_digest`; при applied — исходный валидный result. |

ID receipt и ответ callback не подменяют проверку реального backup/архива. Эту проверку и порядок lifecycle обеспечивает общий engine. `status="ok"` означает корректно завершённый callback, а `ready=false` остаётся отрицательным health-результатом. Библиотека не выводит готовность приложения из одного успешного JSON transport.

## 5. Идемпотентность без скрытого повторения эффекта

Перед запуском callback библиотека сохраняет intent в приватный SQLite ledger с FULL synchronous transaction. Ключ привязан к product/adapter ID; request digest связывает hook, operation ID, data, descriptor и release. Сами data/env-секреты в ledger не сохраняются. Повтор ключа с другим содержимым вызывает `OPERATION_CONFLICT`.

После подтверждённого корректного ответа сохраняется результат. Повтор такого запроса, в том числе после создания нового экземпляра SDK, возвращает `replayed=true` без второго subprocess. Это кэш подтверждённого исхода, а не доказательство атомарности произвольного бизнес-кода.

Timeout, nonzero exit, потеря процесса или невалидный ответ оставляют неизвестный исход. Второй такой вызов не выполняет callback заново. Проект обязан реализовать идемпотентность внутри своей бизнес-транзакции и собственный `operation_status`; объявление `idempotency="application"` без этого capability отвергается.

Продолжение выполняется явно:

```python
outcome = adapter.reconcile("migration-release-1")
```

- `applied` с совпадающим digest и валидным исходным result сохраняется как подтверждённый исход. Следующий `invoke` вернёт кэш.
- `not_applied` разрешает следующую попытку только с `phase="resume"` и тем же исходным request digest. Изменить data под прежним ключом нельзя.
- `unknown` сохраняет блокировку. Повторять изменяющий callback с новым ключом ради обхода блокировки нельзя; сначала требуется реальная проверка состояния продукта.

Reconciliation имеет отдельный durable claim. Одновременная проверка и выполнение одного ключа не должны перезаписывать результат друг друга. Бизнес-адаптер обязан выдавать `not_applied` только по проверяемому журналу/транзакции продукта; простая проверка наличия файла или exit code не доказывает отсутствие эффекта.

Для нового независимого health-check нужен новый idempotency key: повтор старого ключа намеренно возвращает уже зафиксированное наблюдение.

## 6. Проверки и границы доверия

```text
python -m pytest -q tests/test_adapters_protocol.py tests/test_scaffold.py
```

Protocol tests используют реальный subprocess и эфемерную Ed25519-подпись synthetic payload. Проверяются tampering, статические capabilities без exec, handshake, env isolation, bounded streams, timeout, отрицательный health, подтверждённый replay и reconciliation после эффекта с потерянным результатом. Fixture использует подписанную копию системного CPython с системной stdlib; это не тест полностью переносимого embedded runtime продукта.

Scaffold tests проверяют реальные bytes/hash-пины, wheel metadata, schema-invalid DRAFT без входов, отсутствие перезаписи/исполнения, недостающие artifacts, подпись получившегося полного inventory и pin общего SDK. Synthetic wheel и тестовые команды не считаются пройденной приёмкой настоящего приложения.

Подпись подтверждает происхождение и целостность принятого кода, но не делает вредоносный код безопасным. Product adapter исполняется с правами владельца и не является OS sandbox. Системные библиотеки/runtime closure, доверие publisher, проверка лицензий, защищённая сборка и отдельные платформенные E2E остаются обязательными release gates. Автоматической антивирусной гарантии и произвольной загрузки/исполнения GitHub-компонентов здесь нет.

Форматы и свойства используемых механизмов: [Python subprocess](https://docs.python.org/3/library/subprocess.html), [PyPA wheel specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/), [Python os.waitid](https://docs.python.org/3/library/os.html#os.waitid). Документация проверена 25 октября 2026 года; возможности данной реализации подтверждаются указанными тестами отдельно от готовности конечного продукта.


### Backup receipts и запрет неподтверждённой миграции

Standalone `ExecutableProductAdapter` теперь отказывает в новом `migration_apply`
без явного trusted constructor `migration_authorizer(request)`, вернувшего True.
Callback не передаётся через manifest/options/HTTP. Для новой миграции host обязан
проверить реальные backup bytes и reviewed binding через `BackupReceiptStore`;
произвольная строка receipt ID не является разрешением. Исторический SUCCEEDED
replay и read-only UNKNOWN reconciliation не требуют повторной авторизации и
не исполняют миграцию заново. Семантика exactly-once произвольного продукта
не обещается; lost ACK требует явного reconciliation.

При opt-in NativeEngine backup возвращает additive `archive.backup_receipt`.
Приватный store хранит путь; public body содержит digest и binding. Архив реально
расшифровывается и распаковывается в private candidate, проверяются identity,
active pointer и signed recovery payload. Maintenance receipt имеет неизвестную
schema/review, `migration_eligible=false`, `verification_scope=recovery_only`:
он подтверждает recovery, но не разрешает изменение БД. Для будущего update нужен
отдельный reviewed migration plan с наблюдённой schema. Single-step adapter fresh теперь имеет отдельный proof; update и остальные lifecycle
действия по-прежнему unsupported до своей приёмки.


### Single-step fresh

Сначала explicit prepare_migration, затем обычный review/plan и submit с реальными
согласиями. Установка допускает ровно один migration step и отдельный
`fresh_receipt_id` вместо фиктивного backup несуществующих данных. Перед первой
продуктовой записью engine повторно проверяет immutable review, root/owner identity,
пустоту всех persistent roots, signed staged release и точный native intent.
В migration_apply передаётся либо fresh_receipt_id, либо backup_receipt_id;
оба proof одновременно запрещены. UNKNOWN и потерянный ledger запрещают слепой
повтор; явный resume первоначальной операции выполняет read-only reconciliation.
Public SDK factory/resume проверены отдельно от прямых engine вызовов. Полная
installed/clean-host приёмка этого пути сохраняет отдельный статус.
