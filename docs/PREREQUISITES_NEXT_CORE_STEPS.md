# Следующие core-шаги prerequisites

Автор: Serhii Khomenko. Первоначальный разбор CP-20261025-016 по исходникам CP-015; статус обновлён в CP-017.

**Общий lease реализован в CP017.** Актуальный контракт — [EXECUTION_LEASE.md](EXECUTION_LEASE.md),
результаты — `packaging/acceptance-evidence/execution-lease-20261025-cp017.json`.
Full source/integration: 2097 PASS, 1 Windows-only skip, 15 subtests; installed synthetic SDK/CLI PASS.
Ни один scoped PASS не закрывает реальные OS/browser/product gates. Неизвестные
незарегистрированные старые journals и binaries вне протокола не покрываются;
автоматический перенос control journal на новую файловую identity NOT IMPLEMENTED.

**Durable byte progress в CP017 ещё NOT IMPLEMENTED.** Ниже сохранён исходный
проект требований; описания кода в историческом разделе 1 относятся к CP015.

Installed synthetic SDK/CLI receipt CP-016 не закрывает эти требования.
Задачи остаются в каноническом ToDo: HIM-020/HIM-063 и HIM-035. После каждого
отдельного реализованного шага: тесты, evidence, TODO/RESUME, commit и GitHub
с независимым подтверждением ref/tree.

## 1. Общий execution lease — исторический проект до CP017

### Подтверждённое чтением кода

`api/v1.py.submit/resume` публикуют product operations в `api/journal.py`.
Product claims связаны с install root/scope внутри product SQLite, а разные
API state directories имеют разные журналы. Native/Docker engine locks
защищают установки, но не совпадают с prerequisite controller lock.
`prerequisite_state.py` имеет собственные SQLite claim и controller lock;
`prerequisite_async.py` — локальные worker reservations.
`gui.py._product_prerequisite_guard/_reserve_prerequisite_change` проверяют
занятость, но не передают общее владение до effect. Между проверкой и enqueue
остаётся промежуток. Read-only анализ не является выполненным race test.

### Предлагаемая граница

Добавить внутренний `ExecutionLease` для участников одного **доверенно
настроенного execution domain**. Domain должен быть стабильным для original
owner и передаваться trusted bootstrap/coordinator в зарегистрированный SDK.
Он не выбирается через HTTP, manifest/options или другой product state_dir.
Новый catalog digest, release или переключение native/docker не создают новый
domain, позволяющий обойти старый unresolved claim.

Постоянный OS-lock inode отвечает за активное владение контроллера. Durable
claim с fencing token отвечает за незавершённую операцию после смерти процесса.
PID/TTL и отсутствие процесса не доказывают исчезновение внешнего installer
или его эффекта. Приватные token/path/чужой operation ID не выдаются в busy().
Read-only SDK construction/capabilities сохраняют ленивую семантику без записи.

Lease нужен **до durable enqueue**, затем тот же handle передаётся worker без
unlock/reacquire промежутка. Удержание охватывает effects, terminal commit,
final observers/diagnostics и cleanup. External installer не наследует handle.
Существующие engine locks сохраняются. Единый порядок: common lease, короткая
регистрация worker, локальный controller/engine lock, короткая SQLite transaction.
Discovery и installer не исполняются внутри открытой SQLite transaction.

Две прежние SQLite и новый lease store не образуют одной транзакции. Нужен
явный durable протокол RESERVED → QUEUED → ACTIVE и fail-closed recovery:

| Наблюдение после обрыва | Допустимое продолжение |
|---|---|
| RESERVED, operation ещё не опубликована | Точный явный повтор восстанавливает тот же ID; effects до QUEUED запрещены инвариантом. |
| QUEUED, worker отсутствует | Только явно разрешённое восстановление; constructor/status не запускают работу. |
| ACTIVE, исход неизвестен | Claim сохраняется, требуется существующий reconciliation. |
| Journal terminal, claim ещё есть | Проверить безопасное завершение; отсутствие процесса не освобождает claim само. |

Product idempotency key должен связываться с RESERVED до journal publication.
Duplicate start остаётся чтением той же операции, не implicit resume. Release
проверяет актуальный fencing token, чтобы stale worker не снял новый claim.

### Cancel и совместимость

`tests/test_public_api.py::test_cancel_inactive_recovery_releases_api_claim_without_executing_engine`
разрешает новый product plan после CANCELED + resume_required. Это существующий
API claim, а не доказательство quiescence для нового общего lease. Отмена
неизвестного эффекта не должна освобождать общий domain. Cancel сохраняет
cooperative flag и не ждёт длительный execution lock.

