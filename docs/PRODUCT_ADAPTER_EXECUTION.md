# ProductAdapter: контракт исполнения

`ExecutableProductAdapter` подключает продуктовые операции к `NativeEngine`.
Текущая реализация рассчитана на Linux x64, режим `native`, область `user`.
Перед планированием проверяйте `capabilities(target)`: наличие имени операции
в общем API само по себе не разрешает её выполнение.

| Операция | Контракт зарегистрированного адаптера |
|---|---|
| `install` | Новая установка с одним подготовленным шагом миграции и доказательством пустых каталогов |
| `update` | Один подготовленный шаг миграции с проверенным зашифрованным архивом |
| `start`, `stop` | Управление принадлежащими установщику процессами и свежая проверка состояния продукта |
| `backup` | Остановка записи, полный зашифрованный архив и проверка возможности восстановления |
| `restore`, `rollback` | Восстановление той же установки с отдельной проверенной резервной копией текущего состояния |
| `repair`, `reinstall`, `uninstall` | Не поддерживаются этим адаптерным путём; отказ до продуктовых эффектов |

Это контракт версии для разработки. Он не заявляет поддержку Windows, Docker,
системных служб, восстановления после отключения питания или готовность к
производственному распространению. Операции legacy backend имеют собственные
возможности; включённый адаптер не переходит на legacy-команды молча.

## Доверенная регистрация

```python
native = NativeEngine(
    verifier=signed_payload_verifier,
    secret_provider=secret_provider,
    product_adapter_descriptor="installer/adapter.json",
)
api = InstallerAPI(state_dir, engine=BackendRouter(native=native))
```

Регистрацию выполняет доверенный bootstrap или SDK. HTTP options, manifest и
браузер не выбирают adapter command, environment или executable. Для регистрации
требуется настоящий `SignedPayloadVerifier`. Без регистрации сохраняется прежний
native backend.

Descriptor — подписанный JSON protocol 1.0. `start` требует `readiness`, `stop` —
`quiesce` и `operation_status`, `backup` — все три. `environment_allowlist`
объявляет `HOMENSAI_DATA_HOME`; движок передаёт проверенный путь из собственного
контекста установки. `HOMENSAI_CONFIG_HOME` также передаётся только при наличии
в подписанном allowlist. Environment из браузера не принимается.

## План и повторная проверка

Обычные `plan` и `discover` читают descriptor и capabilities, проверяют
подписанный payload, но не исполняют adapter hooks. В
`engine_plan.product_adapter_binding` сохраняются descriptor path, release,
runtime executable, manifest SHA-256, target, inventory digest и capabilities
digest. Для обслуживания используется проверенный установленный выпуск.

Перед изменениями движок заново строит binding и сравнивает его с принятым
планом. Отсутствующий review или изменённая привязка дают `PLAN_STALE`.
Даже другой корректно подписанный descriptor требует нового плана. Проверка
установленных байтов повторяется перед каждым hook. Root lock, журнал API и
execution lease сохраняют общую блокировку lifecycle.

## Readiness

Сначала движок проверяет принадлежность процессов, владельца loopback listener
и HTTP readiness с product/release/installation identity. Затем executable
adapter получает свежий `probe_id` и read-only idempotency key. Для интеграции
с `NativeEngine` результат должен содержать наблюдённую identity:

```json
{
  "ready": true,
  "checks": [{"id": "live-observation", "status": "ok"}],
  "observed_identity": {
    "product_id": "example-product",
    "release": "v1",
    "installation_id": "example-instance",
    "probe_id": "probe_<fresh-value>"
  }
}
```

Адаптер получает version и instance из состояния продукта. Простое отражение
ожидаемых значений не является проверкой. Движок сверяет product, active release,
installation и свежий probe; чужой экземпляр, старая версия или прежний ответ
отвергаются. Подпись задаёт доверие продуктовому коду, но не доказывает правдивость
произвольной реализации адаптера. Новый `start` выполняет новое наблюдение;
повтор завершённого operation ID возвращает сохранённый результат.

## Quiesce и неопределённый исход

