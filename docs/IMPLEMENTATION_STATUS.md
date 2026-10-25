# Фактическое состояние реализации

Дата: 25.10.2026. Checkpoint: **CP-20261025-014**.
Ветка `codex/universal-installer-20261025`. **`production_ready=false`.**
Исходный commit: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.



## Последний выполненный шаг

CP-20261025-014: В capabilities добавлена проверяемая проекция шести полей требований версии/обязательности/scope. Данные берутся из повторно проверенного подписанного каталога, без discovery, effects, путей или команд. Сохранённый plan v1, digest и consent refs не меняются; старая queued операция восстанавливается прежним контрактом. OpenAPI и встроенные API документы синхронизированы.

55 целевых source/API/HTTP проверок PASS, 0 skips (5.933 s), Python 3.14.3; 12 новых случаев входят в эти 55. Root сравнил все семь интегрированных файлов с проверенным commit 2c6d3c9 и receipt-хешами. Первый harness-прогон 9 PASS/3 FAIL сохранён: исправлено ошибочное ожидание отсутствующего store после trusted constructor. Новый полный source suite/installed wheel/browser/OS acceptance NOT RUN; source1961 PASS CP-013 остаётся историческим результатом других bytes. GUI ещё в разработке.

## Исторический шаг CP-20261025-013

CP-20261025-013: Восстановлена неотправленная локальная CP-012 и подтверждены GitHub ref/tree. Независимый повтор выявил устаревший снимок в тесте отмены: durable журнал уже содержал CANCELED. Дополнительно исправлена преждевременная проекция execution.active=False при финальном observer. Пока worker удерживает reservation — active=True; занятый/неизвестный внешний controller — active=null. Тест заново читает журнал после освобождения потока, не ослабляя требование CANCELED.

Текущие source bytes: 1961 PASS, 3 skips, 10 subtests (78.82 s), Python 3.14.3. Два пропущенных Linux process/D-Bus случая и три настоящих loopback HTTP случая отдельно: 5 PASS (2.61 s), JUnit receipt. Windows-only case NOT RUN. Пять детерминированных новых regressions FAIL на CP-012 и PASS после исправления; focused141 PASS (20.88 s), области пересекаются. Первоначальный recovery-прогон 1955 PASS/1 FAIL/3 skips сохранён. Новый installed/frozen/browser/full platform acceptance NOT RUN; Chromium --version вне sandbox также exit139, причина не установлена.

## Исторический шаг CP-20261025-012

CP-20261025-012: Подключены HTTP routes и синхронный CLI к тому же доверенно зарегистрированному prerequisite coordinator. HTTP сравнивает полный signed payload, хранит планы на сервере, требует явные согласия и выдаёт durable background ACK. Status/events/cancel отвечают при медленных probes/installers; повтор start не становится resume. Подготовка и READY сбрасывают product review. Общий read-only busy guard видит claim старого каталога без раскрытия чужого ID. Исправлена реальная гонка отмены при sync recheck: принятая отмена не теряется при финальном сохранении, неизвестный installer сохраняет receipt/claim, отмена перед intent не запускает installer.

Текущий root full source на Python 3.14.3: 1956 PASS, 3 skips, 10 subtests (84.26 s); два Linux skip отдельно выполнены с matching PID namespace: 2 PASS (0.12 s), Windows-only case NOT RUN. HTTP focused 141 PASS и настоящий loopback 3 PASS; CLI focused 69 PASS; независимый core/HTTP review 12+8 PASS, области пересекаются. Installed SDK planning: 63 exact files и 7 CLI calls PASS, wheel 5c327073061674dec7917915ce07d4ceea5eb9e3e2685e9c3867d3b39bb197e1. Ранее выявленные FAIL/EPERM сохранены в истории. GUI flow, durable byte events и atomic cross-executor lease ещё NOT IMPLEMENTED; registered installed prerequisite lifecycle, live OS/Windows/Docker/browser/frozen acceptance NOT RUN. Browser BLOCKED: официальный альтернативный архив проверен, запуск SIGSEGV с неизвестной причиной. CI CP-011: 4 jobs без runner/steps, тесты NOT RUN.

## Исторические проверки CP-008

В общий checkout интегрированы Docker install/uninstall, systemd user platform
port с защитой границы native/user и prerequisite coordinator через общий SDK.
Проверки выполнены на Linux x64 / Python 3.12.14:

