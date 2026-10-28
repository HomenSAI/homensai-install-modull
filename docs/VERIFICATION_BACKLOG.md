# Невыполненные проверки — обязательное задание следующему ИИ

Terra CP-20261028T143633Z-a8c51a2df8c6: installed update lost-ACK PASS в
изолированном Linux DEV контейнере на SDK 6edcbb83; реальный signed adapter,
backup authorizer и public SDK, инъекция потери ответа после apply. Дубликат
submit не повторил effect, resume той же операции SUCCEEDED. Это не SIGKILL,
power-loss или clean-host; остальные installed faults и ACC-09/12 остаются открыты.

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

Автор: Serhii Khomenko. CP-20261025-018.

**Полная приёмка НЕ ГОТОВА; production_ready=false.** Этот документ и
`verification-status.json` описывают проверки. Единственный список реализации —
`HomenSAI_Universal_Installer_TODO_RU.txt`; его ID/критерии не заменяются этим реестром.

## Финальная scoped приёмка CP-20261026-008

Работа остановлена по явному указанию владельца перед выключением. Single-step fresh Linux/native/user завершён: source171PASS+5subtests; root84targetedPASS; installed true fresh PASS на immutable SDK a5130d19343bfa4475147d8af34f653f7c30f7f2, 69 SDK/7 reference/161 dependency files и 3 signed bundle trees неизменны. Public factory→prepare→plan→submit, настоящий fresh proof, один migration effect, start/stop/duplicate start и encrypted full backup restore проверены. Legacy migration sentinel не исполнился; синтетический authorizer в этой приёмке не использовался. Runtime borrowed из DEV image: это не clean-host/standalone acceptance. Update1→2→3 ещё NOT IMPLEMENTED/NOT RUN, capability=false; полный suite после последнего fresh не повторялся (CP004 2416PASS исторический). Все G0–G5 OPEN, production_ready=false. Следующее точное действие — docs/NEXT_AI_HANDOFF_RU.md: RED adapter update test и source/target bindings + real backup per step. Агенты и тесты остановлены, компьютер владелец выключает сам.

## Сохранённое состояние CP-20261026-006

По просьбе владельца сохранено текущее состояние. В основной ветке: явная SDK factory-регистрация ProductAdapter, 36 targeted PASS; установленная preparation на точном CP005 — 5 случаев PASS (69 SDK / 7 reference / 161 dependency files). Сохранён исходный harness FAIL неправильного имени capability, исправлено на registered. Fresh engine пакет ddbe107 прошёл 171 source PASS и 5 subtests, включая SIGKILL; он отправлен отдельной веткой GitHub и ещё не интегрирован root. Installed fresh и update по-прежнему NOT RUN; capabilities основной ветки не расширены этим source-only пакетом. Агентские ветки и основной handoff сохранены; активных тестовых процессов нет. production_ready=false, все G0–G5 OPEN. Продолжение: интегрировать ddbe107 с независимым review/public SDK resume/factory, затем installed fresh и updates 1→2→3. Исходное окно UTC08:25:06–20:25:06 сохраняется.

## Проверенный preparation пакет CP-20261026-005

Реализованы явная NativeEngine.prepare_migration, неизменяемый MigrationReviewStore и проверка пустых каталогов/владельца для fresh. Обычный plan не вызывает hooks. SDK bridge проверен настоящим подписанным процессом и SQLite. Root воспроизвёл 4 FAIL: журналы FAILED/QUEUED/UNKNOWN/null допускали новый review; исправлено, исходный RED сохранён. Reference наблюдает схему без записи; изменение SQL и business receipt фиксируются одним COMMIT, неизвестный исход остаётся UNKNOWN. Объединённый целевой прогон: 201 PASS за 46.79 s, 11 входов не менялись. Агент проверил installed transport faults на точном e693025 с синтетическим authorizer; это не приёмка engine. Новый полный suite и установленный artifact CP005: NOT RUN. CP004 full 2416 PASS и maintenance 8 clients — исторические результаты. Adapter install/update ещё выключены до проверенного fresh/apply. production_ready=false, G0–G5 OPEN. Далее: installed public preparation на CP005, затем single-step fresh с настоящим proof/SIGKILL и updates 1→2→3.

## Проверенный первый пакет CP-20261026-004

Интегрированы настоящие encrypted BackupReceiptStore с проверкой восстановленного signed payload/installation identity и default-deny migration authorizer. Maintenance receipt recovery_only не разрешает миграцию. Добавлен explicit SDK/BackendRouter prepare_migration bridge; NativeEngine preparation/install/update ещё unsupported в этом CP. Independent installed reference maintenance:8clientsPASS/69SDK files/7reference files, реальные process/HTTP/SQLite/archive restore, UID1000, source/package/deps unchanged; bootstraplegacy и runtime borrowed явно ограничены. Full frozen source/integration:2416PASS/3skip/15subtests,682.08s; после нормализацииEOF в двух docs выполнены78overlappingcontract checks и новыйinstalledwheelPASS. Все G0–G5OPEN, production_ready=false. Окно продолжается; следующий пакет explicit migration preparation/fresh witness, затем single-step apply.

## Baseline активного запуска CP-20261026-003

