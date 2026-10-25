# Фактическое состояние реализации

Дата: 25.10.2026. Ветка `codex/universal-installer-20261025`.
Исходный commit: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.

## Что сохранено и как используется

| Область исходного ядра | Решение | Текущее применение и граница |
|---|---|---|
| `shared/manifest.py`, schema v1/v2 | Сохранить, интегрировать | Строгая исходная проверка перед нормализацией в публичном API; v1 не превращается молча в готовый v2 runtime. |
| `shared/datahome.py` | Сохранить, исправить совместимость | Владение путями, запрет опасных ссылок; совместимая проверка Windows reserved names на Python 3.12/3.14. |
| `shared/update_plan.py` | Сохранить, интегрировать | Последовательность update/rollback; новый фасад дополняет durable recovery и согласиями. |
| `shared/adapters_fs.py` | Сохранить, интегрировать | Проверка полного дерева и staging; NativeEngine связывает с реальным процессом и health. |
| `shared/archive.py`, `archive_crypto.py` | Сохранить, интегрировать | Проверяемые полные архивы, AES-GCM/scrypt; пароль через отдельный provider. |
| `shared/adapters_docker.py` | Сохранить; требуется подключение | Существующая реализация не выдаётся за принятый Docker backend нового API. |
| `shared/services.py`, Linux/Windows generators | Сохранить; требуется подключение | Генерация service scripts сама по себе не доказывает зарегистрированную службу. |
| `shared/ctl.py`, doctor | Сохранить совместимость | Новый `hinstall` — публичный фасад; прежние команды остаются доступными. |
| Пакет и discovery | Добавить | `pyproject.toml`, SDK, CLI, schemas, OpenAPI, `INSTALLER_API.md`; проверка установленного wheel вне checkout. |
| Подпись поставки | Добавить | Ed25519 + SHA-256, manifest/target/runtime binding; production trust root ещё не выпущен. |
| Диагностика и три языка | Добавить | SQLite + ограниченный fallback, 43 codes, ru/en/de, local statistics/export. |
| GUI и end-user bootstrap | Реализован DEV Linux профиль | ru/en/de; frozen install/backup/uninstall/restore и browser export проверены; остальные платформы открыты. |

## Проверяемое покрытие

Доступен Linux x64 в изолированной облачной среде. Для исходных тестов
в CP-006/007 используется Python 3.12.14; исторический CP-005 — Python 3.14.3. Это не компьютер владельца и не его Docker.

NativeEngine реализует Linux **user-session**: установку, запуск/остановку,
readiness по идентичности продукта/установки/релиза, update и автоматический
rollback, backup/restore, repair и сохранное uninstall. Реальные процессы и
HTTP используются только с синтетическими данными на loopback.

Проверяются обрывы после migration и переключения release, повтор старта и health,
cancel/resume, неоднозначное завершение migration, владение PID и отказ до
изменения чужого каталога. Источником истины для восстановления является журнал
операции; DiagnosticStore — необязательная диагностическая проекция.

Windows native, Docker на обеих ОС и службы уровня ОС ещё не приняты. API
возвращает capability ограничения; результат unit tests не заменяет эти проверки.
Начальные targets: Windows 11/Server с совместимым runtime и Linux Debian/Ubuntu
с glibc; точные версии и архитектуры разрешаются только после отдельной сборки
и проверки. ARM64 не объявлен принятым. Linux headless должен использовать CLI
или безопасный доступ к мастеру по SSH; открывать мастер на внешнем интерфейсе нельзя.

## Что требуется для выпуска

1. Принять reference через GUI во всех четырёх сочетаниях ОС/режима и трёх языках.
2. Подключить/проверить Windows service и Docker backend, prerequisites и reboot.
3. Принять реальные JobPilot/Rent, отдельные миграции и полное восстановление.
4. Закрепить полный dependency inventory, лицензии и сборки всех платформ.
5. Предоставить production publisher key/certificate и утверждённые юридические тексты.
6. Подключить Git владельца в Docker и подтвердить оба remotes.

Нельзя обещать абсолютную защиту от вирусов или криптографически доказать
безопасность программы одной контрольной суммой. Проверка подписи устанавливает
происхождение и целостность относительно закреплённого ключа. CRC для этой цели
не используется. Установщик не должен генерировать новый доверенный publisher key
при каждой установке или принимать ключ из неподписанного скачивания.


## Дополнение CP-004: выполненные проверки

Полный source suite на Python 3.14.3: **1493 passed, 1 skipped, 10 subtests**, 35.41 s.
Предшествовавший прогон выявил два пропуска диагностической проекции при пустой
версии продукта; причина исправлена и общий прогон повторён успешно.
Отдельные Linux NativeEngine/API/HTTP integration: **26 PASS**; GUI/trust/prerequisite
целевые проверки: **53 PASS**. Это разные области проверки, их числа не складываются.