| Область | Результат | Что подтверждает |
|---|---|---|
| Полный root source suite | **1856 PASS, 1 skip, 10 subtests**, 70.24 s | Интегрированные исходники и контракты; моделируемые Docker/service ответы не являются live acceptance |
| Отдельные Linux integration tests | **49 PASS, 5 subtests**, 43.23 s | Собственные реальные процессы, loopback HTTP и синтетические данные в согласованном PID/proc namespace |
| SDK wheel вне checkout | **44 Python files, 18 resources**, 5 CLI вызовов, 0 network attempts | Совпадение package bytes, импорт новых модулей и офлайн-контракты; lifecycle установленного продукта этим не проверяется |

Это разные области проверки; их counts не складываются. Точные команды и scope
полного прогона — в [source receipt CP-008](../packaging/acceptance-evidence/source-20261025-cp008.json)
и [WORK_LOG.md](WORK_LOG.md). Независимый review и отдельные regressions записаны
в [review receipt CP-008](../packaging/acceptance-evidence/review-20261025-cp008.json).
[Installed SDK receipt CP-008](../packaging/acceptance-evidence/sdk-installed-20261025-cp008.json)
содержит wheel SHA-256
`c4d862c6c0ea9c49b9126e0cae630fd70ef1b172e285d2f508606a68b6189268`,
хеши 44 Python files и 18 resources; новые state-файлы при этой проверке не создавались.
Исторические frozen/SDK результаты ниже относятся к своим прежним artifacts.
Новая frozen CP-008 сборка и полная platform acceptance этими прогонами не приняты.

## Что сохранено и как используется

| Область исходного ядра | Решение | Текущее применение и граница |
|---|---|---|
| `shared/manifest.py`, schema v1/v2 | Сохранить, интегрировать | Строгая исходная проверка перед нормализацией в публичном API; v1 не превращается молча в готовый v2 runtime. |
| `shared/datahome.py` | Сохранить, исправить совместимость | Владение путями, запрет опасных ссылок; совместимая проверка Windows reserved names на Python 3.12/3.14. |
| `shared/update_plan.py` | Сохранить, интегрировать | Последовательность update/rollback; новый фасад дополняет durable recovery и согласиями. |
| `shared/adapters_fs.py` | Сохранить, интегрировать | Проверка полного дерева и staging; NativeEngine связывает с реальным процессом и health. |
| `shared/archive.py`, `archive_crypto.py` | Сохранить, интегрировать | Проверяемые полные архивы, AES-GCM/scrypt; пароль через отдельный provider. |
| `shared/adapters_docker.py` | Сохранить legacy Compose интерфейс | Общий API использует отдельный DockerEngine через BackendRouter; legacy adapter не получает его гарантии автоматически. |
| `shared/services.py`, Linux/Windows generators | Сохранить contracts и generators | Добавлен исполняемый SystemdUserManager; native/service orchestration и Windows SCM ещё не подключены. |
| `shared/ctl.py`, doctor | Сохранить совместимость | Новый `hinstall` — публичный фасад; прежние команды остаются доступными. |
| Пакет и discovery | Добавить | `pyproject.toml`, SDK, CLI, schemas, OpenAPI, `INSTALLER_API.md`; проверка установленного wheel вне checkout. |
| Подпись поставки | Добавить | Ed25519 + SHA-256, manifest/target/runtime binding; production trust root ещё не выпущен. |
| Диагностика и три языка | Добавить | SQLite + ограниченный fallback, 43 codes, ru/en/de, local statistics/export. |
| GUI и end-user bootstrap | Реализован DEV Linux профиль | ru/en/de; frozen install/backup/uninstall/restore и browser export проверены на исторических CP-004/005 artifacts; остальные платформы и новая frozen сборка открыты. |

## Проверяемое покрытие

