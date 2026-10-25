# Продолжение разработки

Checkpoint: `CP-20261025-016`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый local/GitHub до создания CP-20261025-016:
`fde526435a7061cf9037251bfeb87bcc04bbe3f1` (CP-20261025-015).
SHA текущего checkpoint проверяется по Git history/ref и внешней sync receipt.
Не создавать рекурсивный commit ради записи собственного SHA.

## Восстановление контекста

Прочитать CLAUDE.md, полный PROMPT, канонический ToDo, docs/work-state.json,
IMPLEMENTATION_STATUS.md, WORK_LOG.md, INSTALLER_API.md,
**VERIFICATION_BACKLOG.md и verification-status.json**. Невыполненные проверки
обязательны для следующего ИИ; NOT RUN/BLOCKED/FAIL означают НЕ ГОТОВО.
Отсутствующий тест сначала реализовать, потом выполнить; PASS только с receipt. Проверить branch,
dirty/staged files и активные процессы. Не сбрасывать чужие изменения и не
завершать произвольный PID. Частичный PASS не означает готовность выпуска.

106 задач: 8 DONE, 63 IN_PROGRESS, 4 BLOCKED, 31 TODO. HIM-006/007 закрыты
только как source audit и design target matrix. Все G0–G5 OPEN.
Список задач только в HomenSAI_Universal_Installer_TODO_RU.txt.

## Среда и сохранение

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
2. Реализовать общий атомарный product/prerequisite execution lease: trusted domain, durable claim/fencing, handoff до enqueue/worker, независимые process/crash/cancel tests. Текущий busy probe не закрывает cross-executor race. Чтение кода и предложенный протокол: docs/PREREQUISITES_NEXT_CORE_STEPS.md; реализация и новые race tests пока NOT IMPLEMENTED/NOT RUN.
3. Реализовать durable measured byte progress: обязательная запись отдельно от best-effort callback, свежий snapshot без потери cancel, retry/verification и отсутствие ложного100%. CP016 installed synthetic SDK/CLI уже PASS; после core изменений повторить его на новом wheel. Production signed catalogue/pins/licences/host providers и browser local artifact selection ещё НЕ ГОТОВО. Current actual browser остаётся BLOCKED; Node не принимает вёрстку/focus/keyboard/OS installation.
4. Подключить подписанный ExecutableProductAdapter к NativeEngine: reviewed descriptor binding и fresh readiness observation, затем migration contract с настоящими backup receipts и fresh-empty precondition. Добавить независимый reference через публичный установленный пакет. Не заменять UNKNOWN автоматическим rollback.
5. Docker: explicit abort/compensation interrupted install, trusted host attestation и image transport/import/offline bundle, update/restore/rollback/repair/reinstall. Live daemon в текущей среде BLOCKED.
6. Windows native bootstrap и narrow elevation; Windows Docker/WSL2; systemd service integration с общим lock lease/execution ownership. Отсутствие runner не означает, что реализация закончена.
7. Portable/legacy/multi-root recovery, modify_components, build/doctor facade, signed maintenance catalogue и mandatory production licence gate; затем thin product adapters JobPilot/Rent по pinned source audit. Исторический build2 FAIL не закрывать без исходных scenarios.
8. Актуальные DEV artifacts и полная reference/product acceptance на четырёх targets × ru/en/de, offline, migration/rollback/restore, reboot/power loss. Каждый NOT RUN/BLOCKED/FAIL из verification-status.json должен быть выполнен следующим ИИ при доступности реализации/среды. NOT IMPLEMENTED означает сначала реализовать недостающий механизм/тест.
9. Production signing/approved legal/licence decisions и отдельный Docker Git владельца остаются BLOCKED до реального подключения/решения. GitHub CI CP-015: четыре jobs без runner/steps; источник проблемы не доказан. Не менять billing/workflow по догадке.

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