Начато 12-часовое окно 26.10.2026 08:25:06Z–20:25:06Z (10:25–22:25 Europe/Berlin); основной и два SOL6.1High работают. Baseline cd319a0:2363PASS/3skip/15subtests,525.37s,157inputs unchanged в Docker CPython3.14.3. GitHub baseline push/PR CI success; это source/scoped проверки. Исправлено устаревшее описание явного NativeEngine opt-in в integration guide и SDK resource. Старый cp021 worktree удалён после сохранения черновика Git stash8d32829; новые рабочие копии только внутри .local-state/agents. Следующий пакет: реальная backup receipt authorization и installed maintenance/reference. Production_ready=false, G0–G5OPEN.

## Материалы CP-20261026-001

Подготовлены план/настройка трёх SOL 6.1 High агентов и перенос с GitHub.
Runtime не менялся, исторические PASS не расширены; полный suite не повторялся.
Новая проверка handoff: `packaging/acceptance-evidence/handoff-20261026-cp001.json`.
Дальнейшая реализация — по `SOL61_12H_PLAN_RU.md`; все прежние gaps остаются открыты.

## Правило владельца от 25.10.2026

При каждом возобновлении прочитать этот реестр, канонический ToDo и RESUME.
Все неисполненные тесты нужно выполнить, когда доступны реализация и среда.
Не объявлять невыполненное проверенным, не считать наличие тестового файла
результатом теста и не переносить прежний PASS на изменённый artifact.

| Статус | Значение |
|---|---|
| PASS | Проверка реально выполнена в указанной области, есть результат/receipt. |
| FAIL | Проверка запускалась и не прошла; исходный результат сохраняется. |
| NOT RUN | Не запускалась. Готовность не подтверждена. |
| BLOCKED | Не запускалась из-за указанной среды, зависимости или решения. |
| PENDING | Выполнение/полная приёмка не завершены. |

`test_code=NOT IMPLEMENTED` означает, что воспроизводимый тест ещё надо написать;
`PARTIAL` — есть только часть harness/source coverage. Ни одно не означает PASS.
`ready=false` снимается только после всех критериев данной области. Ошибки
ранних попыток сохранять в истории, новый успех записывать отдельной квитанцией.

## Что подтверждено сейчас

- CP-20261025-018: Combined source+integration Python 3.14.3: 2192 PASS, 1 Windows-only skip, 15 subtests (156.754 s, включая runner), 182 inputs неизменны. Текущий installed wheel: 10 isolated clients/11 CLI calls PASS (6.958 s), 65 package и 161 dependency files неизменны. Public SDK подтверждает verified100 до synthetic installer при RUNNING/notREADY; новый процесс дочитывает все events страницами limit 3. Shipped JS: 29 Node групп, ru/en/de по 227 ключей; Node входит в pytest wrapper. Сохранены исходные FAIL: mandatory writer/cleanup, early progress throttle, same-digest replan, UI pagination/unknown verified source и обе SQLite races, а также harness/environment отказы. Пересекающиеся focused/agent/Node counts не суммируются. Current actual browser/frozen, Windows/ACL/UAC, live Docker/services, реальные OS installers/reboot/relogin, hardware power-loss и полная product acceptance NOT RUN; CI CP017 четыре jobs без runner/steps. Все G0–G5 OPEN, production_ready=false. Receipt: `packaging/acceptance-evidence/progress-20261025-cp018.json`.

- CP-20261025-017: Точный combined source+integration на Python 3.14.3: 2097 PASS, 1 Windows-only skip, 15 subtests (148.508 s, включая runner); 176 входных файлов не менялись. Новый installed wheel: 10 clients/11 CLI calls PASS (7.109 s), 64 package и 161 dependency files неизменны. Независимые 57 source + 1 actual loopback HTTP PASS пересекаются с full suite и не суммируются. Исходные 2 sync-lock, 6 legacy-bypass, 4 malformed/inode и 2 GUI FAIL сохранены вместе с исправлениями, harness failures и непринятым прогоном с изменявшимися inputs. Windows/ACL/UAC, actual browser/frozen, реальные installers/reboot/relogin, live Docker/services, hardware power-loss и полная product acceptance NOT RUN; CI CP016: четыре jobs без runner/steps. Автоматическое восстановление control journal с новой файловой identity NOT IMPLEMENTED; неизвестные ранее незарегистрированные roots и старые binaries вне протокола не покрываются. Все G0–G5 OPEN, production_ready=false. Receipt: `packaging/acceptance-evidence/execution-lease-20261025-cp017.json`.

- CP-20261025-016: Root current wheel: 10 независимых clients/11 CLI calls PASS (7.362 s), 63 package и 161 dependency files побайтно совпали до/после, ordinary denied attempts=0; отдельный guard self-test блокирует checkout/DNS/subprocess. Wheel 7cdf5f5e73054f3ccfbd2b7f6622cb390e114c1d21efb68b907553ce66a5a507. Два начальных agent harness FAIL и промежуточные PASS сохранены; ошибочно ожидалась CLI version1 вместо1.0. Все142 runtime/API/test inputs идентичны CP015; новый full pytest вCP016 не запускался, исходный scoped результат CP0151974PASS/3skips/10subtests +processHTTP5PASS не расширяется. Actual OS installers/process-kill/full product/frozen/browser/Windows/Docker NOT RUN. CI CP015 четыре jobs без runner/steps; full G1–G5 и production acceptance НЕ ГОТОВО. Receipt: `packaging/acceptance-evidence/installed-prerequisites-summary-20261025-cp016.json`.

