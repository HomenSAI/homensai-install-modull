# Продолжение разработки

Checkpoint: `CP-20261025-009`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый local/GitHub до создания CP-009:
`960827cb32b65c9748c9a3f4eaee8406dfe27606` (CP-008).
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

106 задач: 8 DONE, 62 IN_PROGRESS, 4 BLOCKED, 32 TODO. HIM-006/007 закрыты
только как source audit и design target matrix. Все G0–G5 OPEN.
Список задач только в HomenSAI_Universal_Installer_TODO_RU.txt.

## Среда и сохранение

Cloud Ubuntu 24.04.3 LTS x64, Python 3.12.14. Исторические CP-005 receipts
относятся к Python 3.14.3; результаты сред не смешивать. Начальная target matrix:
Windows 11 x64 и Ubuntu 24.04 LTS x86_64, четыре native/Docker пути, ru/en/de.
Это не Windows владельца и не его Docker/Forgejo. Docker CLI/socket и настоящий
systemd manager здесь отсутствуют; Windows runner не подключён.

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

## Последняя проверка

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

1. Проверить текущий CP-009 local/GitHub ref/tree. CP-008 уже подтверждён;
   Node contract stage со скриншота завершён. CI CP-008 runs40188356899/40188360805
   завершились до runner/steps: source tests NOT RUN, HIM-086 BLOCKED.
   На каждом следующем шаге сначала проверить, обновить TODO/RESUME, сохранить
   checkpoint и подтвердить GitHub ref/tree, только затем переходить дальше.
   Последние наблюдённые CP-007 runs40186123426/40186125750 completed/failure
   до runner allocation: runner_id=0, steps=[]. Причина через API неизвестна.
   Не менять workflow/платёжные настройки по догадке; HIM-086 BLOCKED.
2. Подключить systemd platform port к общему NativeEngine service lifecycle.
   Сейчас native/service unavailable. Затем принять реальный systemd user
   manager, logout/relogin/reboot, restart policy и product quiescence.
   Windows SCM/WinSW, UAC/ACL/secret loader — отдельная незавершённая реализация.
3. Docker: реализовать trusted host attestation для обычного пользователя;
   docker group не гарантирует чтение root dockerd /proc. Нужны approved image
   transport/import/offline bundle, update/restore/rollback/repair/reinstall и
   явный abort/compensation failed install. Retained recovery не содержит образы,
   restart=no, установка поверх оставшихся данных не допускается автоматически.
4. Завершить Windows native bootstrap и узкое повышение прав. Подготовить
   production signed prerequisite catalogue с реальными pins/licence decisions,
   host providers, publisher checks, Windows boot/login markers и общий GUI/CLI
   auto/manual flow. READY должен запускать fresh product preflight; реальный
   reboot/logoff требует принятой host integration, coordinator их не выполняет.
5. Интегрировать thin product adapters JobPilot/Rent по source audit без
   product-specific веток общего ядра. JobPilot native worktree недоступны,
   build2 rollback FAIL остаётся. Новый Rent payload требует приёмки миграции0036;
   существующий business archive не равен полному DataHome/secrets archive.
6. Выполнить clean-host reference и product acceptance по точным artifacts:
   четыре targets × три языка, users/paths/ports, offline, migrations, recovery,
   retained data, service profiles и аппаратные reboot/power-loss boundaries.
7. Закрыть exact runtime/frozen/image licences/notices, обязательный release
   checker, production publisher keys/certificates и утверждённые legal texts.
   Production ready=false; ключи и решения не заменять фиктивными approvals.

Это реальные инженерные работы плюс внешние блокировки; весь остаток нельзя
объяснять только отсутствием доступа. Docker Git владельца подключать отдельно
по установленной схеме; cloud loopback не является его Forgejo.

## Сохранённые ограничения

В прежних workspace large-bundle fixtures обнаружены лишние временные файлы с
неустановленным источником; один update остановился на backup:0. Failed fixtures
не удалять и inventory ради PASS не ослаблять. Последующие прогоны в новом /tmp
прошли, что не устанавливает прежнюю причину.

Private publisher key, архивные пароли, product secrets и launch URL tokens
не добавлять в Git/logs. Не открывать мастер на внешнем интерфейсе. Main/release
не публиковать без соответствующего разрешения. Продолжать независимую работу,
если заблокирован отдельный runner/remote; не выдавать source PASS за выпуск.
