# Подключение ProductAdapter к native maintenance

Автор: Serhii Khomenko. Контрольная точка: `CP-20261025-021`.

Этот этап подключает `ExecutableProductAdapter` к существующему `NativeEngine`
для Linux x64 native/user-session обслуживания уже установленного подписанного
выпуска. Поддержаны `start`, `stop`, `backup`. Это частичный результат HIM-015/023,
а не готовность полного product lifecycle или новой платформы.

## Явная доверенная регистрация

```python
native = NativeEngine(
    verifier=signed_payload_verifier,
    secret_provider=secret_provider,
    product_adapter_descriptor="installer/adapter.json",
)
api = InstallerAPI(state_dir, engine=BackendRouter(native=native))
```

`product_adapter_descriptor` задаёт доверенный bootstrap/SDK в конструкторе.
HTTP options, manifest и браузер не включают эту возможность, не выбирают
команду, environment или исполняемый файл. Нужен настоящий
`SignedPayloadVerifier`; произвольный callback для такой регистрации запрещён.
Без opt-in сохраняется прежний legacy native path.

Descriptor остаётся signed JSON protocol 1.0 с прежними обязательными полями.
Для `start` требуется `readiness`; для `stop` — `quiesce` и `operation_status`;
для `backup` — все три. `environment_allowlist` должен объявлять
`HOMENSAI_DATA_HOME`: движок передаёт только свой DataHome из проверенного
manifest/install context. Это позволяет executable находить продуктовые
квитанции при отдельном `operation_status`; browser environment не принимается.

## Неизменяемый review и исполнение

План читает descriptor/capabilities и проверяет полный подписанный payload;
никакие adapter hooks во время plan/discover не запускаются. В
`engine_plan.product_adapter_binding` сохраняются descriptor path, release,
runtime executable, manifest SHA-256, target, inventory digest и capabilities
digest. Для maintenance используется verified installed active release, а не
новый source bundle с другим executable.

Перед файловыми или процессными эффектами движок заново строит binding и точно
сравнивает его с reviewed plan. Прямой opt-in execute без этого review получает
`PLAN_STALE`. Переключение даже на другой легитимно подписанный descriptor
требует нового плана. Повторная проверка installed bytes выполняется и перед
каждым adapter hook. Сохраняются прежние root lock, общий API journal и
execution lease; второй lifecycle engine не добавлен.

## Readiness — новое наблюдение нужного экземпляра

Сохраняются проверки принадлежащих установщику процессов, владельца loopback
listener и HTTP readiness с product/release/installation identity. Только после
них движок вызывает `readiness` через executable adapter. Каждый вызов имеет
новый `probe_id` и read-only idempotency key; replay прежнего readiness ответа
не принимается.

В protocol 1.0 добавлено необязательное для standalone adapter поле результата:

```json
{
  "ready": true,
  "checks": [{"id": "live-observation", "status": "ok"}],
  "observed_identity": {
    "product_id": "synthetic-reference",
    "release": "v1",
    "installation_id": "synthetic-instance",
    "probe_id": "probe_<fresh-value>"
  }
}
```

Старые standalone results `ready/checks` сохраняют значение. Для включённой
NativeEngine интеграции `observed_identity` обязательно и должно точно
совпадать с ожидаемыми product ID, активным release, installation ID и свежим
probe. Адаптер обязан получить version/instance из состояния самого продукта,
а не просто отразить expected identity. Подпись доверяет этому продуктовому
коду; общий движок не способен доказать правдивость произвольного adapter.
Неверная версия, чужой экземпляр, старый probe, отсутствие identity и not-ready
дают отказ. Новый start operation выполняет новое наблюдение; повтор уже
завершённого operation ID возвращает исторический результат без новых эффектов.

## Quiesce, неопределённый исход и backup

`stop` и `backup` вызывают mutating `quiesce` через прежний durable adapter
ledger, затем прежний owned-process stop. Требуется подтверждение
`quiesced=true`, `active_writers=0`. Nonzero/timeout/protocol failure не доказывают
отсутствия эффекта. Native journal сохраняет `RECOVERY_REQUIRED` и
`product_adapter_unknown=true`; новая операция/новый API state directory не
обходят известную запись в том же install root. Это гарантия новых участников
доверенного протокола, а не произвольных старых binaries.