- CP-20261025-015: Полный source на Python 3.14.3: 1974 PASS, 3 skips, 10 subtests (75.88 s). Два Linux skip отдельно вместе с тремя настоящими loopback HTTP: 5 PASS (2.58 s); Windows case NOT RUN. Настоящий JS в Node VM: 19 новых групп и 6 прежних PASS, они входят в pytest wrappers и не прибавляются к source count. Исходные polling/API-schema FAIL сохранены с последующим PASS; newline hypothesis явно не подтверждена. Current installed wheel/frozen/actual browser/OS acceptance NOT RUN; browser BLOCKED, CI CP-014 четыре jobs без runner/steps. Global lease, durable byte progress и browser local artifact selection NOT IMPLEMENTED. Receipt: `packaging/acceptance-evidence/gui-prerequisites-20261025-cp015.json`.

- CP-20261025-014: 55 целевых source/API/HTTP проверок PASS, 0 skips (5.933 s), Python 3.14.3; 12 новых случаев входят в эти 55. Root сравнил все семь интегрированных файлов с проверенным commit 2c6d3c9 и receipt-хешами. Первый harness-прогон 9 PASS/3 FAIL сохранён: исправлено ошибочное ожидание отсутствующего store после trusted constructor. Новый полный source suite/installed wheel/browser/OS acceptance NOT RUN; source1961 PASS CP-013 остаётся историческим результатом других bytes. GUI ещё в разработке. Receipt: `packaging/acceptance-evidence/requirements-projection-20261025-cp014.json`.

- CP-20261025-013: Текущие source bytes: 1961 PASS, 3 skips, 10 subtests (78.82 s), Python 3.14.3. Два пропущенных Linux process/D-Bus случая и три настоящих loopback HTTP случая отдельно: 5 PASS (2.61 s), JUnit receipt. Windows-only case NOT RUN. Пять детерминированных новых regressions FAIL на CP-012 и PASS после исправления; focused141 PASS (20.88 s), области пересекаются. Первоначальный recovery-прогон 1955 PASS/1 FAIL/3 skips сохранён. Новый installed/frozen/browser/full platform acceptance NOT RUN; Chromium --version вне sandbox также exit139, причина не установлена. Receipt: `packaging/acceptance-evidence/recovery-20261025-cp013.json`.

- CP-20261025-012: Текущий root full source на Python 3.14.3: 1956 PASS, 3 skips, 10 subtests (84.26 s); два Linux skip отдельно выполнены с matching PID namespace: 2 PASS (0.12 s), Windows-only case NOT RUN. HTTP focused 141 PASS и настоящий loopback 3 PASS; CLI focused 69 PASS; независимый core/HTTP review 12+8 PASS, области пересекаются. Installed SDK planning: 63 exact files и 7 CLI calls PASS, wheel 5c327073061674dec7917915ce07d4ceea5eb9e3e2685e9c3867d3b39bb197e1. Ранее выявленные FAIL/EPERM сохранены в истории. GUI flow, durable byte events и atomic cross-executor lease ещё NOT IMPLEMENTED; registered installed prerequisite lifecycle, live OS/Windows/Docker/browser/frozen acceptance NOT RUN. Browser BLOCKED: официальный альтернативный архив проверен, запуск SIGSEGV с неизвестной причиной. CI CP-011: 4 jobs без runner/steps, тесты NOT RUN.
- CP-20261025-011: Финальный source focused: 185 PASS (11.84 s), новые async cases: 17 PASS; независимый review: 7 race cases PASS; после интеграции root: 26 PASS (3.71 s). Новый installed SDK planning contract: 63 exact files/5 CLI calls PASS, wheel 931f6ec22a0550f5f5b8b6289dd3329adf1f959927f735d59996154c176c6535. Отдельный baseline CP-010 на Python 3.14.3: 1858 PASS/3 skips/10 subtests; 49 integration + 2 ранее пропущенных Linux cases = отдельный прогон 51 PASS/5 subtests. Windows case NOT RUN. Текущий новый full source/frozen/platform acceptance НЕ ГОТОВ; GUI browser BLOCKED (официальный endpoint вернул HTML), CI CP-010 без runner/steps. Exact scopes и предыдущие ошибки записаны в receipts.
- CP-20261025-010: CP-010: 63 целевых source tests PASS (1.69 s); отдельно установленный SDK/CLI PASS: 62 exact package files, 5 CLI commands, 4 SDK refusal cases, plan reload, 0 network/child-process attempts, нет product effects. Wheel SHA-256 46be06ff502e13975f49e8599283129af419959caf1d21b41f25532a3c737a28. Первые EPERM/test-harness FAIL сохранены в sdk-public-contract-20261025-cp010-attempts.json. Runtime fixture неисполняемый; reference wheel не исполнен; lifecycle/Windows/Docker/full G1 NOT RUN, НЕ ГОТОВО.
- CP-008 `960827c` восстановлен в новой изолированной чистой копии и сверён с
  фактическим GitHub ref/tree. Возврат к CP-005 не нужен.
