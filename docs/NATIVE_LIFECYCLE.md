# Native lifecycle: исполняемый backend и его границы

`homensai_install.engine.NativeEngine` реализует локальную установку подготовленного
release с собственным runtime. В текущей версии исполнение разрешается для Linux,
совпадающей архитектуры хоста и `scope=user`. Это user-session backend:
автозагрузка через systemd/SCM, Windows execution, Docker execution и постоянный
supervisor пока не включены в его capabilities.

## Подключение к общему API

```python
from homensai_install.api import InstallerAPI
from homensai_install.engine import NativeEngine
from homensai_install.trust import load_trust_config

# Публичные ключи задаёт доверенный bootstrap/SDK, а не JSON-запрос клиента.
verifier = load_trust_config("/trusted/bootstrap/public-keys.json")

def secret_provider(reference: str) -> str:
    return protected_store.read(reference)  # Адаптер хранилища конкретного хоста.

engine = NativeEngine(verifier=verifier, secret_provider=secret_provider)
api = InstallerAPI("/private/installer-state", engine=engine)
```

`protected_store` здесь обозначает предоставленный продуктом адаптер секретов,
а не реализованный внутри engine универсальный vault. CLI предоставляет отдельный
явный provider по ссылкам на переменные окружения. SDK может подключить системное
хранилище. Значения паролей не передаются в plan/options и не записываются в
journals. `backup_password_ref` и `secret_refs` содержат только имена ссылок.

`NativeEngine()` без verifier отказывается исполнять новый payload. Параметр
конструктора `development_runtime` существует для source/integration проверок;
это явно внешний интерпретатор и не является готовой автономной поставкой.

Основные options:

| Поле | Назначение |
| --- | --- |
| `install_root` | Абсолютный каталог, которым установщик действительно владеет; чужой непустой каталог не присваивается |
| `payload_dir`, `inventory_file`, `signature_file` | Полный подготовленный payload и внешние подписанные метаданные |
| `release`, `runtime_executable` | Версия и относительный путь runtime, связанные подписью с target и манифестом |
| `modes` | Допустимые режимы из манифеста v2 |
| `eula_locale`, `eula_digest` | `ru/en/de` и SHA-256 соответствующего `legal/<locale>.txt` из подписанного inventory |
| `work_dir` | Собственный recovery-каталог вне install root; по умолчанию соседний каталог |
| `backup_password_ref` | Ссылка на пароль AES-архива в доверенном provider |
| `secret_refs` | Соответствие имени `manifest.secrets` ссылке provider; по умолчанию ссылка равна имени |
| `archive_path` | Явный архив для restore/rollback или место экспорта backup |
| `acknowledge_data_loss` | Обязательное `true` для позднего rollback к выбранному snapshot |

Соглашение считается принятым общим API только после получения обязательного
consent reference. Сам engine дополнительно связывает hash с действительным
подписанным текстом выбранного языка. Произвольный hash от клиента не принимается
за содержимое соглашения.

## Обслуживание после rollback текущим установщиком

`start`, `stop`, `backup`, `uninstall` обслуживают **подписанный активный
установленный выпуск**. Поэтому текущий bootstrap v3 может обслужить v2 после
late rollback, даже если runtime lock/wheel hashes в их манифестах различаются.
Команды, runtime, компоненты, readiness и сохранённые product secret refs берутся
из проверенных metadata активной установки. Новые argv, hooks, runtime path или
`secret_refs`, переданные вместе с другим bootstrap, их не заменяют.

План общего API раскрывает `engine_plan.effective_active`:

- `release` — фактически обслуживаемый выпуск, `source_release` — выпуск
  открытого bootstrap;
- SHA-256 исходного и активного manifest, полного inventory, локальных metadata,
  signed inventory и signature envelope;
- installation ID, target, modes, components, подписанный runtime path и web URL;
- `credential_policy=installed_release_refs` и hash сохранённых ссылок; сами
  значения секретов в план не попадают.

Разрешение активного выпуска выполняется повторно **под lifecycle lock до
эффектов**. Изменение pointer, выпуска или привязанных metadata по отношению к
reviewed plan возвращает `PLAN_STALE`. Изменение подписанных bytes/signature
остаётся `INTEGRITY_FAILED`. На durable границах pointer и metadata сверяются
повторно; при чтении активных metadata проверяется их привязка к плану.

Это не импорт произвольной установки. Source locator обязан точно совпадать с
активной установкой по product ID, `paths`, имени pointer, ОС/архитектуре/backend/
scope и выбранным modes. Изменение этих параметров требует отдельного adapter.
Bootstrap должен доверять ключу издателя активного выпуска. Совпадение имени
продукта само по себе доверия не создаёт.

Для разных manifest необходим новый reviewed plan с `effective_active`.
Старые immutable планы без этой привязки не дополняются догадкой при исполнении.
Совместимость прямых SDK-вызовов без `engine_plan` сохранена только при полном
совпадении source manifest и active manifest. Неактуальный план нужно явно
закрыть и создать новый через API; удалять журналы ради обхода проверки нельзя.