Настоящий frozen Linux x64 bundle05: 896 подписанных payload files,
install (ru), encrypted backup (en), uninstall (de) с сохранением пользовательского
файла, restore (ru), реальный HTTP/SQLite readiness, errors/search/statistics,
preview/confirmed support export, stop (en) — PASS. Ошибок JavaScript не было.
Browser screenshots просмотрены. DEV bootstrap включает runtime; host Python
не используется установленным reference продуктом.

Reference v1/v2/v3 имеет настоящие SQLite migrations, проверяемые данные,
настройки, optional test secret и контролируемые migration/health failures.
ProductAdapter protocol и scaffold проверены: signed descriptor, fixed argv,
bounded I/O, timeout, durable effect receipts и запрет слепого повторения.
Executable adapter пока не заменяет автоматически legacy hooks NativeEngine.

Build lock закрепляет 17 версий и 22 SHA-256 wheels для Linux/Windows x64;
Linux установленный набор совпадает. Добавлены CI source checks с закреплёнными
GitHub Actions. Факт создания workflow не означает выполненную CI-проверку;
результат конкретного run должен быть записан отдельно.

Production лицензии остаются BLOCKED: удалены ненужные BDB/Tcl/ensurepip, но
встроенные runtime/frozen зависимости и обязательные notices требуют закрытия
по точным bytes. Development publisher key, черновик EULA и непринятые платформы
также не допускают production release. Исторические integrity failures сохранены
в журнале; неизвестные файлы не исключаются из проверки ради успешного теста.

Подтверждено перед CP-004: frozen bad-health update → FAILED_ROLLED_BACK;
фактический HTTP сайт v3.0.0/SQLite3 ready=true и user file сохранены.


## Доказательства, добавленные после CP-004

- Настоящий frozen controller был SIGKILL во время stage. Новый process и новый
  browser context нашли прежний operation_id в RECOVERY_REQUIRED; автоматического
  replay не было. Нажатие Resume завершило ту же операцию, SQLite/HTTPready PASS.
- Frozen repair(v3) и явный late rollback(v3→v2) через GUI PASS. Отдельно проверен
  зашифрованный snapshot перед поздним откатом: внутри SQLite schema3, после
  восстановления рабочая БД schema2. Это доказательство сохранения состояния
  перед заменой, а не только проверки файлового расширения архива.
- DockerCLI/contract/controller реализован для start/stop/backup уже подготовленных
  owned containers. Scoped163PASS+10subtests: реальные crypto/files/syntheticELF,
  моделируемый daemon. Полный provisioning/update/restore не реализован, реального
  Docker acceptance нет. См. DOCKER_ENGINE.md.
- GitHub CI наCP004 создал Linux/Windows jobs и завершился failure до runner/steps.
  Проверки там не исполнялись; scopeCI BLOCKED. Причина в доступном ответе неизвестна.

Выявленный stop после late rollback исправлен: обслуживание привязано к
подписанному активному выпуску. Старую неактивную попытку закрыли через новый GUI;
новые reviewed stop(de)/start(en)/backup(ru)/stop(en) завершились успешно для v2,
хотя был открыт bootstrap v3. Реальный сайт сообщил v2.0.0/SQLite2 ready=true;
архив принадлежит тому же выпуску. Screenshots ru/en/de сохранены и просмотрены.

Restore/late rollback использует архивные credential refs без incoming overlay.
Девять новых реальных integration cases проверяют maintenance и archived secrets;
отсутствующий старый секрет не останавливает и не меняет текущую версию.

Независимый review выявил гонку delayed status→concurrent resume→cancel. Snapshot
CAS под SQLite write transaction сохраняет новую попытку и resource claim; same-owner
resume также защищён monotonic event sequence. GUI при RUNNING продолжает polling.
Source interleavings и отдельный browser response-contract проверены; последний
использует воспроизведённые ответы, а не настоящий конкурентный backend.

Итоговый core SDK установлен в clean venv вне checkout: 9 групп, 35 Python files,
18 packaged resources, 17 guarded CLI invocations, 0 socket attempts; state не
создан. Wheel/исходный snapshot и точные границы находятся в sdk-installed receipt.

Точные последние test counts, artifact hashes, успешные сценарии и сохранённые
исторические FAIL: packaging/acceptance-evidence/source-20261025-cp005.json,
linux-frozen-20261025-cp005.json и sdk-installed-20261025.json. Отдельный final
crash-run сначала увидел RUNNING немедленно после SIGKILL; позже owner уже отсутствовал.
Этот FAIL сохранён. Проверка должна дождаться подтверждённого RECOVERY_REQUIRED,
не выполнять скрытый resume и не подменять отсутствие готовности успехом.


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


## Дополнение CP-007

BackendRouter связывает общий API/CLI/GUI с явными native/Docker backend; отдельные
signed payload/terms выбранного режима и симметричный ownership guard проверены.
Full source 1659 PASS, real Linux integration 47 PASS; последний focused 65 PASS.
Точные scopes/subtests/bytes — CP-007 receipts. Installed wheel проверен вне
checkout: 36 Python files, 18 resources, offline CLI без сети. Docker provisioning,
OS services и prerequisites пока разрабатываются отдельно; live gates открыты.
