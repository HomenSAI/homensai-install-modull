# Текущий пакет CP-20261026-010

Интегрирован Linux-only helper безопасного сравнения всех пяти live persistent roots со restored backup: bytes+dirs, fd-relative O_NOFOLLOW, links/hardlinks/reparse/гонки fail-closed; исключаются только bytes точного own update journal. Agent149PASS/1Windows-onlyskip (59 новых helper cases), root59PASS/2SKIP; два root fault tests ждут update capability, это НЕ PASS lifecycle. Installed update-readiness повторён на immutable SDKCP009/e4f8dbc:69SDK/7reference/161dependency files unchanged, fresh/preparation и три отказа update PASS. Полный update execution ещё в отдельном core worktree, публичная capability основной ветки false. По запросу владельца создана private веб-страница статуса; публикация Sites succeeded. Production_ready=false, все G0–G5 OPEN; baseline2497PASS относится к CP008/f90a20a, новый полный suite ещё NOT RUN.

Следующий шаг: интегрировать проверенный core update execution с phase-scoped quiesce/real receipts, проверить оба независимых root fault tests и installed1→2→3. При недостаточной проверке capability оставитьfalse. Deadline16:00UTC/17:00Berlin; затем handoff/GitHub/стоп.

Исторические записи ниже:

# Текущий запуск до 17:00 Berlin: CP-20261026-009

Возобновлена работа по новому указанию владельца до 17:00 Berlin (16:00UTC), старое окно архивировано. Полный baseline CP008/f90a20a:2497PASS/3skip/15subtests,542.76s,431inputs unchanged. Добавлены private update review schema2/from_adapter_binding и source adapter/failure policy в read-only engine plan; fresh schema1 и public7fields сохранены. Исходный RED4FAIL/2PASS сохранён; core112PASS/1XFAIL и отдельно64PASS, root11PASS/1XFAIL (наборы пересекаются). Installed update-readiness на SDKf90a20a: PASS, 69SDK/7reference/161dependency files unchanged; public fresh и readonly update preparation, отказы v2/reviewed-v2/v3 до mutation, effectcount1. Update execution/capability пока выключены, полный1→2→3 XFAIL/NOT RUN. Следующий пакет: scoped quiesce, реальный backup/receipt, live snapshot comparison и fail-closed forward journal без automatic legacy restore. Production_ready=false, G0–G5 OPEN.

Реализовать adapter update execution с отдельными source/target bindings и phase-scoped quiesce; Ports.backup выдаёт настоящий encrypted BackupReceiptStore receipt, authorizer сравнивает live roots со snapshot. UpdatePlan сохраняет forward journal без automatic legacy restore. Включать capability только после1→2→3/faults; после15:42UTC freeze на финальный suite, в16:00UTC handoff/GitHub/стоп.

Старый запуск08:25–20:25UTC сохранён в execution_history; актуальное окно14:35:31–16:00UTC. Не продлевать deadline.

## Исторический handoff CP008

# Продолжение после сохранения перед выключением

Владелец 26.10.2026 явно попросил закончить текущий этап, сохранить GitHub и
оставить продолжение другому ИИ. Новые задачи остановлены. Компьютер выключает
сам владелец; исторические shutdown-команды не выполнять.

Этот документ — технический handoff, а не второй список задач. Канонический
реестр: `HomenSAI_Universal_Installer_TODO_RU.txt`. Актуальные checkpoint/проверки —
`RESUME.md`, `work-state.json`, `verification-status.json` и receipts.

## Восстановление на другом компьютере

1. Получить ветку `codex/universal-installer-20261025` из
   `https://github.com/HomenSAI/homensai-install-modull.git` по START_HERE_RU.md.
   Сверить HEAD с GitHub ref, проверить dirty/index и активные процессы.
2. Прочитать AGENTS.md, AI_START_HERE.md, RESUME/work-state, этот handoff,
   канонические PROMPT/TODO и verification backlog. Не возвращаться к CP021
   и не восстанавливать старый stash: его рабочая папка уже удалена.