Доступен Linux x64 в изолированной облачной среде. Для исходных тестов
в CP-006/007/008 используется Python 3.12.14; исторический CP-005 — Python 3.14.3.
Это не компьютер владельца и не его Docker. Обычная sandbox shell имеет
несовпадающие PID/proc namespaces; положительные process tests выполнялись
отдельно там, где это соответствие проверено.

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
Начальные targets по [TARGET_MATRIX.md](TARGET_MATRIX.md): **Windows 11 x64** и
**Ubuntu 24.04 LTS x86_64 с glibc**, для каждой ОС native/Docker и ru/en/de.
Публичный начальный scope — `user`. Windows Server, другие дистрибутивы,
ARM64, musl и macOS не входят в первую матрицу. Наличие target в плане разработки
не означает принятую поддержку. Linux headless должен использовать CLI или
безопасный доступ к мастеру по SSH; открывать мастер на внешнем интерфейсе нельзя.

## Интегрированные platform ports CP-008

### Docker install/uninstall

`DockerEngine` через BackendRouter выполняет fresh install, start/stop, health,
encrypted backup и uninstall с сохранением данных. Install создаёт только
собственные network/container по reviewed plan; полный signed payload закреплён
в плане и ownership receipt. Перед каждым effect сохраняется intent; неизвестный
RPC требует сверки фактического ресурса и explicit resume, без слепого повтора.
Verified payload, inventory и signature сохраняются до первого create RPC.
Если исходный cache исчез, resume может использовать повторно проверенные owned
recovery bytes. Uninstall проверяет foreign network/namespace consumers до stop
и сохраняет DataHome, images и recovery metadata.

Исполняемый профиль ограничен локальным rootful Linux daemon в тех же
mount/network namespaces, проверенным endpoint и совпадающим non-root UID:GID
установщика/контейнера. Install/start/backup требуют Engine >= 28.0.0. Проверка
host identity через `/proc` остаётся реальным условием допуска: доступ к Docker
socket или членство в docker group не гарантируют чтение root-owned dockerd.
При недоступной проверке backend отказывает; trusted host adapter и live
проверка обычного пользователя ещё требуются.

Каждый image должен уже присутствовать с полным RepoDigest и image ID;
используется `--pull=never`. Получение/import образов и полный offline image
bundle не реализованы; образы не входят в recovery/data archive. Update,
restore, rollback, repair, reinstall, migration/product-secret hooks и отдельная
compensation failed install ещё не поддерживаются. Автозапуск после reboot
не предоставляется (`restart=no`). Windows Desktop/WSL, VM, remote и rootless
требуют отдельных adapters. Live Docker acceptance в текущей среде BLOCKED.
Контракт: [DOCKER_ENGINE.md](DOCKER_ENGINE.md), проверки и ограничения:
[DOCKER_PROVISIONING.md](DOCKER_PROVISIONING.md).

### Systemd user port и граница native/user

`SystemdUserManager` и доверенный runner реализуют register, enable/disable,
start/stop/status/uninstall принадлежащих установке units. Проверяются весь
installed signed release, effective unit/argv/environment, PID/cgroup и
отсутствие чужих overrides. Port использует общий root lifecycle lock,
durable pending/done receipts и explicit resume. Неизвестный stop или живой
writer не дают разрешения на snapshot. DataHome, releases и recovery сохраняются.

Native/user дополнительно отказывает при зарегистрированных units, pending
операции или недостоверном service journal; параллельного управления данными
двумя execution scopes не допускается. Это integration guard, а общий
`native/service` backend ещё unavailable. Встраивание port в NativeEngine требует
разделения существующего lock lease и полного lifecycle orchestration.
Windows WinSW остаётся signed renderer, SCM adapter отсутствует. Настоящие
systemd/product lifecycle, restart policy, logout/login, reboot/autostart и ACL
не приняты. Private D-Bus и моделируемый manager не заменяют эти проверки.
Подробности: [SERVICE_MANAGER.md](SERVICE_MANAGER.md).

### Prerequisites через явную SDK registration

`SignedPrerequisiteCatalog` и `PrerequisiteCoordinator` используют существующие
trust/download primitives: полный payload binding, exact artifact pins и
licence text, dependency order, отдельные согласия и fixed adapters без shell.
Есть auto/manual/use/skip в допустимых границах, ручной recheck без download/exec,
история попыток, сохранённые reboot/relogin states, now/later и explicit resume.
Новый PID или повторное окно не заменяют смену boot/session marker; неизвестный
исход не разрешает повтор installer.