До quiesce в native journal записывается durable intent; после проверенного
результата — complete marker. Незавершённый intent нельзя обойти новым plan,
даже если controller прервался до создания adapter ledger. Повтор native hook
требует уже существующий ledger и запись именно этого invocation key. Наличие
БД от прежнего readiness не разрешает создать новую quiesce invocation после
потерянного native intent; отсутствие row не считается proof not-applied. `reconcile` никогда не создаёт отсутствующую
БД или её схему; существующие транзакции открывают SQLite через `mode=rw`.
Один живой Ledger instance перепроверяет type/owner/mode/nlink и прежние
`device/inode` до и после транзакций; это не persistent identity после нового
constructor.
Исчезнувшая БД, zero-byte replacement и существующая схема без ожидаемой
invocation не доказывают отсутствие прежнего эффекта и сохраняют отказ.

Обычный повтор execute не повторяет UNKNOWN. Только явный `resume` исходного
плана запускает read-only reconciliation по прежнему request digest:

- `applied` повторно использует проверенную прежнюю quiesce receipt;
- `not_applied` разрешает explicit adapter resume;
- `unknown` сохраняет отказ без повторного эффекта и автоматического rollback.

Результат `applied` также должен подтверждать отсутствие активных writers.
Для `backup` разрешено продолжение исходного UNKNOWN quiesce только через
явный resume с прежним review. После подтверждения quiesce его durable fence
снимается до следующих stop/archive эффектов: последующая ошибка архива не
получает разрешение на retry из прежней quiesce неопределённости. Прежний
консервативный отказ для другого failed backup сохраняется.
После разрешённого stop выполняется существующий полный encrypted filesystem
backup с recovery payload, SHA-256 и реальной архивной проверкой. Синтетическая
строка `backup_receipt_id` не используется вместо архива. При ранее работавшем
продукте `backup` запускает его прежним lifecycle и повторно проверяет readiness.

Если создание архива завершилось ошибкой, а последующий recovery start/readiness
тоже не прошёл, первичное исключение сохраняется. Native journal и публичные
details содержат отдельные безопасные `primary_error_code` и
`recovery_error={phase, code}`. Сырые сообщения и traceback в эти поля не попадают.
Этот regression закрывает конкретное маскирование первичной backup-ошибки;
полная диагностическая приёмка ACC-17 остаётся открытой.

Standalone `invoke` по умолчанию сохраняет первоначальное создание нового
ledger. Сам по себе новый standalone constructor не умеет отличить полностью
удалённую прежнюю историю от действительно нового каталога. Native integration
использует собственный known-intent marker и `existing_ledger=true` для повторов;
это не универсальный forensic detector подмены любого валидного SQLite image.
Полная долговременная привязка файловой identity standalone ledger и доверенный
перенос control journals на новую identity остаются отдельным открытым контрактом.

## Что намеренно остаётся неподдержанным

При enabled integration `install`, `update`, `restore`, `rollback`, `repair`,
`reinstall`, `uninstall` заранее объявлены `supported=false` и отказывают до
эффектов. Они не переходят на legacy migration commands молча.

Связь `migration_plan/apply` с настоящими backup receipts, fresh-empty contract,
версионированная миграция схемы и соответствующее recovery ещё требуют отдельной
реализации. Получение новой signed инсталляции, GUI/CLI/bootstrap фабрика для
opt-in, Windows/Docker/service support и production signing этим этапом не
добавлены. `production_ready=false`, полные HIM/ACC и G0–G5 остаются открыты.

## Проверки

`integration_tests/test_native_product_adapter.py` использует Ed25519 signed
synthetic payload, отдельный executable adapter, настоящие процессы и loopback,
реальную SQLite миграцию в legacy fixture и encrypted backup. Исходная установка
явно выполняется legacy engine; это не приёмка нового adapter install path.
Сценарии проверяют freshness, version/instance/probe mismatch, descriptor
substitution после review, explicit reconciliation всех трёх исходов, отсутствие
слепого повторения и отказ неподдержанных lifecycle действий.

`tests/test_native_product_adapter_contract.py` отдельно проверяет additive
protocol shape и trusted constructor boundary. Existing adapter/native/service
профили повторяются на текущих bytes; точные counts, hashes, commands и ограничения
должны браться из receipt текущего запуска. Это source/synthetic integration,
не installed/frozen/browser/platform acceptance. Авария перед созданием adapter
ledger моделируется fault injection; это не SIGKILL, reboot, relogin или power-loss.