3. Работа остановлена владельцем. Продолжать только по новой инструкции владельца.
   Предыдущее окно UTC08:25:06–20:25:06 (Berlin09:25–21:25CET) завершено досрочно;
   не выдавать время выключенного компьютера за работу. Для нового полного
   запуска сохранить историю старого окна и записать новые UTC start/deadline.
4. Поднять DEV Docker по BUILDING/START_HERE. Три агента всего, SOL6.1/high:
   основной, core_lifecycle, platform_acceptance. Два worktree — только внутри
   `.local-state/agents/`; не создавать соседние папки проекта.

## Что уже находится в основной ветке

- Реальные encrypted backup receipts: повторная расшифровка/restore в private
  candidate, проверка installation identity, pointer, signed recovery payload.
  Maintenance receipt имеет `migration_eligible=false`, это recovery-only.
- Явная `InstallerAPI.prepare_migration(action, manifest, target, options)`:
  signed read-only hook вызывается отдельно от plan. Immutable review связан с
  source/target, schema, ownership и fresh filesystem witness. Неизвестные либо
  неразрешённые native journals не допускают новый review.
- Trusted factory: `InstallerAPI.from_bootstrap(..., product_adapter_descriptor=
  "installer/adapter.json")`. Это Python keyword, не manifest/HTTP option.
  Без signed verifier opt-in отклоняется; default не включает адаптер.
- Single-step fresh install Linux/native/user: explicit prepare → plan/review →
  submit, настоящий fresh-empty proof перед первой product write. Signed stage,
  exact native intent, root identity и пустые persistent roots перепроверяются.
  `migration_apply` принимает ровно один `fresh_receipt_id` или `backup_receipt_id`.
- Public original-operation resume проверен при lost ACK и известном отказе
  authorizer до effect. UNKNOWN/missing ledger не разрешает повтор миграции.
- Reference SQLite adapter: read-only schema plan и атомарный COMMIT business
  schema + product result receipt. Неполные defaults после COMMIT дают UNKNOWN.
  Наличие приложения и DB schema не подменяет подтверждение завершённого effect.

Поддержка текущего adapter engine: `install` (один step), `start`, `stop`, `backup`.
**Update/restore/rollback/repair/reinstall/uninstall пока выключены.**
Все G0–G5 открыты, `production_ready=false`.

Проверки имеют разные scopes: CP004 full source/integration — 2416 PASS/3skip/
15subtests; fresh core — 171 PASS/5subtests; root final targeted — 84 PASS.
Эти числа пересекаются и не складываются. Полный suite после последнего fresh
изменения ещё нужно повторить. Финальный installed fresh — PASS на точном SDK
`a5130d19343bfa4475147d8af34f653f7c30f7f2`: 69 SDK/7 reference/161 dependency
files неизменны, реальный engine fresh proof, один apply, start/stop/backup+restore
проверены. Receipt: `packaging/acceptance-evidence/installed-reference-fresh-20261026-agent01.json`.
Runtime заимствует stdlib/libpython DEV image, это не clean-host/standalone proof.

## Первый следующий инженерный пакет: single-step update

HIM-015/023/050/054/056/063. Начать с нового
`integration_tests/test_native_adapter_update.py` — файл ещё не создавался.
Использовать `migration`/`fresh_plan` из test_native_migration.py: настоящий
adapter fresh release-1/schema_1 → signed release-2/schema_2 → release-3/schema_3.
Первый ожидаемый RED — `CAPABILITY_UNSUPPORTED` для update. Сначала сохранить RED,
затем реализовывать следующий контракт; не включать capability заранее.

1. Сохранить public migration_binding из семи полей. Apply data строго:
   `{plan_digest, backup_receipt_id, migration_binding}`. Пути и archive contents
   в продуктовый request не добавлять.