- CP-009: 37 настоящих temporary Git/checkpoint recovery tests и один Node
  wizard capability contract — **38 PASS, 3.46 s**, Python 3.12.14.
- CP-008 CI: четыре задания завершились с failure до выделения runner; steps
  отсутствуют. Это **BLOCKED**, а не выполненные тесты с отрицательным результатом.
- Полный source suite/installed SDK CP-008 и frozen Linux CP-005 — исторические
  scoped receipts. Новые Windows/Docker/systemd/reboot/clean-host проверки отсутствуют.

## Как выполнить оставшиеся проверки

Сначала выбрать задачу и сценарий ниже, проверить наличие реализации и теста.
Если теста нет — реализовать его на синтетических данных; это отдельный шаг.
Указать artifact SHA-256, source SHA, OS/version/arch/mode/scope/locale,
команду/действия, expected/observed, exit status, время и очищенный receipt.
Выполнить проверку в подходящей среде. При FAIL сохранить ошибку и точку
восстановления. При отсутствии среды записать конкретный BLOCKED и следующее
действие; независимая разработка продолжается.

После каждого шага: проверить → обновить соответствующий HIM/ACC и RESUME →
проверить diff → checkpoint → GitHub → независимо подтвердить ref/tree →
следующий шаг. Docker Git владельца отдельно SYNC_BLOCKED до его подключения.

Нельзя запускать строки из этого файла как shell script. Команды нужно
прочитать и адаптировать к проверенному test fixture. Никаких реальных
email/Telegram, пользовательских баз, чужих процессов или ports 8810/8811/8812.

## Конкретные незавершённые механизмы — состояние CP-018

Наличие прошедших transport/source тестов не закрывает эти требования.
Каждый пункт также записан в `verification-status.json` → `implementation_gaps`.

| Механизм | Реализация / тест | Что должен сделать следующий исполнитель |
|---|---|---|
| Общая атомарная блокировка product/prerequisite executors | Source IMPLEMENTED; independent process/HTTP и installed synthetic PASS | Принять реальные платформы и installers. Гарантия ограничена trusted domain и известными зарегистрированными journals; автоматический перенос control journal на новую файловую identity не реализован. Контракт: `EXECUTION_LEASE.md`. |
| GUI auto/manual/terms/progress/recovery | Source IMPLEMENTED; Node contract PASS; browser NOT RUN/BLOCKED | Выполнить actual browser rendering/focus/keyboard/localization и installed/platform acceptance на новых bytes. 29 progress/prerequisite и 6 capability Node групп не заменяют браузер. |
| Installed prerequisite SDK/CLI lifecycle | Synthetic installed profile PASS; real OS NOT RUN | Профиль CP018 повторять после изменения package bytes; настоящие installers/product lifecycle/platform scenarios принять отдельно. |
| Durable progress скачанных байтов | Source/API/UI IMPLEMENTED; synthetic source/HTTP/installed PASS | Выполнить actual browser/OS и реальные network fault acceptance. Snapshot хранит observed bytes, не durable offset; 100% относится только к staged artifact. `PREREQUISITES_PROGRESS.md`. |
| Выбор локального prerequisite installer через браузер | Source/SDK/HTTP/Node/installed browser IMPLEMENTED и scoped PASS CP020; полная platform acceptance PENDING | Сохранить bounded upload/opaque handles/signed pins и quota fencing; выполнить реальные installers/OS/offline/TLS и безопасную межсессионную retention. |

Текущая browser acceptance BLOCKED: официальный альтернативный архив получен
и ZIP CRC проверен, но executable и `--version` падают с SIGSEGV; причина неизвестна.
CI CP-017 также BLOCKED: четыре задания без runner/steps, тесты не запускались.
Факты и последующие действия: `packaging/acceptance-evidence/environment-20261025-cp012.json`
и `packaging/acceptance-evidence/github-ci-20261025-cp017.json`.

Первоначальный разбор двух core-шагов сохранён в
`docs/PREREQUISITES_NEXT_CORE_STEPS.md`. Lease реализован в CP017 с отдельной
квитанцией; progress реализован в CP018 с отдельными исходными FAIL и итоговыми проверками.

## Полные сценарии, которые ещё НЕ ГОТОВЫ

### ACC-01 — NOT RUN; НЕ ГОТОВО

Чистая GUI-установка без developer tools; auto/manual prerequisites. Задачи: HIM-029–045. Начальное состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Собрать актуальные DEV artifacts; чистые desktop ОС без developer tools; выбрать auto/manual, проверить обязательные зависимости и результат установки. Production prerequisite registry и Windows bootstrap ещё требуют реализации.

Существующая частичная проверка: `packaging/check_reference_gui.cjs`.

### ACC-02 — NOT RUN; НЕ ГОТОВО

Все компоненты, backend/DB/workers, HTML/JS/CSS, browser smoke. Задачи: HIM-040, HIM-042–045, HIM-078, HIM-082. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Проверить реальные HTML/JS/CSS, продуктовую БД, все workers и browser smoke на каждой заявленной поставке.