`InstallerAPI(prerequisites=coordinator)` делегирует тот же coordinator без
второго executor. Регистрация приходит только от trusted bootstrap; production
registry с настоящими версиями, hashes, условиями и publisher policy автоматически
не создаётся. `READY` означает готовность prerequisites; перед product install
нужны fresh discover и новый reviewed product plan. SDK facade готов в source;
GUI auto-flow, неблокирующая host integration, Windows elevation/Authenticode/
ACL/session providers, реальные OS package installs и reboot/relogin acceptance
остаются открытыми. Эти методы не превращены в произвольный CLI/HTTP installer.
Подробности: [PREREQUISITES.md](PREREQUISITES.md) и
[INSTALLER_API.md](../INSTALLER_API.md).

## Что требуется для выпуска

1. Принять reference через GUI во всех четырёх сочетаниях ОС/режима и трёх языках.
2. Интегрировать systemd port в общий service lifecycle, реализовать Windows SCM
   и принять реальные stop/restart/logout/reboot/autostart сценарии.
3. Довести Docker host attestation, получение/offline transport images и оставшиеся
   lifecycle actions; принять их на настоящем daemon и соответствующих Windows adapters.
4. Завершить Windows native bootstrap, production prerequisite catalogue и GUI
   auto-flow; принять elevation, ручной путь и настоящий reboot/relogin.
5. Подключить и принять реальные JobPilot/Rent, отдельные миграции и полное
   восстановление. [Аудит исходников](PRODUCT_AUDIT_20261025.md) закрывает HIM-006,
   но не product acceptance; JobPilot build2 rollback FAIL остаётся открытым.
6. Закрепить полный dependency inventory, лицензии и сборки всех платформ,
   обязательный production licence checker, publisher key/certificate и юридические тексты.
7. Диагностировать CI: [наблюдавшиеся runs CP-007](../packaging/acceptance-evidence/github-ci-20261025-cp007.json)
   завершились до выделения runner/steps, source tests там не исполнялись;
   точная причина доступным API не установлена.
8. Подключить Git владельца в Docker и подтвердить оба remotes. Все G0–G5 остаются OPEN.

Нельзя обещать абсолютную защиту от вирусов или криптографически доказать
безопасность программы одной контрольной суммой. Проверка подписи устанавливает
происхождение и целостность относительно закреплённого ключа. CRC для этой цели
не используется. Установщик не должен генерировать новый доверенный publisher key
при каждой установке или принимать ключ из неподписанного скачивания.


## Исторические проверки CP-004

Этот и следующие исторические разделы описывают прежние исходники и artifacts.
Их counts, hashes и receipts сохранены отдельно; они не являются новой
проверкой CP-008 или доказательством live Docker/systemd/Windows acceptance.

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


## Исторические доказательства CP-005 после CP-004

- Настоящий frozen controller был SIGKILL во время stage. Новый process и новый
  browser context нашли прежний operation_id в RECOVERY_REQUIRED; автоматического
  replay не было. Нажатие Resume завершило ту же операцию, SQLite/HTTPready PASS.
- Frozen repair(v3) и явный late rollback(v3→v2) через GUI PASS. Отдельно проверен
  зашифрованный snapshot перед поздним откатом: внутри SQLite schema3, после
  восстановления рабочая БД schema2. Это доказательство сохранения состояния
  перед заменой, а не только проверки файлового расширения архива.
- На CP-005 DockerCLI/contract/controller был реализован для start/stop/backup уже подготовленных
  owned containers. Scoped163PASS+10subtests: реальные crypto/files/syntheticELF,
  моделируемый daemon. Provisioning/update/restore на этом checkpoint ещё не были
  реализованы, реального Docker acceptance не было. Текущая реализация описана выше.
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

Точные исторические test counts, artifact hashes, успешные сценарии и сохранённые
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


## Историческое дополнение CP-007

BackendRouter связывает общий API/CLI/GUI с явными native/Docker backend; отдельные
signed payload/terms выбранного режима и симметричный ownership guard проверены.
Full source 1659 PASS, real Linux integration 47 PASS; последний focused 65 PASS.
Точные scopes/subtests/bytes — CP-007 receipts. Installed wheel проверен вне
checkout: 36 Python files, 18 resources, offline CLI без сети. На этом checkpoint
Docker provisioning, systemd port и prerequisites ещё разрабатывались отдельно.
Они интегрированы в CP-008 в пределах, описанных выше; live gates остаются открытыми.
