# Продолжение разработки

## Активное окно Terra 28.10.2026 — HIM-054

CP-20261028T143449Z-8878f171e2eb: отдельный installed lost-ACK профиль — WIP,
AST PASS, реальный прогон ещё NOT RUN. SDK source `6edcbb83`; свежий baseline
installed happy-path PASS в DEV Docker, не clean-host. Runtime не изменён;
подробности и следующая проверка в `docs/TERRA61_2H_PROGRESS_RU.md`.

Дополнение CP-20261028T143633Z-a8c51a2df8c6: первый installed lost-ACK
профиль PASS на SDK `6edcbb83`, wheel `c7c0e21e38559fd9aa28032205e5d61aed22a31434b92f84e94812f94dccfb96`.
Receipt `packaging/acceptance-evidence/installed-update-lost-ack-terra-20261028.json`.
Тестовая подмена ответа после реального signed apply, не SIGKILL/clean-host.
Остальные fault сценарии и production gates остаются открытыми.
CP-20261028T143832Z-262d480572a6: второй профиль tampered-backup подготовлен,
AST PASS, прогон ещё NOT RUN; выполнить isolated Docker test.
Дополнение CP-20261028T143954Z-972d8c3a1cf6: tampered-backup installed
ожидаемый отказ PASS, schema v1/effect count 1 и данные сохранены. Receipt
`packaging/acceptance-evidence/installed-update-tampered-backup-terra-20261028.json`;
Linux DEV, не clean-host и не полная приёмка HIM-054.
CP-20261028T144201Z-4d26028d6ee8: lost-ACK повторён после добавления второго
профиля — PASS на итоговом harness; focused source regression ещё RUNNING.

## Финальный handoff CP-20261026-012

Работа завершена по указанию владельца в окне до17:00Berlin/16:00UTC. Single-step adapter update Linux/native/user реализован и принят в DEV scope. Полный frozen source/integration: 2625PASS/3skip/15subtests, 1181.13s,208inputs unchanged. Installed public factory→fresh1→update2→update3 на immutable SDK6edcbb83 PASS:70SDK/7reference/161dependency files unchanged; effects1/2/3, source/target runtime+health identity, данные/settings/syntheticcredential сохранны. Все4update encrypted archives действительно decrypt/restored в oracle. Root2datafaultPASS/25contractPASS; core180PASS и отдельно223PASS/1root-exportdeselect (пересекаются с full, не складывать). CI SDKCP011:Linux/Windows source jobs success. Forward journal не делает automatic legacy restore; UNKNOWN/missingledger не разрешает повтор. Public restore/rollback/repair/reinstall/uninstall adapter capabilities остаютсяfalse. BorrowedDEVruntime, это не clean-host/standalone acceptance. Production_ready=false, G0–G5 OPEN. Веб-страница текущей работы опубликована privately; финальная публикация её результатов выполняется после этого checkpoint и хранится внешнейreceipt.

Следующий шаг: На immutable SDKCP011 добавить independent installed update fault profile: lostACK/UNKNOWN/missingledger, tampered/foreign backup, live data drift и candidate health/cleanup failure через original public SDK operation. Затем реализовать отдельный verified before-restore/before-rollback backup protocol и adapter restore/rollback; capabilities не включать заранее. Windows11/Ubuntu24cleanhost, productDocker/services, GUI12locale-target сочетаний, JobPilot/Rent и production trust/licences остаются отдельными обязательными gates. Новое окно — только по явной команде владельца.

Receipts: source-20261026-cp012.json; installed-update-forward-20261026-cp011.json. Текущий sync pending до внешней checkpoint receipt; собственного будущего SHA здесь нет.

## История кандидата CP-20261026-011

Интегрирован core runtime freeze091c0f4: single-step Linux/native/user adapter update с отдельными source/target bindings, phase-scoped quiesce, реальными encrypted backup receipts archive+backup:0 и live snapshot comparison. UpdatePlan сохраняет forward journal без automatic legacy restore; UNKNOWN требует original-operation reconciliation, missing ledger не разрешает повтор; health error сохраняет данные/отдельные primary+cleanupcodes. Public capability update включена как development. Public no-override1→2→3/identity/sourcequiesce/health5PASS; precap177PASS/1XFAIL, root2actualdatafaultPASS и25contractPASS (исходныйAPIdeclarationFAIL сохранён иисправлен). Полный финальный source и independent installed update на этом SDK ещё PENDING. Production_ready=false, G0–G5 OPEN.

Следующий шаг: installed public1→2→3 с actual restore oracle и финальный полный source/integration на стабильных bytes. CP010 GitHub/CI success подтверждены; CP011sync pending до внешней receipt. Deadline16:00UTC/17:00CET.

## История пакета CP-20261026-010

Интегрирован Linux-only helper безопасного сравнения всех пяти live persistent roots со restored backup: bytes+dirs, fd-relative O_NOFOLLOW, links/hardlinks/reparse/гонки fail-closed; исключаются только bytes точного own update journal. Agent149PASS/1Windows-onlyskip (59 новых helper cases), root59PASS/2SKIP; два root fault tests ждут update capability, это НЕ PASS lifecycle. Installed update-readiness повторён на immutable SDKCP009/e4f8dbc:69SDK/7reference/161dependency files unchanged, fresh/preparation и три отказа update PASS. Полный update execution ещё в отдельном core worktree, публичная capability основной ветки false. По запросу владельца создана private веб-страница статуса; публикация Sites succeeded. Production_ready=false, все G0–G5 OPEN; baseline2497PASS относится к CP008/f90a20a, новый полный suite ещё NOT RUN.

