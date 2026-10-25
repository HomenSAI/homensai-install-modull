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
используется отдельный Python 3.14.3. Это не компьютер владельца и не его Docker.

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