Существующая частичная проверка: `packaging/check_reference_gui.cjs`.

### ACC-03 — NOT RUN; НЕ ГОТОВО

Обычный пользователь, две OS-учётные записи, кириллица/пробелы, другой администратор в UAC, user/service scope. Задачи: HIM-019, HIM-028, HIM-047, HIM-094. Состояние: NOT RUN.

Готовность тестового сценария: **NOT IMPLEMENTED**. Подготовить два тестовых OS users, пути с пробелами/кириллицей, другой admin в UAC; проверить ACL, DPAPI и сохранение исходного владельца. Не использовать личные DataHome.

### ACC-04 — NOT RUN; НЕ ГОТОВО

Чужой занятый порт, повторный start, параллельные installers, отсутствие второго scheduler и корректный stop. Задачи: HIM-020, HIM-046–047, HIM-080. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Повторить на установленном artifact: чужой порт/процесс, две конкурирующие операции, repeat start/stop, отсутствие второго scheduler. Source tests не заменяют installed proof.

Существующая частичная проверка: `integration_tests/test_native_engine.py`.

### ACC-05 — NOT RUN; НЕ ГОТОВО

Отключение сети, retry/resume/cache, локальный prerequisite, proxy/TLS errors без отключения проверки сертификатов. Задачи: HIM-033, HIM-046, HIM-048. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Реальный контролируемый TLS/proxy transport: обрыв/resume/range/cache/неверный локальный package; доверие TLS сохраняется. Live harness надо завершить.

Существующая частичная проверка: `tests/test_downloads.py`.

### ACC-06 — NOT RUN; НЕ ГОТОВО

Неверный hash/signature, подмена manifest, publisher/target/channel, обычный update с недопустимым downgrade. Задачи: HIM-021, HIM-089–090. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Проверить изменённые shipped bytes, manifest+payload, чужой ключ/publisher/target/channel/downgrade на каждом artifact; production доверие пока не принято.

Существующая частичная проверка: `tests/test_trust.py`.

### ACC-07 — NOT RUN; НЕ ГОТОВО

Принятие/отказ EULA, отдельные условия prerequisites, receipt, доступность с клавиатуры, незаполненное согласие. Задачи: HIM-027, HIM-091. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Для точных ru/en/de условий проверить отказ/scroll/keyboard/масштаб, receipt hash, отдельные prerequisites consent и отсутствие эффекта при отказе.

Существующая частичная проверка: `packaging/check_reference_gui.cjs`, `tests/test_wizard.py`.

### ACC-08 — NOT RUN; НЕ ГОТОВО

Reboot/relogin, повторный preflight, refresh/close/reopen GUI; прогресс не выдаёт waiting/failure за 100% SUCCESS. Задачи: HIM-034–035, HIM-046. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Настоящие reboot/relogin, GUI close/reopen/refresh; сохранение operation identity, новый preflight, отсутствие ложного 100%. OS runners отсутствуют; не имитировать reboot сменой PID.

Существующая частичная проверка: `tests/test_prerequisites_execution.py`, `tests/test_wizard_capabilities.py`.

### ACC-09 — NOT RUN; НЕ ГОТОВО

Fresh v1 → update v2 → update v3; повтор версии и reinstall. Задачи: HIM-054, HIM-058, HIM-072. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Новые v1/v2/v3 bytes: update/reinstall/повтор версии с проверкой БД, user files и реального runtime. Docker/Windows lifecycle ещё не завершён.

Существующая частичная проверка: `packaging/check_reference_upgrade.cjs`.

### ACC-10 — NOT RUN; НЕ ГОТОВО

Full backup и фактический restore с БД, файлами, ключами и runtime; неверный пароль, отсутствующий ключ, legacy format, несколько roots. Задачи: HIM-050–053, HIM-061, HIM-081. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Расшифровать и восстановить полный архив в другой каталог/host: БД, файлы, ключи, runtime. Проверить wrong password/missing key/legacy/multi-root. Multi-root/legacy bridge требуется реализовать.

Существующая частичная проверка: `packaging/check_reference_maintenance.cjs`, `tests/test_archive_crypto.py`.

### ACC-11 — NOT RUN; НЕ ГОТОВО

Ошибка stop/backup/migration/readiness, before-rollback, late rollback и отсутствие old release/runtime/metadata. Задачи: HIM-055–057, HIM-078, HIM-093. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. На новых frozen bytes вызвать stop/backup/migration/health failures; проверить before-rollback и old runtime. Сохраняется исторический JobPilot build2 FAIL; все 16 исходных metadata cases нужно получить и повторить, отсутствующие не выдумывать.

Существующая частичная проверка: `packaging/check_reference_upgrade.cjs`, `integration_tests/test_native_engine.py`.

### ACC-12 — NOT RUN; НЕ ГОТОВО

Реальный process-kill на границах journal/staging/migration/pointer/ restore/readiness; повтор не дублирует побочный эффект. Задачи: HIM-056–057, HIM-072. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Реальный SIGKILL только своего controller на каждой опасной границе journal/stage/migration/pointer/restore/readiness; после restart проверить тот же operation_id и отсутствие двойных эффектов. Существующий harness покрывает узкую staging boundary.