`quiesce` обязан подтвердить `quiesced=true` и `active_writers=0`. Затем движок
останавливает только принадлежащие ему процессы. Timeout, nonzero exit или
неверный ответ не доказывают отсутствия эффекта: сохраняются
`RECOVERY_REQUIRED` и `product_adapter_unknown=true`.

До изменения записывается durable intent, после проверенного результата — marker
завершения. Новый план или другой API state directory не обходят незавершённый
intent в той же установке. Повтор hook требует существующий authoritative ledger
и прежний invocation key. Наличие другой записи в БД не разрешает создать
потерянный invocation заново.

Только явный `resume` исходной операции выполняет read-only reconciliation с
прежним request digest:

| Результат `operation_status` | Разрешённое продолжение |
|---|---|
| `applied` | Использовать проверенный прежний результат без повтора эффекта |
| `not_applied` | Разрешить explicit resume исходного invocation |
| `unknown` | Сохранить отказ без повторного эффекта и автоматического отката |

`reconcile` не создаёт отсутствующую БД или схему; существующая SQLite открывается
через `mode=rw`. Исчезнувший ledger, пустой заменяющий файл и отсутствие нужной
записи сохраняют отказ. Живой экземпляр ledger проверяет type, owner, mode, nlink
и device/inode до и после транзакций. Эта проверка не является универсальным
детектором замены валидной БД между отдельными запусками. Доверенный перенос
control journals на другую файловую identity автоматически не выполняется.

## Резервное копирование и разрешение миграции

После подтверждённого quiesce и остановки создаётся полный зашифрованный архив
с recovery payload. `BackupReceiptStore` хранит private archive reference и
public digest body. Перед выдачей или проверкой receipt архив фактически
расшифровывается и восстанавливается в отдельный кандидатный каталог.
Движок проверяет installation marker, manifest/inventory digests, подпись
recovery payload и active pointer. Чтение использует ограничение размера,
проверки fd identity, owner/mode/nlink и `O_NOFOLLOW`.

Обычный maintenance backup возвращает `archive.backup_receipt` с
`migration_eligible=false` и `verification_scope=recovery_only`: при нём схема
и migration review не устанавливались. Строка receipt ID не заменяет доказательство
пригодности архива для конкретной миграции. Согласованность ограничена остановкой
собственных процессов и подтверждением quiesce продуктом, а не контролем любых
внешних writers.

Standalone `migration_apply` требует доверенный constructor callback
`migration_authorizer(request)`. Без него новый эффект запрещён с
`CAPABILITY_UNSUPPORTED`. Callback нельзя передать через HTTP или options.
Он проверяет архив и reviewed binding перед новым mutating intent либо explicit
`not_applied` resume. Повтор завершённой операции и reconciliation читают прежние
доказательства и не запускают миграцию заново.

Если продукт работал до backup, после архива он запускается и снова проходит
readiness. При ошибке архива и дополнительной ошибке recovery start первичная
ошибка сохраняется; публичный результат содержит отдельные безопасные коды,
без сырых сообщений и traceback.

## Подготовка миграции

`InstallerAPI.prepare_migration("install" | "update", manifest, target, options)`
вызывает подписанный read-only `migration_plan` отдельно от общего `plan`.
Подготовка связывает installation, source metadata и schema с target release,
подписанным адаптером и product plan `from_version/to_version/steps/digest`.
Для новой установки требуется `from_version="empty"` и проверяются пустые roots.

`migration_review_id` — digest неизменяемого private review. Обычный план читает
его без hooks; исполнение повторно проверяет binding и фактический product plan.
Изменившиеся schema, metadata, roots, owner или review останавливают операцию.
Для отсутствующей установки preparation создаёт private ownership reservation
с `installed=false`, пустые каталоги и control metadata без фиктивного
`current_release`. Подготовка миграции доступна через SDK; отдельного CLI/HTTP
маршрута для неё пока нет.

Reference `migration_plan` читает SQLite в DELETE mode, не создавая БД, WAL или
SHM. Неизвестная, повреждённая и более новая schema отвергаются. Целевая схема
берётся из подписанного product profile.

## Новая установка