`install`, `update`, `repair`, `reinstall`, `restore`, `rollback` продолжают
использовать свои исходные подписанные source/archive contracts. Выбор active
maintenance manifest не ослабляет проверки нового payload и не подменяет
семантику этих операций.

Повторное чтение завершённой operation receipt возвращает её исторический
результат. Прерванный `uninstall`, который уже сохранил recovery payload, может
продолжить удаление частично удалённого кода: проверяются собственный checkpoint,
полный подписанный recovery payload и каждый оставшийся файл. Дополнительные
или изменённые файлы приводят к отказу; отсутствие кода после частичного удаления
не заставляет исполнять код из нового bootstrap.

## Что должно находиться в release

Builder сначала полностью собирает приложение и runtime для конкретных ОС и
архитектуры. Python wheels должны быть уже установлены в bundled runtime либо
код продукта должен быть импортируем из собственного подписанного каталога.
После сборки builder создаёт полное inventory и подписывает его. Engine не
исполняет `pip install`, не обращается к PyPI и не устанавливает vendor wheels
во время обслуживания. `runtime.lock_files` и `vendor_wheels` проверяются как
pins исходного v2-контракта, а не как доказательство установки зависимостей.

Выпуск размещается в `releases/app-<release>`. Все его файлы проверяются целиком,
включая дополнительные/пропавшие файлы. Ссылки, специальные файлы и конфликты
регистра запрещены. Исполняемое право добавляется только указанному подписанному
runtime и подписанным executable компонентов; setuid не устанавливается.

Перед запуском и продуктовым hook engine повторно проверяет подпись, манифест,
target, release, runtime и полный release inventory. CRC не заменяет SHA-256 и
Ed25519. Проверка подтверждает соответствие доверенному выпуску, но не доказывает
отсутствие ошибок или вредоносного поведения в самом доверенном продукте.

## Процессы и готовность

Компоненты запускаются в порядке зависимостей, останавливаются в обратном
порядке. Команда передаётся как argv без shell. Компонент должен оставаться
управляемым foreground-процессом и корректно завершаться по SIGTERM.
Daemonization и внешние/shared писатели требуют отдельного адаптера.

Process receipt содержит kernel birth identity, installation/release identity
и случайный launch token. PID сам по себе не даёт право остановки. Linux stop
использует pidfd; повторное использование PID не направляет сигнал чужому
процессу. Живой процесс с несовпадающей identity блокирует операцию.

До Popen сохраняется launch intent. После обрыва между запуском и записью PID
engine может принять ровно один процесс с ожидаемыми UID, executable, cwd, argv,
installation/release и launch token. Если однозначного процесса нет, результат
`RECOVERY_REQUIRED`: неизвестный запуск не повторяется автоматически.

Дочернему процессу передаётся ограниченное окружение. `PYTHONPATH`,
`LD_PRELOAD` и произвольные значения build shell не копируются. User site-packages
отключены через `PYTHONNOUSERSITE=1`. Секреты выдаются только declared context:
`admin` для hooks, `service` для компонентов.

Ссылки credentials сохраняются в metadata release. При старте они повторно
используются; automatic rollback получает refs старого release. В рамках одной
операции provider resolution кэшируется только в памяти. Для надёжного recovery
после обрыва vault refs должны указывать на неизменяемую версию секрета; нельзя
незаметно менять значение такого ref во время незавершённой операции.

Готовность требует одновременно живых owned-компонентов и HTTP 200 на loopback
без proxy/redirect. JSON должен содержать правильные `product_id`,
`installation_id`, `release` и `ready=true`. Продукт обязан включать в readiness
действительную проверку своей БД и зависимостей. Дополнительно выполняются
подписанные `hooks.health_checks`. Сам engine пока не мониторит heartbeat-файлы
и не предоставляет постоянный restart supervisor.

Linux backend также сопоставляет inode listening socket из `/proc/net/tcp` с fd
конкретного owned PID. Чужой локальный HTTP-сервер не может подтвердить readiness
одним скопированным JSON. Wildcard bind не соответствует loopback-контракту.
IPv6, внешний reverse proxy и listener только у неучтённого daemon требуют
отдельного адаптера.

## Стратегия backup: `filesystem_quiesced`

Capabilities, plan и результаты явно показывают `backup_strategy=filesystem_quiesced`
и `product_backup_hooks_supported=false`. Legacy `backup.cmd` и `restore.cmd`
из манифеста v2 этим backend не исполняются. Продуктовые проверки могут быть
включены в `hooks.pre_start` / `hooks.health_checks` согласно их контракту.