2. В engine_plan добавить `product_adapter_source_binding` и
   `update_failure_policy="preserve_forward_journal_no_automatic_restore"`.
   Private update review — schema_version2 с `binding.from_adapter_binding`.
   Старые update reviews schema1 отвергать PLAN_STALE; fresh schema1 сохранить.
3. Source adapter/manifest/runtime/inventory закрепить отдельно от target.
   `_installed_product_adapter` выбирает source/target binding по pointer/release/
   фазе. Recheck schema/review — под root lock до native intent/stop. Resume
   продолжает первоначальный journal, не утверждая старую schema после mutation.
4. Quiesce intent key — digest(native token, UpdatePlan phase key, release).
   Хранить отдельные started/complete/result записи; source completion не даёт
   разрешение на stop/cleanup candidate и не снимает чужой UNKNOWN fence.
5. Использовать существующий UpdatePlan с единственным reviewed migration_id.
   Ports.backup поверх реального super().backup выдаёт BackupReceiptStore receipt
   и сохраняет ref до следующей фазы. Binding включает source identity/releases,
   manifest/inventory, authoritative from_version, operation_id и digest review.
   В текущем `_backup` `migration_review` — вложенный record: source schema нужно
   брать из `review["product_plan"]["from_version"]`, не из верхнего get().
6. До effect повторить ciphertext/decrypt/restore/signed recovery/identity checks
   и сравнить live persistent roots с snapshot. Исключать только точно известный
   свой services/update-<token>.json, меняющийся законно. Read-only schema hook
   вызвать вне `_Ledger.begin`: внутри держится write transaction того же ledger,
   повторный invoke может заблокироваться. Callback проверяет hashes без hooks.
7. Первый update slice — success и fail-closed forward journal, без автоматического
   legacy restore/rollback. Ordinary failure не должен попасть в UpdatePlan
   catch(Exception), который восстановит данные без нового backup protocol.
   UNKNOWN уже BaseException; не повторять и не откатывать его автоматически.
   Health failure останавливает только owned candidate, сохраняет данные/архивы
   и отдельные безопасные primary/cleanup codes. Restore/rollback caps оставить false.
8. Проверить 1→2→3 с сохранными данными; foreign/missing/tampered archive до effects;
   live data/schema drift; source/target quiesce; SIGKILL до/после backup, ledger,
   effect и pointer; lost ACK/missing ledger; ровно один apply; health failure без
   молчаливой подмены текущих данных старым snapshot.

Далее — installed versioned reference lifecycle на том же immutable SDK и
полный source/integration candidate. Для настоящего restore/rollback нужен
отдельный before-rollback backup и доказанный recovery; не считать legacy tests
приёмкой нового adapter. Windows11/Ubuntu24.04 clean host, Docker/service,
ru/en/de GUI и JobPilot/Rent остаются отдельными обязательными пакетами.

## Артефакты, Git и уборка

Evidence лежит в packaging/acceptance-evidence. Raw attempts/wheels во время
этого запуска экспортировались в `.local-state`, не в ephemeral container.
На новом ПК wheels можно воспроизвести по pinned inputs; приватные ключи,
пароли, .venv и личные базы в Git не включаются.

После каждого проверенного пакета обновлять HIM/ACC и handoff, сохранять
allowlist checkpoint, обычный feature push и независимо проверять ref/tree.
Не использовать force push. GitHub agent refs сохранены:
`codex/core-lifecycle-20261026-run01` и `codex/platform-acceptance-20261026-run01`;
основная ветка содержит интегрированные принятые части. Перед cherry-pick сначала
проверить историю: повторно применять уже интегрированный пакет нельзя.

Старый cp021 worktree удалён. Один ненужный temporary work каталог внутри
acceptance/.local-state/attempts/maintenance-01 оставлен: автоматическая проверка
отклонила удаление с `blocked by policy`. Не повторять обходными командами.
Это не blocker разработки. Новые сторонние модули разрешены только после
проверки точной лицензии и условий распространения по THIRD_PARTY_POLICY_RU.md.
