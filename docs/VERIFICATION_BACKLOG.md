# Невыполненные проверки — обязательное задание следующему ИИ

Автор: Serhii Khomenko. CP-20261025-012.

**Полная приёмка НЕ ГОТОВА; production_ready=false.** Этот документ и
`verification-status.json` описывают проверки. Единственный список реализации —
`HomenSAI_Universal_Installer_TODO_RU.txt`; его ID/критерии не заменяются этим реестром.

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

## Конкретные незавершённые механизмы CP-012

Наличие прошедших transport/source тестов не закрывает эти требования.
Каждый пункт также записан в `verification-status.json` → `implementation_gaps`.

| Механизм | Реализация / тест | Что должен сделать следующий исполнитель |
|---|---|---|
| Общая атомарная блокировка product/prerequisite executors | NOT IMPLEMENTED / NOT RUN | Реализовать общий durable lease; проверить параллельные независимые процессы, обрыв, неизвестный эффект и безопасное освобождение. `busy()` проверяет состояние, но не выдаёт такой lease. |
| GUI auto/manual/terms/progress/recovery | NOT IMPLEMENTED / NOT RUN | Подключить явные действия к HTTP, написать проверки настоящего JS и выполнить browser acceptance. |
| Durable progress скачанных байтов | NOT IMPLEMENTED / NOT RUN | Сохранять измеренные события с порядком/cursor; проверить reconnect/retry/повторы без ложного100%. |
| Выбор локального prerequisite installer через браузер | NOT IMPLEMENTED / NOT RUN | Добавить безопасный явный выбор с проверкой signed pins; произвольные browser paths по-прежнему запрещены. |

Текущая browser acceptance BLOCKED: официальный альтернативный архив получен
и ZIP CRC проверен, но executable и `--version` падают с SIGSEGV; причина неизвестна.
CI CP-011 также BLOCKED: четыре задания без runner/steps, лог недоступен.
Факты и последующие действия: `packaging/acceptance-evidence/environment-20261025-cp012.json`.

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