Существующая частичная проверка: `packaging/check_reference_restart.cjs`.

### ACC-13 — NOT RUN; НЕ ГОТОВО

Repair той же версии; modify_components; сохранное удаление, недоступный DataHome, uninstall → reinstall/recovery. Задачи: HIM-058–060, HIM-095. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Repair без изменения данных; modify_components, повреждённый/unavailable DataHome, owned-only uninstall, reinstall и restore сохранённого recovery. Отсутствующие lifecycle действия сначала реализовать.

Существующая частичная проверка: `packaging/check_reference_maintenance.cjs`.

### ACC-14 — NOT RUN; НЕ ГОТОВО

Недостаток места, занятый файл, исчезнувший носитель, ошибка прав, symlink/junction/reparse и опасный archive path. Задачи: HIM-019, HIM-048, HIM-051, HIM-093. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. На тестовых файловых системах проверить disk-full, locked file, пропавший носитель, ACL/symlink/junction/reparse и архивные атаки. Fault injection в unit не выдавать за real OS acceptance.

Существующая частичная проверка: `tests/test_archive.py`, `tests/test_datahome.py`.

### ACC-15 — NOT RUN; НЕ ГОТОВО

Установка/update/recovery offline при действительно отключённой сети. Задачи: HIM-070, HIM-087, HIM-095. Состояние: NOT RUN.

Готовность тестового сценария: **NOT IMPLEMENTED**. Полный новый offline bundle: реально ограничить сеть в изолированной VM/контейнере; install/update/restore должны использовать имеющиеся runtime/images/resources. Запрет сети в source tests — другая проверка.

### ACC-16 — NOT RUN; НЕ ГОТОВО

API: dry-run, unsupported capability, PLAN_STALE, idempotency, событие повторно/по cursor, согласованность SDK/CLI/HTTP/OpenAPI. Задачи: HIM-024, HIM-062–064. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Установить wheel вне checkout; пройти публичные SDK/CLI/HTTP/openapi сценарии dry-run/unsupported/PLAN_STALE/idempotency/events; отдельно полный установленный lifecycle.

Существующая частичная проверка: `tests/test_public_api.py`, `tests/test_backends.py`.

### ACC-17 — NOT RUN; НЕ ГОТОВО

Известная/неизвестная ошибка, доказанная/предполагаемая причина, первичная ошибка и последующая rollback error; безопасное исправление. Задачи: HIM-065, HIM-068. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. В установленном мастере воспроизвести известную/неизвестную ошибку и primary+rollback failure; проверить безопасное explanation и реальную возможность выбранного recovery.

Существующая частичная проверка: `tests/test_diagnostics.py`, `tests/test_diagnostics_api.py`.

### ACC-18 — NOT RUN; НЕ ГОТОВО

Недоступная/повреждённая DiagnosticStore, bounded fallback, идемпотентный импорт, retention, нет рекурсивного logger failure. Задачи: HIM-066–067. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Установленный bootstrap: недоступная/повреждённая diagnostic DB, fallback/retention/import и disk failure без рекурсии и ложного завершения транзакции.

Существующая частичная проверка: `tests/test_diagnostics.py`.

### ACC-19 — NOT RUN; НЕ ГОТОВО

Статистика по уникальным operations/attempts: retries/reboot/дубли, FAILED_ROLLED_BACK, период/знаменатель, redacted support export. Задачи: HIM-068–069, HIM-094. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Сверить статистику с уникальными operation/attempt IDs, reboot/retries/FAILED_ROLLED_BACK; UI/API/support export должны исключить синтетические secrets.

Существующая частичная проверка: `tests/test_diagnostics_api.py`.

### ACC-20 — NOT RUN; НЕ ГОТОВО

Второй проект вне checkout ядра по публичным docs/API; следующая совместимая версия модуля не ломает интеграцию. Задачи: HIM-083–084. Состояние: NOT RUN.

Готовность тестового сценария: **NOT IMPLEMENTED**. Отдельный разработчик подключает реальный второй продукт только по установленному SDK/public docs; затем следующая совместимая версия. Аудит исходников не заменяет эту приёмку.

### ACC-21 — BLOCKED; НЕ ГОТОВО

Checkpoint разработки: обрыв до/во время push, один remote недоступен, dirty files/конфликт, новая сессия без чата, постоянное Docker-хранилище. Задачи: HIM-003–004, HIM-009, HIM-071, HIM-098. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. 37 Git failure/recovery tests повторены в CP-009; новый независимый checkout восстановлен. Отдельно на доступном Docker Git владельца проверить постоянное хранилище/оба remotes без удаления его контейнеров.

Существующая частичная проверка: `tests/test_checkpoint.py`.

### ACC-22 — BLOCKED; НЕ ГОТОВО

SBOM/лицензии фактических компонентов, внешнее доверие к bootstrap, production/test signing различены; secrets отсутствуют в поставке. Задачи: HIM-088–091, HIM-094, HIM-096. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. По точным shipped bytes проверить SBOM/notices/transitive licences, внешний trust root и production signing. Не заменять ключи/юридическое утверждение тестовыми APPROVED.

