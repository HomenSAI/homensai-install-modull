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
| `uninstall` | Проверенное удаление управляемого кода с сохранением данных и адаптера для последующего восстановления |
| `repair`, `reinstall` | Замена управляемого кода той же подписанной версии и схемы с полным архивом текущих данных; reinstall также после завершённого удаления |

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
заменяет данные повторно.

Замена roots использует отдельный протокол исходной операции. До первого
изменения private root receipt закрепляет все пять before/target projections,
root identities, candidate, source/target bindings, ciphertext hashes и исходный
quiesce witness. `pending_recovery` в installation owner и оставшиеся receipts
ограждают установку даже при потере mutable journal. Отсутствующие полномочия
не реконструируются новой операцией или legacy factory.

Явный resume допускает целиком исходный или целиком заменённый текущий root;
все завершённые и ещё не начатые roots проверяются до следующей записи. Уже
заменённый root не копируется снова. Корневые inode сохраняются, а missing root
или drift permissions не маскируются `mkdir`/`chmod`. Services сохраняют только
заранее закреплённые pointer, подтверждённый released `operation.lock` и
completed owned update journals; новый файл не принимается по одному имени.
Перед продолжением повторно проверяется тот же полный safety archive.

Source quiesce при таком resume подтверждается прежним witness/ledger вне
заменяемых roots: source hook и `operation_status` на смешанной БД запрещены.
После durable all-roots handoff и завершённого target stage разрешены target
hooks с обычным reconciliation, без нового replacement. Matching terminal
receipt связывает result и завершённые фазы; native `SUCCEEDED` без него
не разрешает ранний успешный ответ или снятие pending marker.

Публикация target также получает исходный private intent до первой замены root.
Он связывает operation/root receipts, подписанный target binding, ожидаемые
metadata, путь и key Ports receipt, а также отсутствие target или его точный
preimage. Существующий несовместимый target отклоняется до migration probe,
quiesce и замены данных. Полностью опубликованный target с потерянным stage ACK
проверяется без изменения файлов: требуются прежний Ports receipt, подписанные
metadata и code, runtime permissions, сохранённые identities. После повторной
проверки safety archive запись ACK завершает stage без root recopy, republish,
metadata rewrite или source hooks. Отдельная оставшаяся stage квитанция только
ограждает незавершённую историю; journal/root receipt не реконструируются.
Проверка исторического terminal proof после успешного завершения не требует
старого live target: последующее штатное update/uninstall может его удалить.

Смешанное состояние внутри root, утрата candidate/receipt/ledger, изменение
root identity и частичный target stage сохраняют `RECOVERY_REQUIRED`.
Старые операции без root receipt или исходного stage intent автоматически не
продолжаются. Это протокол границ целых roots и полностью проверенной target
публикации для Linux/native/user; G3, mid-file recovery, power-loss и
другие платформы остаются открытыми. Public API contract также описывает явный
`cancel` неактивного API claim перед отдельным owned-process `stop` без hooks;
отмена сохраняет native authority и уже выполненные изменения.

Reference adapter проверяет schema, SQLite integrity, обязательный файл и settings.
Он транзакционно записывает recovery effect и `external_actions_on_hold=1`.
`post_restore` требует matching restore той же операции и binding;
`operation_status` возвращает `applied` только при согласованной product receipt.
Результат API содержит проверенный `before_recovery` receipt и отдельный
`before_recovery_archive` с path, SHA-256, release и признаком шифрования.
Внешние действия продукта остаются приостановленными после восстановления.

## Удаление и восстановление после него

`uninstall` использует тот же просмотренный набор выпусков и файлов, что native
протокол удаления. Перед удалением ядро получает подтверждённый source `quiesce`,
останавливает свои процессы и сохраняет полный подписанный комплект активного
выпуска. Квитанция `retained_recovery` дополнительно связывает
`source_adapter_binding` и `quiesce_witness` с исходной operation, installation,
review и существующим authoritative ledger. Наличие каталога с копией само по
себе не разрешает исполнение сохранённого кода.

После завершённого удаления owner marker указывает на конкретные operation и
receipt. Новое восстановление проверяет именно этот журнал, завершённое удаление,
квитанцию, стабильную identity владельца и весь подписанный payload. Выбор по
времени изменения или поиск «последнего» receipt не применяется. Повреждённый
живой выпуск не даёт права молча переключиться на сохранённую копию.

Если удаление прервалось после части файлов, разрешено явное продолжение той же
операции. Для него используются собственные intents и receipt, прежний
invocation key и тот же ledger. Пропавший ledger, UNKNOWN или несовпадающий
результат quiesce останавливают работу; создание пустого журнала не разрешает
повторить эффект. Другие изменяющие операции остаются заблокированными.

Новый `restore` после завершённого удаления получает собственный свежий quiesce.
Прежнее подтверждение остановки не доказывает отсутствие новых писателей. До
эффектов ядро сохраняет проверенный source adapter вне заменяемых persistent
roots и связывает эту копию с текущей операцией восстановления. Она нужна,
например, когда retained source v2 восстанавливает архив v1, а runtime root v2
исчезает при replacement. Планирование только проверяет состояние: hooks,
изменение executable mode и создание ledger на этом этапе запрещены.

Перед replacement создаётся настоящий полный `before_restore` archive текущего
состояния, включая изменения после исходного backup. Подпись recovery payload,
identity, фактическое decrypt/restore и все пять roots проверяются обычным
recovery протоколом. Target schema должна соответствовать архиву; скрытая
миграция при restore не выполняется. Успех требует target hooks, activation,
start и свежую readiness.

