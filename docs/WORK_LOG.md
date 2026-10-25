# Журнал выполнения

## CP-20261025-001 — 25.10.2026, Europe/Berlin

Пользователь поручил подготовить PROMPT и ToDo, затем исполнить весь список,
сохранять результаты в GitHub и поддержать ru/en/de в установщике.

Созданы два согласованных документа 1.1: 106 задач, G0–G5, 23 сценария приёмки.
DAG зависимостей проверен, циклов и отсутствующих task IDs нет.

Baseline GitHub main обновился до a16a55d58e65ccf64ed2ba0c853a0053baea6689.
Добавленное ранее AES-шифрование архивов сохраняется. Snapshot проверен по
71 Git blob и root tree 1ba3789cbeea817c476342c8d0a7c2c5cb411bb5.
Создана отдельная ветка codex/universal-installer-20261025.

HIM-001/002: PASS. HIM-003: BLOCKED для локального Docker Git владельца.
HIM-004: документы подготовлены, синхронизация первого checkpoint выполняется.
Дальнейшая независимая разработка продолжается; готовность install/update/
restore/rollback не заявляется без реальных проверок.

## CP-20261025-002 — общий API, Linux lifecycle, доверие и диагностика

CP-001 подтверждён: f99d302bbf944248ae1e3d2d5d9cc6e2d899d31c локально
и в GitHub. Новый checkpoint содержит SDK/CLI, schemas/discovery/OpenAPI,
SQLite plans/operations/events/idempotency, NativeEngine и Ed25519 verifier,
40 кодов ошибок на ru/en/de и устойчивую диагностику. GUI и prerequisites
продолжаются отдельно; не входят в принятие этого checkpoint.

Новая общая проверка исходников: Python 3.14.3, `pytest -q tests` —
1233 passed, 1 skipped, 10 subtests passed, 24.03 s.
Реальные loopback/process проверки перенесены в integration_tests, чтобы
unit tests сохраняли полный запрет сети. Первоначальный FAIL был вызван
неверным размещением этих тестов; после переноса весь unit suite PASS.

Отдельно NativeEngine: 15 PASS на synthetic Linux user-session; публичный
API→NativeEngine: 1 PASS, включающий 7 lifecycle действий. Это не доказательство
Windows/Docker/service/frozen GUI acceptance. Wheel установлен вне checkout,
его offline schemas/locales проверены; финальная сборка будет повторена.

HIM-004/005 закрыты. Остальные частично реализованные задачи сохраняют
IN_PROGRESS с областью фактического PASS. G0–G5 открыты. Блокировки: реальный
Docker/Forgejo владельца, Windows/Docker runners, production signing и юридическое
утверждение. Продолжать независимые GUI/reference, prerequisites и checkpoint tooling.

## CP-20261025-003 — GUI, frozen reference, prerequisites, checkpoint automation

CP-002 подтверждён локально/GitHub: b2d99a2bf75aa8e35328f64c3b8b08dc618bceda.
Добавлены GUI ru/en/de, защищённый HTTP transport, product-bound resume list,
preview/export diagnostics, reference SQLite product, builder и настоящий Linux
frozen bootstrap. Chromium прошёл fresh installation, языки, согласия и web readiness.

Source suite: 1363 passed, 1 skipped, 10 subtests (29.84s). После этого отдельный
расширенный wizard suite: 10 PASS. Native/API/HTTP integration: 24 PASS (27.99s).
Checkpoint automation:37 PASS на настоящих temporary Git/bare repos.
Downloads/prerequisites:56 PASS; read-only/manual first, auto OS install unsupported.

Frozen GUI04: install и encrypted backup PASS. Следующее uninstall остановилось
на extra file в recovery inventory; live release hashes целы. Источник изменения
не установлен. Содержимое сохранено, проверка не ослаблена. Полный happy-path
в следующем чистом bundle обязателен; отказ не записывается как успешное удаление.

Аудит лицензий обнаружил Berkeley DB в лишнем _dbm extension и bundled ensurepip;
reference builder теперь исключает BDB, pip bootstrap и ненужные Tcl/Tk. Встроенные
libpython dependencies и frozen loader всё равно требуют полного inventory/notices.
Production release заблокирован до лицензионного gate, ключей/сертификата и EULA.

Следующая очередь: чистый frozen bundled lifecycle + crash/browser-restart resume,
полный licence gate, signed executable ProductAdapter/scaffold, platform adapters
и настоящие JobPilot/Rent. Docker/Windows runners и Forgejo владельца не подключены.


## CP-20261025-004 — реальный GUI lifecycle, reference versions, adapter и build gates

CP-003 подтверждён локально/GitHub: db85d13811ea63b42142f97ca6a1e7fae76ba54c.
Source full suite1493PASS/1skip/10subtests35.41s. До этого новый field product_version
вызвал2FAIL в diagnostic projection; пустое значение теперь нормализуется, общий
прогон повторён. Native/API/HTTP26PASS; GUI/trust/prerequisite53PASS. Отдельный
concurrency test проверил отсутствие смены секретов между submit и регистрацией
активной операции. Эти числа описывают разные наборы, не суммируются.

Frozen bundle05:896signedfiles, полностью самостоятельный Linux executable/runtime.
Настоящий браузер прошёл install(ru),backup(en),uninstall(de),restore(ru),stop(en),
реальный сайт/SQLite и сохранение user file; errors/search/stats/export PASS.
Открытые токены и архивные пароли в Git не помещались. Private DEV key используется
только вне repo для последовательных update builds; production signing отсутствует.