Существующая частичная проверка: `tests/test_license_policy.py`, `tools/check_licenses.py`.

### ACC-23 — NOT RUN; НЕ ГОТОВО

Языки ru/en/de: 12 сочетаний reference GUI, сохранение выбора, fallback, полный error/help catalog, EULA receipts, key/placeholder parity. Задачи: HIM-101–106. Состояние: NOT RUN.

Готовность тестового сценария: **PARTIAL**. Все 12 reference GUI сочетаний: ru/en/de × четыре target. Проверить keyboard/zoom/long German/Cyrillic/fallback/refresh/maintenance, key/placeholder parity и hashes показанных условий.

Существующая частичная проверка: `tests/test_i18n.py`, `tests/test_wizard.py`.

## Матрица новых artifacts

| Reference GUI | ru | en | de |
|---|---|---|---|
| Windows native | NOT RUN | NOT RUN | NOT RUN |
| Windows Docker | BLOCKED: нет Windows/WSL2 runner | BLOCKED | BLOCKED |
| Linux native | NOT RUN: новая полная приёмка | NOT RUN | NOT RUN |
| Linux Docker | BLOCKED: нет live daemon | BLOCKED | BLOCKED |

Историческая частичная Linux проверка CP-005 сохранена и не обнуляется;
она не является приёмкой всех сценариев новых bytes. JobPilot/Rent имеют
собственные отдельные проверки, сейчас NOT RUN. Отдельные необязательные
service profiles не заменяют обязательные четыре базовых пути.

## Отдельно сохранить известные проблемы

- JobPilot build2 rollback: исторический FAIL 3/16, нужны первичные scenarios
  и новый installed artifact. Source patch не закрывает риск.
- Старые frozen fixture integrity failures и ранний RUNNING после SIGKILL
  находятся в WORK_LOG/receipts. Не удалять их и не ослаблять inventory.
- Реальные reboot/relogin/power loss, Windows UAC/ACL/DPAPI, live Docker и
  systemd, cross-host restore, production licences/signing/legal approval
  ещё не проверены/не приняты. Причину каждой блокировки сохранять отдельно.


## CP-20261025-019 — восстановление и начальные проверки

CP-20261025-019: сохранён CONTINUE_DEVELOPMENT_PROMPT_RU.txt без изменения исходного задания; сверены CP018 branch/ref/HEAD. Новый Docker DEV build с CPython 3.14.3 и require-hashes доступен. До runtime-изменений проверены установленный SDK (65 package files, 7 CLI), prerequisites SDK/CLI (10 clients, 11 CLI) и документированный Windows source профиль (492 PASS; фактически Windows10Home, НЕ Windows11). Полный source/integration baseline ещё PENDING в изолированном snapshot; первая среда имеет недостающие gcc/system Python/D-Bus, готовится полный профиль. Отдельный Docker Git BLOCKED: remote не настроен, Gitea3010 API403. Все G0–G5 OPEN, production_ready=false. Квитанция: packaging/acceptance-evidence/baseline-environment-20261025-cp019.json.

Следующий шаг: Завершить полный baseline в дополненной Docker-среде; затем CP020: безопасный браузерный выбор локального prerequisite-файла, source/HTTP/Node и installed/browser проверки. До выключения в22:30 Berlin завершить проверенные checkpoints и push, непроверенное сохранить явно.


## CP-20261025-020 — local artifact upload и исправления baseline

CP-20261025-020: реализован безопасный browser local prerequisite upload: явный File, bounded raw streaming, signed size/SHA-256, private staging, opaque handles, cancel/interrupt, новый review/consents; исполнение использует прежние SDK/coordinator/lease. ru/en/de: 245 ключей, 39 Node групп (входят в pytest). Исправлены quota при неудачной очистке/открытом canceled writer, rollback временной plan reservation, подтверждённая SQLite zero-link sidecar race и Windows PID liveness без сигналов. Усилен test harness финализатора lease; старые FAIL сохранены. Полный Linux source/integration: 2309 PASS, 3 skip, 15 subtests, inputs unchanged. Установленные SDK/CLI и prerequisites: PASS; настоящий Chromium: 24 PASS. Во всех трёх wheel совпали 67 package files, разные SHA самих архивов явно сохранены. Windows10/Python3.14.3: 492 + 57 PASS (наборы раздельны), 4 POSIX skip; это НЕ Windows11 acceptance. CI CP019 реально выполнил Linux/Windows checks; Windows workflow расширен новыми regressions, текущий CI CP020 до push NOT RUN. Все G0–G5 OPEN, production_ready=false. Квитанция: packaging/acceptance-evidence/local-artifact-upload-20261025-cp020.json.

Оставшиеся проверки: Полные ACC01–23 и 12 reference GUI target×locale сочетаний; Windows11/native Setup/UAC/ACL/другой администратор/реальные reboot и relogin; Ubuntu24 clean native installer, live Docker product lifecycle/systemd acceptance; Реальные prerequisite OS installers и контролируемые live TLS/proxy/offline failures; Полный независимый installed reference artifact, JobPilot/Rent lifecycle; Межсессионная безопасная очистка retained uploads и перенос control journals на новую identity; Production signing/legal/licence approvals и полная воспроизводимость выпускных artifacts.