Окно до16:00UTC/17:00CET сохраняется. Страница: [текущая работа](https://homensai-installer-work-20261026.hoseskhom.chatgpt.site). Sync CP009 подтверждён e4f8dbc/ref/tree; CP010 pending до внешней квитанции.

## История запуска CP-20261026-009

Возобновлена работа по новому указанию владельца до 17:00 Berlin (16:00UTC), старое окно архивировано. Полный baseline CP008/f90a20a:2497PASS/3skip/15subtests,542.76s,431inputs unchanged. Добавлены private update review schema2/from_adapter_binding и source adapter/failure policy в read-only engine plan; fresh schema1 и public7fields сохранены. Исходный RED4FAIL/2PASS сохранён; core112PASS/1XFAIL и отдельно64PASS, root11PASS/1XFAIL (наборы пересекаются). Installed update-readiness на SDKf90a20a: PASS, 69SDK/7reference/161dependency files unchanged; public fresh и readonly update preparation, отказы v2/reviewed-v2/v3 до mutation, effectcount1. Update execution/capability пока выключены, полный1→2→3 XFAIL/NOT RUN. Следующий пакет: scoped quiesce, реальный backup/receipt, live snapshot comparison и fail-closed forward journal без automatic legacy restore. Production_ready=false, G0–G5 OPEN.

Окно: 2026-10-26T14:35:31Z–16:00:00Z; Europe/Berlin15:35:31–17:00CET. Текущий sync pending до внешней квитанции. Нижележащие остановки CP008 — история.

Checkpoint: `CP-20261026-008` — ОСТАНОВЛЕНО ВЛАДЕЛЬЦЕМ, финальное сохранение перед выключением.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый local/GitHub до создания CP-20261026-008:
`85acee1aea939b20e83498be633e779b0e62261c` (CP-20261026-006).
SHA текущего checkpoint проверяется по Git history/ref и внешней sync receipt.
Не создавать рекурсивный commit ради записи собственного SHA.

## Финальный handoff CP-20261026-008

Работа остановлена по явному указанию владельца перед выключением. Single-step fresh Linux/native/user завершён: source171PASS+5subtests; root84targetedPASS; installed true fresh PASS на immutable SDK a5130d19343bfa4475147d8af34f653f7c30f7f2, 69 SDK/7 reference/161 dependency files и 3 signed bundle trees неизменны. Public factory→prepare→plan→submit, настоящий fresh proof, один migration effect, start/stop/duplicate start и encrypted full backup restore проверены. Legacy migration sentinel не исполнился; синтетический authorizer в этой приёмке не использовался. Runtime borrowed из DEV image: это не clean-host/standalone acceptance. Update1→2→3 ещё NOT IMPLEMENTED/NOT RUN, capability=false; полный suite после последнего fresh не повторялся (CP004 2416PASS исторический). Все G0–G5 OPEN, production_ready=false. Следующее точное действие — docs/NEXT_AI_HANDOFF_RU.md: RED adapter update test и source/target bindings + real backup per step. Агенты и тесты остановлены, компьютер владелец выключает сам.

**Новый ИИ: сначала [NEXT_AI_HANDOFF_RU.md](NEXT_AI_HANDOFF_RU.md).**
SDK freeze: `a5130d19343bfa4475147d8af34f653f7c30f7f2`; installed receipt:
`packaging/acceptance-evidence/installed-reference-fresh-20261026-agent01.json`.
Независимо сохранённые agent refs: core ddbe107, acceptance3a868cb.
Текущий CP GitHub SYNC_PENDING до внешней квитанции/ref/tree; собственного SHA здесь нет.
Старое 12-часовое окно остановлено досрочно по владельцу. Новая работа — только
по новому запросу; не повторять shutdown и не запускать тесты после этого handoff.

## История сохранения CP-20261026-006

По просьбе владельца сохранено текущее состояние. В основной ветке: явная SDK factory-регистрация ProductAdapter, 36 targeted PASS; установленная preparation на точном CP005 — 5 случаев PASS (69 SDK / 7 reference / 161 dependency files). Сохранён исходный harness FAIL неправильного имени capability, исправлено на registered. Fresh engine пакет ddbe107 прошёл 171 source PASS и 5 subtests, включая SIGKILL; он отправлен отдельной веткой GitHub и ещё не интегрирован root. Installed fresh и update по-прежнему NOT RUN; capabilities основной ветки не расширены этим source-only пакетом. Агентские ветки и основной handoff сохранены; активных тестовых процессов нет. production_ready=false, все G0–G5 OPEN. Продолжение: интегрировать ddbe107 с независимым review/public SDK resume/factory, затем installed fresh и updates 1→2→3. Исходное окно UTC08:25:06–20:25:06 сохраняется.

Агентские refs независимо подтверждены на GitHub:
- core: `ddbe10717ce8215ccea8925816488a69606b4f03`, ветка `codex/core-lifecycle-20261026-run01`;
- acceptance: `bbbbd8f82f5aada2022085cc904c99b6b39d2f21`, ветка `codex/platform-acceptance-20261026-run01`.

На другом ПК сначала clone/pull основной feature-ветки и чтение AI_START_HERE.md.
Для pending fresh использовать commit из core ref, не восстанавливать старый stash.
Runtime source пакета уже проверен агентом; root review/public SDK/installed fresh ещё обязательны.
Текущий CP GitHub SYNC_PENDING до внешней квитанции; собственного SHA в tracked docs нет.

## История выполнения CP-20261026-005

Реализованы явная NativeEngine.prepare_migration, неизменяемый MigrationReviewStore и проверка пустых каталогов/владельца для fresh. Обычный plan не вызывает hooks. SDK bridge проверен настоящим подписанным процессом и SQLite. Root воспроизвёл 4 FAIL: журналы FAILED/QUEUED/UNKNOWN/null допускали новый review; исправлено, исходный RED сохранён. Reference наблюдает схему без записи; изменение SQL и business receipt фиксируются одним COMMIT, неизвестный исход остаётся UNKNOWN. Объединённый целевой прогон: 201 PASS за 46.79 s, 11 входов не менялись. Агент проверил installed transport faults на точном e693025 с синтетическим authorizer; это не приёмка engine. Новый полный suite и установленный artifact CP005: NOT RUN. CP004 full 2416 PASS и maintenance 8 clients — исторические результаты. Adapter install/update ещё выключены до проверенного fresh/apply. production_ready=false, G0–G5 OPEN. Далее: installed public preparation на CP005, затем single-step fresh с настоящим proof/SIGKILL и updates 1→2→3.

Window UTC08:25:06–20:25:06 (Berlin09:25:06–21:25:06CET), statusRUNNING.
GitHub текущегоCP:SYNC_PENDING до внешнейreceipt.

## История выполнения CP-20261026-004

Интегрированы настоящие encrypted BackupReceiptStore с проверкой восстановленного signed payload/installation identity и default-deny migration authorizer. Maintenance receipt recovery_only не разрешает миграцию. Добавлен explicit SDK/BackendRouter prepare_migration bridge; NativeEngine preparation/install/update ещё unsupported в этом CP. Independent installed reference maintenance:8clientsPASS/69SDK files/7reference files, реальные process/HTTP/SQLite/archive restore, UID1000, source/package/deps unchanged; bootstraplegacy и runtime borrowed явно ограничены. Full frozen source/integration:2416PASS/3skip/15subtests,682.08s; после нормализацииEOF в двух docs выполнены78overlappingcontract checks и новыйinstalledwheelPASS. Все G0–G5OPEN, production_ready=false. Окно продолжается; следующий пакет explicit migration preparation/fresh witness, затем single-step apply.

Next: NativeEngine prep e693025 + reference plan3db7995, затем fresh/apply.
Окно UTC08:25:06–20:25:06; Europe/Berlin09:25:06–21:25:06CET.
Уточнение к CP003: локальное время ранее ошибочно записано10:25–22:25; UTC deadline не менялся.
GitHub новогоCP:SYNC_PENDING до внешней receipt.

## История выполнения CP-20261026-003

Начато 12-часовое окно 26.10.2026 08:25:06Z–20:25:06Z (10:25–22:25 Europe/Berlin); основной и два SOL6.1High работают. Baseline cd319a0:2363PASS/3skip/15subtests,525.37s,157inputs unchanged в Docker CPython3.14.3. GitHub baseline push/PR CI success; это source/scoped проверки. Исправлено устаревшее описание явного NativeEngine opt-in в integration guide и SDK resource. Старый cp021 worktree удалён после сохранения черновика Git stash8d32829; новые рабочие копии только внутри .local-state/agents. Следующий пакет: реальная backup receipt authorization и installed maintenance/reference. Production_ready=false, G0–G5OPEN.

Receipt: `packaging/acceptance-evidence/baseline-20261026-cp003.json`.
Не завершать запуск после baseline; продолжать core/acceptance пакеты.
GitHub этого CP: SYNC_PENDING до внешней квитанции.

## История входа CP-20261026-002

Добавлена единая точка входа AI_START_HERE.md через AGENTS/CLAUDE: восстановление состояния, конкретная первая задача, три агента и продолжение без повторного планирования. Зафиксировано разрешение брать GitHub-модули только после проверки лицензии точной версии и выполнения условий распространения; существующий license gate не ослаблен. Runtime не менялся, 12-часовое выполнение не запускалось.

Начать с `AI_START_HERE.md`; разрешённые зависимости — по
`docs/THIRD_PARTY_POLICY_RU.md`. Следующая инженерная задача не изменилась.
GitHub нового checkpoint: SYNC_PENDING до внешней квитанции.

## История подготовки CP-20261026-001

Подготовлен план SOL 6.1 High на 12 часов: основной агент и два помощника, migration/backup/fresh-empty и independent installed reference в приоритете; добавлены project config, AGENTS и fresh-clone recipe. Runtime не изменён; production_ready=false, G0–G5 OPEN. Старое одноразовое shutdown-задание не действует. 12-часовая реализация ещё не запускалась.

Вход: [START_HERE_RU](START_HERE_RU.md), [12-часовой план](SOL61_12H_PLAN_RU.md),
`SOL61_12H_START_PROMPT_RU.txt`. GitHub CP023 независимо подтверждён 26.10.2026:
`c6aff96b9f4359097ba8cb17d44fe429d8e2dfec`. Новый checkpoint SYNC_PENDING до внешней квитанции.
Все дальнейшие разделы CP023 и раньше — исторические. Указания выключить компьютер,
остановить соседнюю сессию и не начинать новые задачи НЕ ПОВТОРЯТЬ.

## Восстановление контекста

Прочитать CLAUDE.md, полный PROMPT, канонический ToDo, docs/work-state.json,
IMPLEMENTATION_STATUS.md, WORK_LOG.md, INSTALLER_API.md,
**VERIFICATION_BACKLOG.md и verification-status.json**. Невыполненные проверки
обязательны для следующего ИИ; NOT RUN/BLOCKED/FAIL означают НЕ ГОТОВО.
Отсутствующий тест сначала реализовать, потом выполнить; PASS только с receipt. Проверить branch,
dirty/staged files и активные процессы. Не сбрасывать чужие изменения и не
завершать произвольный PID. Частичный PASS не означает готовность выпуска.

106 задач: 8 DONE, 64 IN_PROGRESS, 3 BLOCKED, 31 TODO. HIM-006/007 закрыты
только как source audit и design target matrix. Все G0–G5 OPEN.
Список задач только в HomenSAI_Universal_Installer_TODO_RU.txt.

## Итоговый статус перед выключением CP023

CP-20261025-023 — окончательный статус по просьбе владельца, без новых задач. Все68 package files неизменны относительно проверенного CP021. Локальные результаты:2363PASS/3skip/15subtests; installed SDK/CLI и prerequisites PASS; Chromium24PASS ru/en/de; Windows492+57PASS/4POSIXskip. Последний код CP021 и документация CP022 подтверждены на GitHub. CP022 GitHub Actions push/PR завершились success: Linux2261source+102integration PASS,3skip,15subtests; Windows549PASS/4skip. Собственных процессов, активных контейнеров и слушающих TCP портов проекта не обнаружено; рабочая папка была чистой. Соседняя сессия остановлена по подтверждению владельца. После успешного сохранения и независимой проверки remote отправить ровно одну команду shutdown.exe /s /f /t30; результат хранить локальной квитанцией .git/hinstall-checkpoints/shutdown-20261025.json, не повторять автоматически. Этот doc-only checkpoint использует [skip ci] по указанию не запускать новые задачи. Production_ready=false, всеG0–G5OPEN; Docker Git BLOCKED до URL и авторизации. Следующая разработка: migration/backup-receipt/fresh-empty contract и independent installed reference lifecycle; возобновлять только по новому указанию владельца.

## Материалы продолжения CP022

CP-20261025-022 — только материалы продолжения: README теперь ведёт к актуальному RESUME и явно помечает обзор CP008 как исторический. Код и все68 package files совпадают с проверенным CP021; новых runtime-тестов для этой правки документации не запускали. Последний код: CP021, source2363PASS/3skip/15subtests; installed synthetic SDK/CLI, Chromium24cases и Windows492+57PASS/4POSIXskip сохранены с точными пределами. production_ready=false, всеG0–G5OPEN. Основная рабочая папка E:/homensai/homensai-install-modull; вспомогательный worktree E:/homensai/homensai-install-modull-core-cp021 сохраняет исходный проверенный черновик, интегрированный в основную ветку вместе с последующими исправлениями. Для продолжения использовать основную папку и её канонический ToDo. Собственные тестовые процессы завершены; отдельный Docker Git BLOCKED до URL и авторизации.

## Последний этап кода CP021

CP-20261025-021: подключён ExecutableProductAdapter к NativeEngine через явный trusted constructor opt-in только Linux/native/user start/stop/backup. Reviewed plan закрепляет signed descriptor/payload/runtime; readiness требует свежие release/installation/probe identity вместе с прежними owned PID/listener/HTTP. Durable quiesce intent и строгий ledger reconciliation не разрешают повтор UNKNOWN после утраты ledger или отсутствующей ожидаемой invocation. Backup использует настоящий полный encrypted архив. Исправлен воспроизведённый дефект: secondary recovery start/readiness больше не маскирует primary backup error, оба безопасных кода сохранены в native journal/public details. Все исходные FAIL и reverse-fault RED сохранены отдельно. Final source/integration: 2363 PASS, 3 skip, 15 subtests, inputs unchanged. Installed SDK/CLI и synthetic prerequisites PASS; actual Chromium установленного wheel: 24 PASS ru/en/de. Во всех трёх отдельных wheel совпадают 68 package files; SHA архивов различаются и не объявлены bit-reproducible. Windows10/Python3.14.3: 492 и отдельно57 PASS, 4 POSIX skip. Это не Windows11/platform acceptance. Installed ProductAdapter maintenance отдельно NOT RUN; новые maintenance процессы/HTTP/backup приняты на source уровне. Все G0–G5 OPEN, production_ready=false. Квитанция: packaging/acceptance-evidence/product-adapter-maintenance-20261025-cp021.json.

Следующий шаг: Реализовать migration_plan/apply с настоящими backup receipts и fresh-empty, затем независимый installed reference artifact и полный lifecycle; выполнить оставшуюся реальную четырёхплатформенную приёмку.

Сохранение перед выключением владельца: все действующие результаты в feature-ветке; собственные тестовые процессы завершить. Отдельный Docker Git требует URL репозитория и авторизацию, не создавать вымышленные remotes.

## Исторический этап CP020

CP-20261025-020: реализован безопасный browser local prerequisite upload: явный File, bounded raw streaming, signed size/SHA-256, private staging, opaque handles, cancel/interrupt, новый review/consents; исполнение использует прежние SDK/coordinator/lease. ru/en/de: 245 ключей, 39 Node групп (входят в pytest). Исправлены quota при неудачной очистке/открытом canceled writer, rollback временной plan reservation, подтверждённая SQLite zero-link sidecar race и Windows PID liveness без сигналов. Усилен test harness финализатора lease; старые FAIL сохранены. Полный Linux source/integration: 2309 PASS, 3 skip, 15 subtests, inputs unchanged. Установленные SDK/CLI и prerequisites: PASS; настоящий Chromium: 24 PASS. Во всех трёх wheel совпали 67 package files, разные SHA самих архивов явно сохранены. Windows10/Python3.14.3: 492 + 57 PASS (наборы раздельны), 4 POSIX skip; это НЕ Windows11 acceptance. CI CP019 реально выполнил Linux/Windows checks; Windows workflow расширен новыми regressions, текущий CI CP020 до push NOT RUN. Все G0–G5 OPEN, production_ready=false. Квитанция: packaging/acceptance-evidence/local-artifact-upload-20261025-cp020.json.

Следующий шаг: CP021: узкое trusted ProductAdapter maintenance подключение с signed review binding, свежей identity и строгим UNKNOWN reconciliation; сначала закрыть reviewer regression missing invocation. Затем реальная платформенная/reference приёмка.

Прочитать также сохранённый CONTINUE_DEVELOPMENT_PROMPT_RU.txt. Исторические блокировки CP018/019 не переносить автоматически на текущий Docker, browser и CI.

## Исторический этап CP019

CP-20261025-019: сохранён CONTINUE_DEVELOPMENT_PROMPT_RU.txt без изменения исходного задания; сверены CP018 branch/ref/HEAD. Новый Docker DEV build с CPython 3.14.3 и require-hashes доступен. До runtime-изменений проверены установленный SDK (65 package files, 7 CLI), prerequisites SDK/CLI (10 clients, 11 CLI) и документированный Windows source профиль (492 PASS; фактически Windows10Home, НЕ Windows11). Полный source/integration baseline ещё PENDING в изолированном snapshot; первая среда имеет недостающие gcc/system Python/D-Bus, готовится полный профиль. Отдельный Docker Git BLOCKED: remote не настроен, Gitea3010 API403. Все G0–G5 OPEN, production_ready=false. Квитанция: packaging/acceptance-evidence/baseline-environment-20261025-cp019.json.

Следующий шаг: Завершить полный baseline в дополненной Docker-среде; затем CP020: безопасный браузерный выбор локального prerequisite-файла, source/HTTP/Node и installed/browser проверки. До выключения в22:30 Berlin завершить проверенные checkpoints и push, непроверенное сохранить явно.

## Историческая среда CP018 и сохранение

Cloud Ubuntu 24.04.3 LTS x64, Python 3.12.14. Исторические CP-005 receipts
относятся к Python 3.14.3; результаты сред не смешивать. Начальная target matrix:
Windows 11 x64 и Ubuntu 24.04 LTS x86_64, четыре native/Docker пути, ru/en/de.
Это не Windows владельца и не его Docker/Forgejo. Docker CLI/socket и настоящий
systemd manager здесь отсутствуют; Windows runner не подключён.

Новый изолированный DEV build-env на CPython 3.14.3 подготовлен и проверен по
requirements-build.lock (receipts CP-011); source/HTTP CP-015 и новый installed
signed synthetic SDK/CLI CP-016 выполнены именно им. Production security approval
этого runtime НЕ ГОТОВО.

Sandbox PID не совпадает с mounted /proc. Реальные process tests выполнены
за этой изоляцией после проверки совпадения PID/proc, только с собственными
синтетическими процессами и loopback. Это не clean-host acceptance.

GitHub доступен через connector; shell HTTPS credentials отсутствуют. Точный
commit/tree/parent/ref проверять после push. Connector может назначить свои
commit metadata: импортировать точный remote commit только при совпадении tree,
не переписывать рабочие файлы и сохранять локальный checkpoint ref. В обычной
среде использовать tools/checkpoint.py и docs/CHECKPOINTS.md.
Cloud Git не заменяет отдельный Git владельца в Docker: он SYNC_BLOCKED.

## Что возобновлено и интегрировано

CP-006 восстановил точные 205 blobs/commit/tree CP-005. Python 3.12 baseline
выявил 41 Windows renderer failure: ntpath.isreserved заменён существующим
совместимым строгим validator. CP-007 добавил BackendRouter с reviewed target,
явным trusted Docker bootstrap и отдельными signed payload/terms выбранного
режима. Неподдержанный backend не заменяется другим автоматически.

CP-008 объединяет независимые source units в общем root checkpoint:

- DockerEngine install/uninstall-preserve в прежнем executor/journal, exact
  preloaded images, --pull=never, ownership/inspect/reconcile, signed retained
  recovery при потере cache, полный payload pin. Исправлены intent collision
  для component network и чужие namespace references по name/short ID.
- SystemdUserManager и trusted fixed runner: register/start/stop/enable/disable/
  uninstall, typed D-Bus, effective config, cgroup/process identity, durable
  receipts. Общий native root lock и native/user fence исключают параллельное
  управление registered/pending service writers. Полный payload закреплён
  в review; отклонённый fresh apply не создаёт ложный recovery fence.
- Signed PrerequisiteCoordinator: graph, auto/manual/recheck, отдельные terms,
  fixed adapters, durable attempts, cancel/resume/replan и reboot/relogin states.
  InstallerAPI делегирует тот же coordinator; READY не означает product SUCCESS.
  Это explicit synchronous SDK, без production registry и готового GUI auto-flow.
- Wizard action capabilities: режим доступен, если доступно хотя бы одно действие;
  выбранное неподдержанное действие блокируется, смена action/mode сбрасывает review.
- Актуальные product audit, target matrix и architecture boundaries сохранены.
  JobPilot dev 43d079c477d3e6489c1960e59a84c4f539d8d37b; Rent main
  a456f69480006b3a67558485368609b2d05874be. Продуктовые процессы не запускались.

Изменения worktree уже включены в CP-008. Не cherry-pick их повторно вслепую;
сначала проверить root tree/history. Главная очередь — канонический ToDo.



## Последний выполненный шаг

CP-20261025-018: Добавлен сохраняемый measured download_progress и согласованный SDK/CLI/HTTP/OpenAPI контракт: 17 полей с signed plan/artifact binding, одна транзакция snapshot/event, обязательный writer, защита от stale transfer/attempt и сохранение cancellation. 100% разрешены только после SHA-256 staged-файла, не означают READY. Replan очищает проекцию даже при прежнем digest; восстановление использует фактический partial offset, не сохранённый byte counter. Мастер ru/en/de показывает отдельную загрузку, потерю связи и дочитывает страницы events по полученным sequence. Исправлены воспроизведённые SQLite sidecar races в prerequisite store и execution domain; чтение/запись через connection уже созданного prerequisite store не пересоздаёт исчезнувшую основную БД. Fresh constructor сохраняет отдельное явное создание новой БД.

Combined source+integration Python 3.14.3: 2192 PASS, 1 Windows-only skip, 15 subtests (156.754 s, включая runner), 182 inputs неизменны. Текущий installed wheel: 10 isolated clients/11 CLI calls PASS (6.958 s), 65 package и 161 dependency files неизменны. Public SDK подтверждает verified100 до synthetic installer при RUNNING/notREADY; новый процесс дочитывает все events страницами limit 3. Shipped JS: 29 Node групп, ru/en/de по 227 ключей; Node входит в pytest wrapper. Сохранены исходные FAIL: mandatory writer/cleanup, early progress throttle, same-digest replan, UI pagination/unknown verified source и обе SQLite races, а также harness/environment отказы. Пересекающиеся focused/agent/Node counts не суммируются. Current actual browser/frozen, Windows/ACL/UAC, live Docker/services, реальные OS installers/reboot/relogin, hardware power-loss и полная product acceptance NOT RUN; CI CP017 четыре jobs без runner/steps. Все G0–G5 OPEN, production_ready=false.

Receipt: `packaging/acceptance-evidence/progress-20261025-cp018.json`.
Следующий шаг: Подтвердить CP018 GitHub ref/tree, затем безопасный явный выбор локального prerequisite artifact в мастере с signed pins и bounded handling; сохранить source и installed проверки. Обязательна actual browser/platform acceptance текущих artifacts при доступной среде. Параллельная core очередь: ExecutableProductAdapter/NativeEngine, migration backup contract и независимый reference через установленный публичный SDK; production catalog/signing/legal gates открыты.

## Исторический шаг CP-20261025-017

CP-20261025-017: Реализован общий product/prerequisite execution lease в доверенном original-owner domain: постоянный OS lock, durable claim/fencing и передача владения от reserve до завершения worker. Старые незавершённые v1 операции учитываются через проверяемый реестр известных журналов; новый API state_dir, отмена неизвестного эффекта и повреждённый/подменённый журнал не обходят блокировку. Синхронное исполнение больше не удерживает общий Python lock во время долгого engine; status/cancel доступны. Исправлена блокировка duplicate ACK/resume собственной product claim.

Точный combined source+integration на Python 3.14.3: 2097 PASS, 1 Windows-only skip, 15 subtests (148.508 s, включая runner); 176 входных файлов не менялись. Новый installed wheel: 10 clients/11 CLI calls PASS (7.109 s), 64 package и 161 dependency files неизменны. Независимые 57 source + 1 actual loopback HTTP PASS пересекаются с full suite и не суммируются. Исходные 2 sync-lock, 6 legacy-bypass, 4 malformed/inode и 2 GUI FAIL сохранены вместе с исправлениями, harness failures и непринятым прогоном с изменявшимися inputs. Windows/ACL/UAC, actual browser/frozen, реальные installers/reboot/relogin, live Docker/services, hardware power-loss и полная product acceptance NOT RUN; CI CP016: четыре jobs без runner/steps. Автоматическое восстановление control journal с новой файловой identity NOT IMPLEMENTED; неизвестные ранее незарегистрированные roots и старые binaries вне протокола не покрываются. Все G0–G5 OPEN, production_ready=false.

Receipt: `packaging/acceptance-evidence/execution-lease-20261025-cp017.json`.
Следующий шаг: После независимого подтверждения CP017 в GitHub объединить durable measured download progress: обязательный storage writer, свежая projection/event без потери cancel, retry fencing, 100% только после staged hash verification; подключить UI ru/en/de и API schema. Повторить combined source/integration и расширенный installed synthetic harness на новых bytes. Actual browser/OS/platform acceptance остаётся обязательной.

## Исторический шаг CP-20261025-016

CP-20261025-016: Добавлен воспроизводимый offline installed harness: новый wheel, чистый venv, pinned security closure с require-hashes, внешние isolated clients без checkout/network/subprocess. Проверены manual→READY, dependency-order auto с postconditions, потерянный ACK/idempotent replay, cancel/restore, подпись/согласия, events/attempts/redaction и настоящий CLI parser с trusted injection. Сохранён read-only разбор общего execution lease и durable progress; эти механизмы не реализованы.

Root current wheel: 10 независимых clients/11 CLI calls PASS (7.362 s), 63 package и 161 dependency files побайтно совпали до/после, ordinary denied attempts=0; отдельный guard self-test блокирует checkout/DNS/subprocess. Wheel 7cdf5f5e73054f3ccfbd2b7f6622cb390e114c1d21efb68b907553ce66a5a507. Два начальных agent harness FAIL и промежуточные PASS сохранены; ошибочно ожидалась CLI version1 вместо1.0. Все142 runtime/API/test inputs идентичны CP015; новый full pytest вCP016 не запускался, исходный scoped результат CP0151974PASS/3skips/10subtests +processHTTP5PASS не расширяется. Actual OS installers/process-kill/full product/frozen/browser/Windows/Docker NOT RUN. CI CP015 четыре jobs без runner/steps; full G1–G5 и production acceptance НЕ ГОТОВО.

Receipt: `packaging/acceptance-evidence/installed-prerequisites-summary-20261025-cp016.json`.
Следующий шаг: Реализовать общий атомарный product/prerequisite execution lease для доверенно зарегистрированного execution domain: durable ownership/fencing, handoff до enqueue/worker и independent-process crash/cancel tests. Затем durable measured byte progress с обязательным storage writer и truthful verification semantics. Проект следующего шага: docs/PREREQUISITES_NEXT_CORE_STEPS.md; implementation/tests пока NOT IMPLEMENTED/NOT RUN. Actual browser/OS/platform acceptance обязательна.

## Исторический шаг CP-20261025-015

CP-20261025-015: Реализован условный мастер подготовки через общий HTTP/SDK: auto/manual/use/skip, signed version requirements, отдельные product/license/action/plan согласия, progress состояний, явные cancel/recheck/replan/resume. Reload и потерянный ACK читают тот же ID без implicit start/resume; active=true/null блокирует действия, READY требует нового product preflight/terms/review. Исправлены подтверждённая потеря polling после Refresh и неверный inner SDK api_version в OpenAPI; HTTP envelope остаётся 1, SDK 1.0. ru/en/de: 215 ключей.

Полный source на Python 3.14.3: 1974 PASS, 3 skips, 10 subtests (75.88 s). Два Linux skip отдельно вместе с тремя настоящими loopback HTTP: 5 PASS (2.58 s); Windows case NOT RUN. Настоящий JS в Node VM: 19 новых групп и 6 прежних PASS, они входят в pytest wrappers и не прибавляются к source count. Исходные polling/API-schema FAIL сохранены с последующим PASS; newline hypothesis явно не подтверждена. Current installed wheel/frozen/actual browser/OS acceptance NOT RUN; browser BLOCKED, CI CP-014 четыре jobs без runner/steps. Global lease, durable byte progress и browser local artifact selection NOT IMPLEMENTED.

Receipt: `packaging/acceptance-evidence/gui-prerequisites-20261025-cp015.json`.
Следующий шаг: После подтверждения CP-015 в GitHub выполнить новый installed signed synthetic prerequisite SDK/CLI lifecycle из wheel вне checkout и сохранить CP-016. Затем общий атомарный product/prerequisite execution lease и durable measured byte progress; actual browser/platform acceptance остаётся обязательной.

## Исторический шаг CP-20261025-014

CP-20261025-014: В capabilities добавлена проверяемая проекция шести полей требований версии/обязательности/scope. Данные берутся из повторно проверенного подписанного каталога, без discovery, effects, путей или команд. Сохранённый plan v1, digest и consent refs не меняются; старая queued операция восстанавливается прежним контрактом. OpenAPI и встроенные API документы синхронизированы.

55 целевых source/API/HTTP проверок PASS, 0 skips (5.933 s), Python 3.14.3; 12 новых случаев входят в эти 55. Root сравнил все семь интегрированных файлов с проверенным commit 2c6d3c9 и receipt-хешами. Первый harness-прогон 9 PASS/3 FAIL сохранён: исправлено ошибочное ожидание отсутствующего store после trusted constructor. Новый полный source suite/installed wheel/browser/OS acceptance NOT RUN; source1961 PASS CP-013 остаётся историческим результатом других bytes. GUI ещё в разработке.

Receipt: `packaging/acceptance-evidence/requirements-projection-20261025-cp014.json`.
Следующий шаг: HIM-029/035/102: закончить условный GUI auto/manual/terms/progress/recovery и независимые Node contracts. Требования соединять с plan только при совпадении product/owner/target/catalog digest. READY и execution.active=false допускают только новый явный product preflight/terms/review; product submit сам не запускается. Затем отдельный installed signed prerequisite lifecycle и обязательные lease/byte-progress механизмы.

## Исторический шаг CP-20261025-013

CP-20261025-013: Восстановлена неотправленная локальная CP-012 и подтверждены GitHub ref/tree. Независимый повтор выявил устаревший снимок в тесте отмены: durable журнал уже содержал CANCELED. Дополнительно исправлена преждевременная проекция execution.active=False при финальном observer. Пока worker удерживает reservation — active=True; занятый/неизвестный внешний controller — active=null. Тест заново читает журнал после освобождения потока, не ослабляя требование CANCELED.

Текущие source bytes: 1961 PASS, 3 skips, 10 subtests (78.82 s), Python 3.14.3. Два пропущенных Linux process/D-Bus случая и три настоящих loopback HTTP случая отдельно: 5 PASS (2.61 s), JUnit receipt. Windows-only case NOT RUN. Пять детерминированных новых regressions FAIL на CP-012 и PASS после исправления; focused141 PASS (20.88 s), области пересекаются. Первоначальный recovery-прогон 1955 PASS/1 FAIL/3 skips сохранён. Новый installed/frozen/browser/full platform acceptance NOT RUN; Chromium --version вне sandbox также exit139, причина не установлена.

Receipt: `packaging/acceptance-evidence/recovery-20261025-cp013.json`.
Следующий шаг: HIM-029/035/102: условный GUI auto/manual/terms/progress/recovery для зарегистрированного coordinator, безопасная capabilities-проекция требований версий без изменения plan v1, независимые Node contract tests. Любой execution.active=true/null блокирует новый запуск/продолжение продукта, включая state=finished. После READY нужен новый явный product preflight/terms/review. Общий lease и durable byte-progress остаются обязательными отдельными core шагами.

## Исторический шаг CP-20261025-012

CP-20261025-012: Подключены HTTP routes и синхронный CLI к тому же доверенно зарегистрированному prerequisite coordinator. HTTP сравнивает полный signed payload, хранит планы на сервере, требует явные согласия и выдаёт durable background ACK. Status/events/cancel отвечают при медленных probes/installers; повтор start не становится resume. Подготовка и READY сбрасывают product review. Общий read-only busy guard видит claim старого каталога без раскрытия чужого ID. Исправлена реальная гонка отмены при sync recheck: принятая отмена не теряется при финальном сохранении, неизвестный installer сохраняет receipt/claim, отмена перед intent не запускает installer.

Текущий root full source на Python 3.14.3: 1956 PASS, 3 skips, 10 subtests (84.26 s); два Linux skip отдельно выполнены с matching PID namespace: 2 PASS (0.12 s), Windows-only case NOT RUN. HTTP focused 141 PASS и настоящий loopback 3 PASS; CLI focused 69 PASS; независимый core/HTTP review 12+8 PASS, области пересекаются. Installed SDK planning: 63 exact files и 7 CLI calls PASS, wheel 5c327073061674dec7917915ce07d4ceea5eb9e3e2685e9c3867d3b39bb197e1. Ранее выявленные FAIL/EPERM сохранены в истории. GUI flow, durable byte events и atomic cross-executor lease ещё NOT IMPLEMENTED; registered installed prerequisite lifecycle, live OS/Windows/Docker/browser/frozen acceptance NOT RUN. Browser BLOCKED: официальный альтернативный архив проверен, запуск SIGSEGV с неизвестной причиной. CI CP-011: 4 jobs без runner/steps, тесты NOT RUN.

Следующий шаг: HIM-029/035/102: условный GUI auto/manual/terms/progress/recovery для зарегистрированного coordinator, безопасная проекция требований версий, независимые Node contract tests. После READY — новый product preflight/terms/review по явному действию. Глобальный product/prerequisite lease и durable byte-progress остаются отдельными обязательными core шагами до полной приёмки.

## Историческая проверка CP-008

Полный source: 1856 PASS, 1 Windows-only path skip, 10 subtests, 70.24s.
Real Linux integration: 49 PASS, 5 subtests, 43.23s. Включены новые случаи:
служба регистрируется после reviewed backup/uninstall → отказ до stop/data effects.
Тесты используют synthetic app; рабочие JobPilot/Rent и их порты не затронуты.

SDK wheel построен и установлен вне checkout: 44 Python files/18 resources
сверены побайтно с исходниками и wheel, 5 CLI calls, ru/en/de, 0 network attempts,
state при offline discovery не создан. SHA-256:
`c4d862c6c0ea9c49b9126e0cae630fd70ef1b172e285d2f508606a68b6189268`.
Это SDK, не новый frozen installer. JS проверен в Node DOM/response contract,
новая визуальная browser acceptance CP-008 не выполнялась.

Точные scopes и поздние test-only platform guards находятся в receipts:
source-20261025-cp008.json, sdk-installed-20261025-cp008.json,
review-20261025-cp008.json. Modeled Docker/systemd не являются live acceptance;
private D-Bus и собственный child подтверждают только узкий runner boundary.

Исторические CP-005 frozen receipts сохраняют Linux GUI install, backup,
uninstall-preserve, restore, v1→v2→v3, bad-health automatic rollback, repair,
late rollback и SIGKILL→new controller/browser→explicit resume. Старый FAIL
мгновенного чтения RUNNING после SIGKILL сохранён. Этот результат не переносится
на новые CP-008 bytes; повтор нужен при выпуске нового frozen artifact.

## Следующие инженерные шаги

1. Проверить текущий checkpoint по GitHub ref/tree и local receipt. После каждого следующего шага обновить canonical TODO/RESUME/реестр, сохранить и подтвердить GitHub до продолжения.
2. Общий lease реализован и проверен в CP017; контракт и пределы — docs/EXECUTION_LEASE.md. Сохранить fail-closed для неизвестных эффектов и известных старых журналов. Автоматическое восстановление control journal с новой identity пока не реализовано; реальные Windows/ACL/installer/reboot проверки остаются открытыми.
3. Durable measured progress реализован в CP018: docs/PREREQUISITES_PROGRESS.md. Дальше безопасный явный local artifact selection с signed pins, bounded handling и отдельными GUI/SDK/HTTP проверками. Повторять installed harness после изменения package bytes. Production catalogue/pins/licences/host providers ещё НЕ ГОТОВО. Actual browser BLOCKED; 29 Node групп не принимают вёрстку/focus/keyboard/OS installation.
4. Подключить подписанный ExecutableProductAdapter к NativeEngine: reviewed descriptor binding и fresh readiness observation, затем migration contract с настоящими backup receipts и fresh-empty precondition. Добавить независимый reference через публичный установленный пакет. Не заменять UNKNOWN автоматическим rollback.
5. Docker: explicit abort/compensation interrupted install, trusted host attestation и image transport/import/offline bundle, update/restore/rollback/repair/reinstall. Live daemon в текущей среде BLOCKED.
6. Windows native bootstrap и narrow elevation; Windows Docker/WSL2; systemd service integration с общим lock lease/execution ownership. Отсутствие runner не означает, что реализация закончена.
7. Portable/legacy/multi-root recovery, modify_components, build/doctor facade, signed maintenance catalogue и mandatory production licence gate; затем thin product adapters JobPilot/Rent по pinned source audit. Исторический build2 FAIL не закрывать без исходных scenarios.
8. Актуальные DEV artifacts и полная reference/product acceptance на четырёх targets × ru/en/de, offline, migration/rollback/restore, reboot/power loss. Каждый NOT RUN/BLOCKED/FAIL из verification-status.json должен быть выполнен следующим ИИ при доступности реализации/среды. NOT IMPLEMENTED означает сначала реализовать недостающий механизм/тест.
9. Production signing/approved legal/licence decisions и отдельный Docker Git владельца остаются BLOCKED до реального подключения/решения. GitHub CI CP-017: четыре jobs без runner/steps; источник проблемы не доказан. Не менять billing/workflow по догадке.

Все G0–G5 OPEN; production_ready=false. Это инженерная очередь и конкретные внешние блокировки; отдельные source PASS не заменяют приёмку.

## Сохранённые ограничения

В прежних workspace large-bundle fixtures обнаружены лишние временные файлы с
неустановленным источником; один update остановился на backup:0. Failed fixtures
не удалять и inventory ради PASS не ослаблять. Последующие прогоны в новом /tmp
прошли, что не устанавливает прежнюю причину.

Private publisher key, архивные пароли, product secrets и launch URL tokens
не добавлять в Git/logs. Не открывать мастер на внешнем интерфейсе. Main/release
не публиковать без соответствующего разрешения. Продолжать независимую работу,
если заблокирован отдельный runner/remote; не выдавать source PASS за выпуск.