Требуется ровно один подготовленный migration step. До первого product write
движок перепроверяет inode/owner/mode пустых roots, installation metadata,
immutable review, durable intent и подписанные целевые файлы.

`fresh_receipt_id` доказывает пустое начальное состояние и не заменяет backup
существующих данных. Одновременные fresh proof и backup proof запрещены.
Подтверждённый отказ authorizer до mutating intent допускает explicit resume;
потерянный ответ после возможного эффекта требует reconciliation. SDK сохраняет
исходный operation ID.

## Обновление

Update review schema 2 связывает source adapter отдельно от target:
descriptor, runtime, manifest, inventory и capabilities. Старый update review
schema 1 отвергается с `PLAN_STALE`; fresh review schema 1 сохраняется.

Перед intent повторно проверяются review и read-only schema hook. Для `archive`
и `backup:0` сохраняются реальные `BackupReceiptStore` refs с фактической
`product_plan.from_version`. Все live persistent roots сравниваются с реально
восстановленным snapshot по содержимому и каталогам. Единственное исключение —
содержимое собственного `services/update-<native-token>.json`; его type,
ownership и безопасность всё равно проверяются.

Quiesce intent связан с native token, фазой UpdatePlan и release. Source stop
и каждая candidate cleanup имеют отдельные started/complete/result записи.
Неопределённый исход одной фазы не снимается успехом другой. Migration hook
получает только `plan_digest`, `backup_receipt_id`, `migration_binding`.

В план входит политика `preserve_forward_journal_no_automatic_restore`.
После изменения resume сохраняет исходный журнал и не требует прежней schema;
известный эффект не повторяется. Ошибка health останавливает только собственный
candidate и сохраняет новые данные и архивы. Первичная ошибка и cleanup имеют
раздельные коды; UNKNOWN cleanup разрешается reconciliation до нового start.
Автоматический разрушительный откат не выполняется.

## Восстановление и откат

`restore` и `rollback` работают с архивом той же установки. Используются source
`quiesce` и target `migration_plan`, `restore`, `post_restore`, `readiness`,
`quiesce`, `operation_status`. Общий план закрепляет hook contract. Проверка
извлечённых кандидатных roots выполняется только при execution, до stop и
replacement. Нужна известная схема, у которой `from_version == to_version`
и отсутствуют migration steps.

Архив внутри заменяемых roots, чужая installation, несовместимые platform/mode,
изменённый pointer locator и несовместимая schema отвергаются заранее.

`RecoveryBackupReceiptStore` создаёт отдельный полный зашифрованный snapshot
текущего состояния и связывает его с action, operation, installation,
source archive SHA-256 и исходным release metadata. Непосредственно перед
replacement снова проверяются ciphertext, фактический decrypt/restore,
подпись recovery payload и все persistent roots. Исключения для update journal
здесь нет. Такой recovery receipt никогда не разрешает миграцию.

Target hooks получают ровно `installation_id`, `release`, `recovery_action`,
`source_archive_sha256`, `before_recovery_receipt_id`. Доверенный
`recovery_authorizer` по умолчанию отсутствует; standalone mutating recovery
запрещён. Движок разрешает hook после завершённого replacement и повторной
проверки safety receipt. Код выполняется из staged installed signed release.

Для каждого эффекта сохраняются durable intent, binding и результат. UNKNOWN
требует reconciliation исходной операции. Diagnostic probe и resume не создают
отсутствующий authoritative ledger. После подтверждённого hook resume не
заменяет данные повторно. Прерывание внутри replacement сохраняет durable fence
и safety archive: автоматический повтор этой фазы запрещён, требуется отдельное
восстановление из сохранённых данных.

Reference adapter проверяет schema, SQLite integrity, обязательный файл и settings.
Он транзакционно записывает recovery effect и `external_actions_on_hold=1`.
`post_restore` требует matching restore той же операции и binding;
`operation_status` возвращает `applied` только при согласованной product receipt.
Результат API содержит проверенный `before_recovery` receipt и отдельный
`before_recovery_archive` с path, SHA-256, release и признаком шифрования.
Внешние действия продукта остаются приостановленными после восстановления.