Продолжение восстановления использует привязанную к нему копию source и прежние
журналы. Потерянную или повреждённую копию нельзя заменить другим входным payload
под тем же operation ID. После activation старая uninstall квитанция больше не
разрешает новые планы восстановления через retained source; исходный recovery
journal сохраняет собственные правила reconciliation. Данные, настройки,
зашифрованные архивы и незавершённые recovery материалы не удаляются этой
операцией. Для возвращения той же версии также доступен отдельный `reinstall`
с новым проверенным candidate и собственным планом.

## Repair и reinstall той же версии

Эти действия предназначены для замены управляемого кода с сохранением текущих
данных. Под «той же версией» понимаются совпадающие release ID, подписанные
manifest/inventory, runtime, descriptor, capabilities, target и modes.
Другая сборка под прежним release ID не принимается как repair. Смена версии
или схемы выполняется отдельной операцией update либо restore.

`plan` проверяет candidate и закрепляет состояние установленного кода, владельца,
metadata и существующего ledger в `engine_plan.code_maintenance`. В review есть
`review_sha256`, `candidate_binding_sha256`, `release` и `installation_id`.
Отсутствующие или изменённые обычные управляемые файлы могут быть предметом
repair; symlink, special file, чужой owner или недостоверные metadata требуют
отдельного восстановления. Планирование не выполняет hooks, staging, chmod
и не создаёт ledger. Изменение просмотренного состояния требует нового плана.

Выполнение сохраняет отдельно проверенную копию candidate, связанную с исходной
операцией. Старый повреждённый код, runtime и adapter не исполняются. Остановка
использует подтверждённые собственные PID, время рождения и process sessions.
Проверенный candidate должен подтвердить quiesce реального DataHome:
`quiesced=true`, `active_writers=0`. Отдельное read-only наблюдение остановленной
копии данных через `migration_plan` должно показать известную одинаковую схему
`from_version==to_version` и пустой список steps. `migration_apply`,
`restore` и `post_restore` при code maintenance не вызываются.

Новая операция использует новый invocation key в прежнем authoritative ledger
с проверенной файловой identity. Original resume требует исходную invocation.
Отсутствие БД или строки, несовпадающий digest и UNKNOWN не разрешают повторить
эффект. После сохранения ссылки на candidate closure её потерю или повреждение
нельзя исправлять молчаливой подстановкой другого входного payload.

До замены кода создаётся полный зашифрованный `before-repair` или
`before-reinstall` archive всех пяти persistent roots. Он содержит проверенный
same-release recovery payload и действительно расшифровывается и восстанавливается
для проверки. Квитанция имеет `kind=before_code_maintenance_backup`,
`maintenance_action`, `operation_id`, `installation_id`, `from_release`,
`review_sha256`, `candidate_binding_sha256`, `archive_sha256`,
`archive_manifest_sha256`, подтверждения схемы и quiesce. Поля `encrypted` и
`includes_recovery_payload` равны `true`, `migration_eligible` — `false`.
Эта квитанция не разрешает миграцию данных.

Для проверки архива нужны временная копия ciphertext, расшифрованный ZIP и
отдельное дерево восстановленных файлов. Известный отказ проверки свободного
места возвращает `DISK_SPACE_LOW`; повреждение или ошибка подлинности архива —
`INTEGRITY_FAILED`. До успешной повторной проверки приложение не запускается.
После освобождения места требуется явное продолжение исходной операции:
сохраняются её archive, candidate closure и authoritative ledger, подтверждённый
quiesce не повторяется. Новый план не обходит незавершённый журнал.

Непосредственно перед заменой повторно проверяются candidate, просмотренный
набор исходных файлов, safety archive и сохранность текущих данных. Staging и
quarantine принадлежат исходной операции; оба переименования имеют durable
intent с ожидаемой identity каталогов. Для rename требуется одна файловая
система. Продолжение распознаёт только записанные состояния собственного
перехода; чужая подстановка или исчезновение без intent дают отказ.
Старая копия сохраняется в quarantine и не исполняется.

После публикации целевого кода start/readiness используют установленный
проверенный target. Сохраняются installation identity, выбранные modes, secret
references, БД, документы, настройки и история. Reinstall после завершённого
uninstall требует его точную retained receipt, собственный свежий quiesce и
новый явно переданный валидный candidate. Fresh initialization сохранённых
данных запрещена. Marker удаления снимается только после устойчивого возврата
установки в установленное состояние.

Успешный результат содержит `before_maintenance` и
`before_maintenance_archive` с `path`, `sha256`, `release`, `encrypted=true`,
`includes_recovery_payload=true`. Путь имеет вид
`<work_dir>/<operation-token>-before-<action>.hsac`; API сверяет его с планом и
связью receipt, сохраняя рабочий адрес в результате операции. Диагностика и
support export продолжают скрывать приватные значения.

При отказе start/readiness сохраняются текущие данные, архив, candidate и
quarantine. Cleanup имеет отдельный ключ; исходная ошибка и ошибка cleanup
не подменяют друг друга. Повреждённый source не запускается автоматически,
БД не откатывается. Неизвестный исход требует явного reconciliation исходной
операции. Недостоверные owner/metadata, потерянный ledger, чужие pending effects,
несовместимая схема, непригодный existing recovery или невозможность подтвердить
остановку записи остаются явными отказами. Linux DEV проверки этого протокола
не закрывают общую платформенную приёмку repair/reinstall.
