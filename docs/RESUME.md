# Продолжение разработки

Checkpoint: `CP-20261025-011`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый local/GitHub до создания CP-20261025-011:
`a2a4f37703c47b06b96369081f7a8268d1e7bbdd` (CP-20261025-010).
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

CP-20261025-011: Добавлен явный background=True для prerequisite start/resume в общем SDK. Операция, план, согласия и claim сохраняются до ACK; свежая проверка плана выполняется перед первым эффектом и после обрыва. Status/list/cancel не ждут installer; повторный start не становится resume. Реальные process exits и гонки cancel/finish проверены; неизвестный эффект сохраняет RECOVERY_REQUIRED. Публичная фабрика принимает доверенный coordinator, list ограничен owner/product/target/catalog и не раскрывает private plan/receipts.

Финальный source focused: 185 PASS (11.84 s), новые async cases: 17 PASS; независимый review: 7 race cases PASS; после интеграции root: 26 PASS (3.71 s). Новый installed SDK planning contract: 63 exact files/5 CLI calls PASS, wheel 931f6ec22a0550f5f5b8b6289dd3329adf1f959927f735d59996154c176c6535. Отдельный baseline CP-010 на Python 3.14.3: 1858 PASS/3 skips/10 subtests; 49 integration + 2 ранее пропущенных Linux cases = отдельный прогон 51 PASS/5 subtests. Windows case NOT RUN. Текущий новый full source/frozen/platform acceptance НЕ ГОТОВ; GUI browser BLOCKED (официальный endpoint вернул HTML), CI CP-010 без runner/steps. Exact scopes и предыдущие ошибки записаны в receipts.

Следующий шаг: HIM-029/035/062: безопасный HTTP и синхронный CLI transport для зарегистрированного coordinator; затем GUI auto/manual/terms/progress/recovery с новым product review после READY. Production registry и platform acceptance остаются открыты.

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
2. Завершить prerequisites по цепочке: безопасный HTTP/CLI поверх CP-011 → GUI auto/manual/terms/progress/recovery. READY сбрасывает product review и требует fresh preflight; установка продукта сама не начинается. Production signed catalogue/pins/licences/host providers остаются отдельной работой.
3. Подключить подписанный ExecutableProductAdapter к NativeEngine: reviewed descriptor binding и fresh readiness observation, затем migration contract с настоящими backup receipts и fresh-empty precondition. Добавить независимый reference через публичный установленный пакет. Не заменять UNKNOWN автоматическим rollback.
4. Docker: explicit abort/compensation interrupted install, trusted host attestation и image transport/import/offline bundle, update/restore/rollback/repair/reinstall. Live daemon в текущей среде BLOCKED.
5. Windows native bootstrap и narrow elevation; Windows Docker/WSL2; systemd service integration с общим lock lease/execution ownership. Отсутствие runner не означает, что реализация закончена.
6. Portable/legacy/multi-root recovery, modify_components, build/doctor facade, signed maintenance catalogue и mandatory production licence gate; затем thin product adapters JobPilot/Rent по pinned source audit. Исторический build2 FAIL не закрывать без исходных scenarios.
7. Актуальные DEV artifacts и полная reference/product acceptance на четырёх targets × ru/en/de, offline, migration/rollback/restore, reboot/power loss. Каждый NOT RUN/BLOCKED/FAIL из verification-status.json должен быть выполнен следующим ИИ при доступности реализации/среды.
8. Production signing/approved legal/licence decisions и отдельный Docker Git владельца остаются BLOCKED до реального подключения/решения. GitHub CI CP-008 jobs не получили runner/steps; источник проблемы не доказан. Не менять billing/workflow по догадке.

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