Политика освобождения должна отличать отмену до первого effect и подтверждённое
завершение от manual/reboot/relogin/recovery/unknown effect. Для generic engine
нужен доверенный контракт quiescence или консервативное удержание claim.
Отдельно проверить synchronous `background=False`: внешний `_JOBS_LOCK` вокруг
`_launch/_run` может блокировать same-process status/cancel. Это вывод по call
path; отдельный воспроизводимый regression ещё не запускался.

### Обязательные независимые process tests

Проверить обе очередности product/prerequisite start с одним domain и разными
API state directories; окно enqueue → worker; terminal journal → final observer;
lost ACK/idempotency; crash после RESERVED/QUEUED/effect; installer child,
переживший controller; cancel и stale release; storage/fsync failure;
symlink/hardlink/owner refusal; неизменность прежних v1 plan digests/consents.
Использовать отдельные interpreter processes или spawn, собственные temp dirs
и synthetic effects. Старые бинарники и прямые engine calls вне протокола
не получают его гарантий. Windows ACL/UAC/live installers остаются отдельной
непринятой областью, даже после Linux synthetic PASS.

## 2. Durable measured download progress

### Подтверждённое чтением кода

`downloads.py` уже создаёт measured started/bytes/cache_hit/retry/verified,
но `_notify()` игнорирует callback Exception. Последние bytes приходят до
SHA-256 verification; полный восстановленный `.part` и local file имеют
отдельные пути. `prerequisite_execution.py._progress()` передаёт transient
observer и теряет исходный event type/artifact identity.
В `prerequisite_state.py.save()` event sequence равен operation revision;
из параллельного состояния переносится только sticky cancellation. Простая
запись snapshot из callback может потеряться при следующем stale save.

### Предлагаемое additive изменение

Сохранить прежний best-effort observer и добавить отдельный trusted обязательный
writer, например `persist_progress`. Ошибка durable записи должна дойти до
coordinator как storage failure и остановить выполнение до adapter.execute;
она не становится transport retry или пользовательской отменой.

Узкий `store.record_progress` в одной SQLite transaction читает свежую operation,
проверяет plan/component/artifact/transfer binding, обновляет только текущий
download snapshot/revision/time и вставляет event той же sequence. Он не меняет
readiness, status, consents или installer receipt. Последующий stale save
сохраняет свежую progress-проекцию. Replan очищает текущую проекцию, сохраняя history.

Достаточен один bounded snapshot для последовательного executor: plan digest,
component/artifact/hash, transfer ID, network attempt, state, completed/total,
source, resumed, verified и percent. Total берётся из подписанного размера;
счётчики проверяются как bounded integers, bool не принимается. URL/query,
ETag, local paths и raw exceptions в stream не попадают. Старый transfer не
перезаписывает новый, а retries не суммируют completed разных попыток.

Bytes == total ещё не означает verified: до проверки hash/staged bytes показать
verifying и percent=null. 100% разрешается только для проверенного артефакта;
это не dependency READY и не product success. Cache/full-part/local должны
иметь явное подтверждённое событие, corrupt cache/offline miss — не иметь.

Durable progress сохраняет **наблюдение**, а не гарантированно fsynced bytes:
сейчас callback следует за write, а fsync выполняется позже. После restart
реальный offset определяется заново по файлу/протоколу, никогда по snapshot.
Нужен явный предел частоты/числа byte checkpoints; каждый chunk не должен
создавать отдельную транзакцию. State/retry/verified/failure/cancel записываются
обязательно. Event cursor двигается по полученным events, не по более новой
snapshot.revision, иначе можно пропустить страницу.

### Обязательные проверки

Live committed bytes видны новой SDK instance во время заблокированного
synthetic transfer; reconnect cursor непрерывен. Cancel и stale save не теряют
друг друга. Storage failure откатывает snapshot/event вместе и не допускает
installer. Полный неверный hash не даёт verified/100. Проверяются cache/full-part/
local/offline, Range/ETag/reset/retry, несколько artifacts, stale callbacks,
bounds/redaction и прежний queued v1 plan. Затем расширяется installed synthetic
harness и отдельно GUI byte display/Node/actual browser acceptance.

Ни один из перечисленных progress tests не включён в PASS CP-015/016/017.
Lease regressions приняты в ограниченной области CP017 по отдельной квитанции.