Перед snapshot engine согласованно останавливает owned writers и копирует все
постоянные корни: data, backup, services, config, runtime. Архив создаётся и
полностью проверяется в потоковом AES-256-GCM формате с scrypt. Snapshot хранит
также подписанный recovery payload, достаточный для восстановления удалённого
кода. Сам архив находится вне архивируемых корней, без рекурсивного включения
себя. Существующий чужой архив не перезаписывается; внешний каталог экспорта
сохраняет исходные permissions.

Эта стратегия допустима только когда действительно остановлены все писатели
данных. Она не является online backup для PostgreSQL/MySQL, shared database
dump или гарантией остановки неучтённых внешних сервисов. Для них нужен отдельный
подписанный adapter с точными dump/output/verify/restore semantics.

Restore принимает только зашифрованные архивы. До изменения живых данных
проверяются authentication tags, внутренний manifest, SHA-256, roots и весь
extracted candidate. После обрыва candidate проверяется повторно. Подписанный
recovery runtime проверяется независимо от шифрования архива. Перед заменой
создаётся отдельный полный `before-restore` snapshot; текущие данные не исчезают
без сохранённой копии.

Product secret refs для `restore` и позднего `rollback` берутся из metadata
аутентифицированного snapshot. Их имена и допустимые contexts проверяются;
provider должен разрешить все необходимые ссылки **до остановки** работающего
продукта. Эти же archived refs передаются при публикации release metadata,
последующем start и health. Incoming refs нового bootstrap не подмешиваются,
включая восстановление snapshot той же версии. Сам архив не заменяет внешний
vault: если старая версия секрета недоступна, операция отказывается менять живую
установку. При неизменяемом ref его значение также должно оставаться неизменным.

## Реальные действия

| Действие | Поведение |
| --- | --- |
| `install` | Отдельный fresh journal, stage, одна продуктовая миграция, pointer, запуск, readiness |
| `start` / `stop` | Idempotent проверка owned процессов; без остановки чужих PID |
| `backup` | Остановка, AES snapshot, возврат исходно работавшего сервера |
| `update` | Существующий `UpdatePlan`: архив, stage, отдельный backup перед миграцией, switch, start, health; automatic rollback при известном failure |
| `restore` | Проверенный архив в уже принадлежащую установщику установку, в том числе после uninstall |
| `rollback` | Явный snapshot и отдельное подтверждение потери изменений после его даты; `before-restore` сохраняется |
| `repair` / `reinstall` | Замена повреждённого кода той же версии из trusted bundle, quarantine старого кода и сохранение пользовательских данных |
| `uninstall` | Остановка, сохранение recovery payload, удаление только известных файлов releases; data/archives/config остаются |
| `status` / `discover` | Read-only installation/process/integrity state и незавершённые операции |

Restore в абсолютно новый root и перенос на другую платформу пока требуют
отдельного import/data adapter. Windows/systemd services не становятся
поддержанными только от наличия сгенерированного XML/unit.

## Обрывы, отмена и безопасный отказ

Lifecycle lock привязан к install root, поэтому два независимых API state_dir
не могут одновременно менять одну установку. Все checkpoints и receipts пишутся
атомарно с fsync. Завершённый plan возвращает свой сохранённый результат.

Cancellation выполняется между durable фазами. Команда миграции или snapshot
не прерывается посередине. `CANCELED` может означать остановленный сервер и
необходимость `resume` исходного плана. Создание нового token не позволяет обойти
незавершённый mutating plan.

При возобновлении fresh после миграции миграция не повторяется; после update
pointer продолжаются start/health, а не возвращается ложное `already_current`.
Состояние процессов и health проверяется заново. Если hook был начат, но его
завершение не успели подтвердить, engine не угадывает его результат и не
повторяет неизвестный side effect: требуется продуктовый recovery.

## Проверка

Unit suite сохраняет запрет всех socket. Реальные subprocess/loopback проверки
находятся отдельно:

```text
python -m pytest tests -q
python -m pytest integration_tests -q
```

Integration suite использует временные синтетические данные: fresh, повторный
start с тем же PID, несколько update, automatic rollback после bad health,
AES backup, uninstall/restore, repair, late rollback, crash/cancel recovery,
orphan adoption, отказ от plaintext/tampered candidates и защита чужих путей/PID.
Отдельный сценарий использует разные runtime pins у v1/v2, настоящий update и
late rollback, после чего latest bootstrap выполняет reviewed stop/backup/start/
uninstall активной старой версии. Проверяются stale plan, неподходящие locators,
target/modes, неизменность процесса при отказе, изоляция новых credentials/argv
и восстановление прерванного uninstall из доверенного recovery payload.
Отдельно проверяются backup/update/late rollback с `vault:first` и `vault:second`,
same-release restore с несовпадающими incoming refs и отказ отсутствующего
archived secret до остановки текущего процесса.
Наличие этих source checks не заменяет установку frozen артефакта на целевой
ОС, отсутствие host Python, reboot/login, VM acceptance и проверку подписанного
публичного релиза.
