# Продолжение разработки

Checkpoint: `CP-20261025-012`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый local/GitHub до создания CP-20261025-012:
`8e8075d895bd02e3027b7552e796f1ccf5de47e8` (CP-20261025-011).
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
requirements-build.lock (receipts CP-011); текущие source/SDK проверки CP-012
выполнены именно им. Production security approval этого runtime НЕ ГОТОВО.

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
2. Подключить conditional GUI prerequisites: auto/manual/terms/progress/recovery по HTTP CP-012, безопасная signed projection требований версий. READY сбрасывает product review и требует fresh preflight; установка продукта сама не начинается. Real browser остаётся BLOCKED, Node tests не заменяют browser acceptance.
3. Реализовать общий атомарный product/prerequisite execution lease и durable byte-progress stream. Текущий registered-store busy probe не закрывает cross-executor race. Production signed catalogue/pins/licences/host providers и browser local artifact selection также ещё НЕ ГОТОВО.
4. Подключить подписанный ExecutableProductAdapter к NativeEngine: reviewed descriptor binding и fresh readiness observation, затем migration contract с настоящими backup receipts и fresh-empty precondition. Добавить независимый reference через публичный установленный пакет. Не заменять UNKNOWN автоматическим rollback.
5. Docker: explicit abort/compensation interrupted install, trusted host attestation и image transport/import/offline bundle, update/restore/rollback/repair/reinstall. Live daemon в текущей среде BLOCKED.
6. Windows native bootstrap и narrow elevation; Windows Docker/WSL2; systemd service integration с общим lock lease/execution ownership. Отсутствие runner не означает, что реализация закончена.
7. Portable/legacy/multi-root recovery, modify_components, build/doctor facade, signed maintenance catalogue и mandatory production licence gate; затем thin product adapters JobPilot/Rent по pinned source audit. Исторический build2 FAIL не закрывать без исходных scenarios.
8. Актуальные DEV artifacts и полная reference/product acceptance на четырёх targets × ru/en/de, offline, migration/rollback/restore, reboot/power loss. Каждый NOT RUN/BLOCKED/FAIL из verification-status.json должен быть выполнен следующим ИИ при доступности реализации/среды. NOT IMPLEMENTED означает сначала реализовать недостающий механизм/тест.
9. Production signing/approved legal/licence decisions и отдельный Docker Git владельца остаются BLOCKED до реального подключения/решения. GitHub CI CP-011 jobs не получили runner/steps; источник проблемы не доказан. Не менять billing/workflow по догадке.

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
