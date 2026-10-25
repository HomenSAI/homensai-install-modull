# Продолжение разработки

Checkpoint: `CP-20261025-005`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый GitHub/local до создания CP-005:
`9e0cb1d1ede5c288fa9c43f4048d4d42194dc924` (CP-004).
SHA текущего checkpoint проверяется по Git history/ref и внешней sync receipt.
Не создавать рекурсивный commit ради записи собственного SHA.

## Восстановление контекста

Прочитать CLAUDE.md, полный PROMPT, канонический ToDo, docs/work-state.json,
IMPLEMENTATION_STATUS.md, WORK_LOG.md и INSTALLER_API.md. Проверить branch,
dirty/staged files и активные процессы. Не сбрасывать чужие изменения и не
завершать произвольный PID. Частичный PASS не означает готовность выпуска.

## Среда и сохранение

Облачный Linux x64; isolated build/test Python 3.14.3, host Python 3.12.
Это не Windows владельца и не его Docker/Forgejo. Cloud Git не заменяет локальный
Git владельца. GitHub доступен через connector; shell HTTPS credentials отсутствуют.
Канонические commit/tree/parent/ref проверяются после каждого push. Транспорт
connector сохраняет draft checkpoint ref и затем импортирует точный remote commit.
В обычной среде используйте tools/checkpoint.py и docs/CHECKPOINTS.md.

## Что не нужно выполнять заново без изменения соответствующего кода

- Source suite и реальные Native/API/HTTP integration прошли; точные последние
  counts, scope и timings — packaging/acceptance-evidence/source-20261025-cp005.json.
- Frozen Linux user-session: install/backup/uninstall-preserve/restore/stop;
  v1→v2→v3; bad-health update→FAILED_ROLLED_BACK; repair; late rollback→v2.
  Before-rollback архив действительно содержит SQLite schema3, live после rollback — schema2.
- Новый bootstrap обслуживает проверенную старую active release: stop(de),
  start(en), backup(ru), stop(en). План раскрывает effective_active и повторно
  сверяется под lifecycle lock. Incoming runtime/commands/secret refs не подменяют active.
- Restore/late rollback сохраняет архивные credential refs; отсутствующий старый
  secret блокирует операцию до остановки текущего продукта.
- Inactive cancel освобождает API claim без утверждения undo. Задержанный owner
  check использует snapshot CAS и не перезаписывает новый resumed worker.
  При RUNNING после cancel GUI продолжает polling; отдельный browser contract PASS.
- Linux liveness: actual owned unreaped zombie больше не считается живым;
  read/permission failures остаются консервативными. 12 новых cases входят
  в отдельные integration tests, запущенные в совпадающем PID/proc namespace.
- Реальный frozen SIGKILL→new process/browser→explicit resume принят в указанных
  receipts. Исторический FAIL немедленного наблюдения RUNNING также сохранён.
  Process kill не равен отключению питания или reboot.
- SDK wheel установлен вне checkout: 9 групп, 35 Python files/18 ресурсов,
  ru/en/de, offline schemas/OpenAPI/docs, DRAFT scaffold exit2, 0 socket attempts.
  Точный SHA — sdk-installed-20261025.json; lifecycle этим не доказывается.
- DockerCLI/contract/controller: start/stop/encrypted backup подготовленных owned
  containers; 163 scoped tests +10subtests. Daemon моделируемый, live Docker не было.
- Exact build lock, licence checker, ProductAdapter/scaffold и checkpoint tooling
  сохранены. Gates лицензий 04/05 BLOCKED, production_ready=false.

## Следующий инженерный этап

1. Проверить фактический SHA CP-005 и оба доступных Git refs; Docker Git владельца
   подключать отдельно по реальной схеме доступа, не считать cloud loopback его хостом.
2. Диагностировать GitHub CI: CP-004 run40170554823 завершился failure до выделения
   runner/steps. Это не падение source tests. Доступный API не раскрывает причину.
3. Реализовать регистрацию/жизненный цикл системных служб Linux/Windows и принять
   reboot/autostart/restart. Генераторы unit/WinSW config уже есть, принятия служб нет.
4. Довести Docker provisioning, pinned images/Compose, install/update/restore/
   rollback/uninstall, явный backend selection; принять на настоящем daemon.
5. Завершить Windows native bootstrap, UAC/ACL, полный prerequisites auto-install,
   ручной/recheck путь, relogin/reboot и обе Windows ветки.
6. Принять clean-host/cross-target recovery и реальные JobPilot/Rent по свежим
   исходникам. Общий модуль не должен содержать особые ветки конкретных продуктов.
7. Закрыть точный runtime/frozen licence inventory/notices, current security baseline,
   production publisher keys/certificates и утверждённые юридические тексты.

Это инженерные работы и отдельные внешние блокировки; не называть весь остаток
только отсутствием доступа. 106 задач: все критерии проверять по карточкам;
Windows/Docker/system services и все G0–G5 не закрыты.

## Сохранённые ограничения и расследования

В прежних workspace large-bundle fixtures обнаружены лишние временные файлы с
неустановленным источником; один update остановился на backup:0. Failed fixtures
сохранены. Не удалять неизвестные файлы и не ослаблять inventory ради PASS.
Последовательные проверки в новом /tmp прошли; это не устанавливает старую причину.
Временные /tmp fixtures не заменяют переносимые receipts/commands в Git.

Private publisher key, архивные пароли, product secrets, launch URL tokens не
добавлять в repo/logs. Не открывать мастер на внешнем интерфейсе. Main/release
не публиковать без соответствующего разрешения. Продолжать независимую работу,
если заблокирован отдельный runner/remote; не объявлять код готовым выпуском.