Reference v1/v2/v3: настоящие DDL/data migrations, настройки, optional secret,
transactional migration failure/health failure;30source tests. В отдельном /tmp
fixture frozen GUI обновился v1→v2→v3 с проверкой фактической schema/version и файла.
До этого workspace bundle-v3 получил extra hidden libpython tempfile; guard отверг
его до effects. Новый workspace update остановился на backup:0/before-rollback.
Источник изменения неизвестен; failed fixtures сохранены, ослабления проверки нет.

Executable ProductAdapter: signed descriptor/callback/runtime, argv без shell,
bounded execution, durable effect ledger, unknown effect запрещает blind retry.
Scaffold требует настоящие hashes/inputs; иначе DRAFT/BLOCKED. Protocol ещё не
подменяет legacy Native hooks автоматически. Добавлены integration guide и API docs.

Build pipeline:17pins/22wheelhashes с exact installed matching,29new build tests,
SDK wheel build PASS, source CI Linux/Windows с pinned action commits. Actual CI
ещё NOT RUN до remote workflow результата. Source suite не заменяет Windows setup.
License gate принимает только actual artifact evidence;05inventory/gateBLOCKED:
896payload,902selectedartifacts,73componentcandidates,29removed.352signals —
не352юридических нарушений. BDB/Tcl/ensurepip удалены; static/native closure,
notices/OR decisions, historical runtime provenance и current security baseline
остаются открыты. Никаких APPROVED записей или публичного binary release нет.

Исправлена ошибочная ссылка CP-003: Linux evidence принадлежит HIM-043,
HIM-044 — Windows Docker — BLOCKED. Canonical ToDo по-прежнему106tasks,
5DONE, всеG0–G5OPEN. Разработка продолжается; ничего не переименовано в готовый
выпуск ради прохождения gate. Следом: frozen rollback/crash-resume и source Docker.

Подтверждено перед CP-004: frozen bad-health update → FAILED_ROLLED_BACK;
фактический HTTP сайт v3.0.0/SQLite3 ready=true и user file сохранены.


## CP-20261025-005 — Docker source controller и надёжность recovery

CP-004 подтверждён локально/GitHub: 9e0cb1d1ede5c288fa9c43f4048d4d42194dc924.
Добавлен DockerCLI/contract/controller для start/stop/backup подготовленных owned
containers. Проверены exact CLI bytes/argv/endpoint, container/network ownership,
quiesced encrypted snapshot и durable intent/outcome reconciliation. 163 scoped
PASS+10subtests; daemon моделируемый. Полного provisioning/update/restore нет.

Реальный frozen repair(v3) и late rollback(v3→v2) PASS. До замены сохранён AES
snapshot со SQLite3; после восстановления actual site/DB сообщают v2/schema2.
Выявленный затем PLAN_STALE при stop новым bootstrap привёл к исправлению:
start/stop/backup/uninstall используют verified active manifest/runtime/hooks/refs,
reviewed effective_active повторно сверяется под lifecycle lock. Старую попытку
закрыли без утверждения undo; новый GUI прошёл stop(de)/start(en)/backup(ru)/stop(en).
Архив принадлежит active v2. Секреты старого архива не подменяются новыми;
недоступный provider отклоняется до stop. Добавлены реальные regression cases.

Независимый review обнаружил delayed owner check race и потерю GUI polling после
cancel concurrent resume. Добавлены snapshot CAS, same-owner event-sequence guard,
local-worker finalization guard и продолжение UI polling. Три существенных source
interleavings и отдельный browser response-contract PASS. Последний использует
моделируемые ответы, реальные product operations он не меняет.

Frozen SIGKILL на stage→новый process/browser→explicit resume той же операции
принят в scoped receipts. Отдельный повтор сначала увидел RUNNING на первом
мгновенном чтении после kill; позже owner отсутствовал. Неуспех сохранён, его
причина не установлена по одному позднему наблюдению. Harness теперь ограниченно
ожидает recovery, не запускает другую install и не выполняет автоматический replay.

SDK wheel отдельно установлен вне checkout в чистую среду: схемы, контракты,
документация, ru/en/de, console entrypoints, DRAFT scaffold и отсутствие сети
проверены. Точные source/wheel bytes и все последние counts/timings — в receipts
packaging/acceptance-evidence; номера разных scope не складываются.

GitHub CI CP-004 действительно запускался: Linux/Windows jobs завершились failure
при runner_id=0, steps=[]. Это не выполненные source tests. Логи не предоставлены,
точная причина через доступный API неизвестна. CI остаётся BLOCKED. Production
licence/signing/EULA gates, OS services, Windows и полный Docker также открыты.

Обновлены канонический ToDo, RESUME/state и README. 106 карточек: 5 DONE,
59 IN_PROGRESS, 38 TODO, 4 BLOCKED; G0–G5 OPEN. Нет публичного binary release
или merge в main. Синхронизация CP-005 считается успешной только после независимой
проверки GitHub ref/commit/tree; Git владельца в Docker отдельно SYNC_BLOCKED.


Финальная проверка CP-005 после исправлений CAS и Linux owner liveness:
**1637 source PASS, 1 skip, 10 subtests** (40.50s); отдельно **47 integration
PASS, 5 subtests** (41.57s). В последние входят 12 liveness cases: настоящий
собственный unreaped zombie распознаётся завершённым, permission/read failures
остаются unknown/alive, API отмечает recovery без engine effects. Причина старого
первого frozen FAIL этим не считается доказанной. Установленный SDK и актуальные
package resources повторно проверены по точным bytes.

Финальный frozen bundle после owner fix повторно прошёл реальный SIGKILL на stage:
новый controller/browser сразу увидел RECOVERY_REQUIRED, явный Resume завершил
ту же op_e4a33118c10642058bfc85447bee5581, actual v1/SQLite1 ready=true.
Продукт остановлен, собственные тестовые controller processes завершены; failed
fixtures сохранены. Точный executable/inventory hash — CP-005 frozen receipt.