Следующий шаг: CP021: узкое trusted ProductAdapter maintenance подключение с signed review binding, свежей identity и строгим UNKNOWN reconciliation; сначала закрыть reviewer regression missing invocation. Затем реальная платформенная/reference приёмка.


## CP-20261025-021 — ProductAdapter maintenance и сохранение primary error

CP-20261025-021: подключён ExecutableProductAdapter к NativeEngine через явный trusted constructor opt-in только Linux/native/user start/stop/backup. Reviewed plan закрепляет signed descriptor/payload/runtime; readiness требует свежие release/installation/probe identity вместе с прежними owned PID/listener/HTTP. Durable quiesce intent и строгий ledger reconciliation не разрешают повтор UNKNOWN после утраты ledger или отсутствующей ожидаемой invocation. Backup использует настоящий полный encrypted архив. Исправлен воспроизведённый дефект: secondary recovery start/readiness больше не маскирует primary backup error, оба безопасных кода сохранены в native journal/public details. Все исходные FAIL и reverse-fault RED сохранены отдельно. Final source/integration: 2363 PASS, 3 skip, 15 subtests, inputs unchanged. Installed SDK/CLI и synthetic prerequisites PASS; actual Chromium установленного wheel: 24 PASS ru/en/de. Во всех трёх отдельных wheel совпадают 68 package files; SHA архивов различаются и не объявлены bit-reproducible. Windows10/Python3.14.3: 492 и отдельно57 PASS, 4 POSIX skip. Это не Windows11/platform acceptance. Installed ProductAdapter maintenance отдельно NOT RUN; новые maintenance процессы/HTTP/backup приняты на source уровне. Все G0–G5 OPEN, production_ready=false. Квитанция: packaging/acceptance-evidence/product-adapter-maintenance-20261025-cp021.json.

Следующий шаг: Реализовать migration_plan/apply с настоящими backup receipts и fresh-empty, затем независимый installed reference artifact и полный lifecycle; выполнить оставшуюся реальную четырёхплатформенную приёмку.

Ограничения: ProductAdapter install/update/restore/rollback/repair/reinstall/uninstall paths are explicitly unsupported before effects; Migration plan/apply with real backup receipt contract and fresh-empty still requires implementation; Standalone durable ledger file identity across new constructors/forensic valid-image replacement and trusted relocation remain open; Full installed ProductAdapter maintenance lifecycle: NOT RUN; current real maintenance processes/HTTP/backup tested from source; Independent external reference setup artifact/full lifecycle and JobPilot/Rent acceptance NOT RUN; Windows11/UAC/ACL/real reboot/relogin, Ubuntu clean native installer/systemd service and product Docker backend full acceptance NOT RUN; 12 reference target×locale combinations and full ACC01–23 not complete; Production signing/licence/legal approvals and release reproducibility open.


## CP-20261025-022 — сохранение перед выключением

CP-20261025-022 — только материалы продолжения: README теперь ведёт к актуальному RESUME и явно помечает обзор CP008 как исторический. Код и все68 package files совпадают с проверенным CP021; новых runtime-тестов для этой правки документации не запускали. Последний код: CP021, source2363PASS/3skip/15subtests; installed synthetic SDK/CLI, Chromium24cases и Windows492+57PASS/4POSIXskip сохранены с точными пределами. production_ready=false, всеG0–G5OPEN. Основная рабочая папка E:/homensai/homensai-install-modull; вспомогательный worktree E:/homensai/homensai-install-modull-core-cp021 сохраняет исходный проверенный черновик, интегрированный в основную ветку вместе с последующими исправлениями. Для продолжения использовать основную папку и её канонический ToDo. Собственные тестовые процессы завершены; отдельный Docker Git BLOCKED до URL и авторизации.


## CP-20261025-023 — завершение и выключение

CP-20261025-023 — окончательный статус по просьбе владельца, без новых задач. Все68 package files неизменны относительно проверенного CP021. Локальные результаты:2363PASS/3skip/15subtests; installed SDK/CLI и prerequisites PASS; Chromium24PASS ru/en/de; Windows492+57PASS/4POSIXskip. Последний код CP021 и документация CP022 подтверждены на GitHub. CP022 GitHub Actions push/PR завершились success: Linux2261source+102integration PASS,3skip,15subtests; Windows549PASS/4skip. Собственных процессов, активных контейнеров и слушающих TCP портов проекта не обнаружено; рабочая папка была чистой. Соседняя сессия остановлена по подтверждению владельца. После успешного сохранения и независимой проверки remote отправить ровно одну команду shutdown.exe /s /f /t30; результат хранить локальной квитанцией .git/hinstall-checkpoints/shutdown-20261025.json, не повторять автоматически. Этот doc-only checkpoint использует [skip ci] по указанию не запускать новые задачи. Production_ready=false, всеG0–G5OPEN; Docker Git BLOCKED до URL и авторизации. Следующая разработка: migration/backup-receipt/fresh-empty contract и independent installed reference lifecycle; возобновлять только по новому указанию владельца.
