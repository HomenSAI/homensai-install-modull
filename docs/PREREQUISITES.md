# Предварительные проверки, закреплённые загрузки и восстановление prerequisites

Автор: Serhii Khomenko. Реализация: `homensai_install/prerequisites.py`, `downloads.py`, `prerequisite_catalog.py`, `prerequisite_runner.py`, `prerequisite_state.py`, `prerequisite_execution.py`.

## Что работает и какие границы сохраняются

Загрузчик получает ровно закреплённый файл через HTTPS, проверяет размер и SHA-256, затем атомарно делает его доступным в кэше. Он не исполняет загруженные файлы. Предварительная проверка собирает ограниченную информацию о текущей среде, сопоставляет её с декларативными требованиями и формирует проверяемый план ручной подготовки. План требует явного согласия и повторной проверки состояния.

Совместимые функции `plan_prerequisites` / `apply_prerequisite_plan` остаются ручными. Запрос `automatic=True` с необходимыми действиями по-прежнему возвращает `CAPABILITY_UNSUPPORTED`. Отдельный `PrerequisiteCoordinator` исполняет согласованный граф через зарегистрированный `FixedCommandAdapter`, используя подпись payload, точные artifact pins, отдельные лицензионные согласия, private copies и фиксированные `argv` без shell. До регистрации доверенного каталога и платформенной политики автоматической подготовки нет.

Coordinator хранит исполнение и ожидания в собственном авторитетном журнале, переживает закрытие контроллера, допускает только явное продолжение и проверяет результат через discover. `exit 0` не означает готовность daemon. Production каталог реальных версий/хешей и installed Windows/Linux acceptance в этот source-блок не включены. При конфигурации общего SDK/UI используется один зарегистрированный coordinator; GUI не должен создавать второй executor или принимать Python callbacks/команды из HTTP.

## Доверенный каталог артефактов

`ArtifactSpec` — неизменяемая запись с полями:

| Поле | Назначение |
| --- | --- |
| `artifact_id` | Устойчивый идентификатор компонента |
| `version` | Закреплённая версия; `latest`, `main`, `master`, `nightly` запрещены |
| `url` | HTTPS URL без встроенного логина/пароля, fragment и нестандартного порта |
| `sha256` | 64 шестнадцатеричных символа, нижний регистр |
| `size` | Точное положительное число байт, ограниченное политикой |
| `license_ref` | Ссылка на проверенную лицензионную запись/файл проекта |
| `allowed_redirect_hosts` | Дополнительные точные имена доверенных CDN; без wildcard |

Конструктор `DownloadManager` отдельно получает `trusted_hosts`. Исходный host и каждый разрешённый redirect должны входить в этот список. IPv4/IPv6 literals, localhost и локальные домены не принимаются. Загрузчик проверяет TLS системным хранилищем доверия; отключить проверку сертификата штатным параметром нельзя. Неожиданный redirect отклоняется до обращения к новому адресу.

Наличие HTTPS и непустого `license_ref` само по себе **не доказывает** официальность источника и коммерческую совместимость лицензии. Подлинность каталога, право на распространение, лицензионные ограничения конкретной редакции и происхождение SHA-256 проверяет слой доверия/релиза до вызова загрузчика. Нельзя принять URL, hash и host allowlist из одного непроверенного remote JSON и считать проверку происхождения выполненной. SHA-256 выявляет изменение закреплённых байт; подпись доверенного каталога защищает сам pin.

```python
from homensai_install.downloads import ArtifactSpec, DownloadManager

# release_entry и release_hosts уже прошли проверку подписанного каталога.
artifact = ArtifactSpec.from_dict(release_entry)
downloads = DownloadManager(private_cache_dir, trusted_hosts=release_hosts)
result = downloads.fetch(artifact, progress=emit_event,
                         cancel_requested=is_cancel_requested)
assert result["verified"]
# Перед передачей файла одобренному адаптеру повторно сверить байты:
assert downloads.verify(artifact, result["path"])
```

Повторная проверка по пути не устраняет TOCTOU между проверкой и внешним исполнителем. Адаптер должен удерживать файл в приватном каталоге, проверять подлинность пакета непосредственно перед исполнением и применять предусмотренные платформой механизмы подписи. Загруженный файл не становится автоматически доверенной командой оболочки.

## Передача, кэш и восстановление

Чтение идёт ограниченными порциями; модуль запрещает ответ больше закреплённого размера и проверяет соответствие `Content-Length`, если он предоставлен. `Content-Encoding` должен быть `identity`. По умолчанию максимальный артефакт — 8 GiB, размер порции — 128 KiB, timeout запроса — 30 секунд, не более 5 redirects и 2 повторов сетевой ошибки. Параметры имеют верхние границы. Timeout относится к сетевой операции, а не к общему времени всего большого файла.

Имена файлов кэша основаны на SHA-256, а не на удалённом имени. Частичные данные находятся в `.part`; только полностью проверенный файл переводится атомарным `replace` в `.artifact`. Существующий cache hit каждый раз перечитывается и сверяется; receipt не заменяет проверку. `offline=True` использует только проверенный локальный файл и не делает HTTP-запросов. Полностью полученный `.part`, оставшийся перед аварией на этапе promotion, может быть повторно проверен и принят локально.

Частичный файл возобновляется только при сохранённом **strong ETag**: посылаются `Range` и `If-Range`, принимается `206` с точным оставшимся `Content-Range`, прежним ETag и правильным размером. При слабом/отсутствующем ETag загрузка начинается заново. Ответ `200` на Range заменяет частичные байты полной передачей. Неверный `206` или `416` приводит к одному безопасному перезапуску с нуля. В конце всегда проверяется SHA-256 целого файла. Это соответствует ограничениям HTTP Range/If-Range из [RFC 9110, разделы 13–15](https://www.rfc-editor.org/rfc/rfc9110.html).

Сетевые ошибки могут повторяться в заданном пределе. TLS certificate failure, integrity failure и неподдерживаемые HTTP-ответы не скрываются бесконечным retry. Полученный HTML error body не записывается в диагностику. Не реализованы политика `Retry-After`/авторизация корпоративного proxy и произвольные credential providers: для них требуется отдельный одобренный transport. Стандартный `urllib` использует системную конфигурацию proxy; секреты URL не отражаются в событиях или метаданных кэша.

Доступ к одному SHA-256 сериализуется файловой блокировкой. Запрещены symlink, special file и hardlink; inode проверяется до truncation. На POSIX каталог кэша должен принадлежать текущему пользователю и не давать другим запись. Родитель каталога также должен быть доверенной областью установки. Windows DACL/ACL и сопротивление подмене родительских каталогов требуют платформенного приёмочного теста; один POSIX mode не является доказательством защиты Windows. Байты и metadata сбрасываются через `fsync`; поведение при внезапном отключении питания и durability записи каталога не выдано за установленную гарантию.

События: `download.started`, `download.bytes`, `download.retry`, `download.cache_hit`, `download.verified`. Передаются `component_id`, `completed`, `total`, `unit="bytes"`, `bytes_delta` и признак resume. URL с query credentials не сохраняется: metadata содержит только hash полного URL и разрешённый `source_host`. Ошибка UI observer не меняет результат проверки файла.

## Read-only инвентаризация

```python
from homensai_install.prerequisites import (
    collect_inventory, plan_prerequisites, apply_prerequisite_plan,
)

inventory = collect_inventory(paths=[installation_dir, data_dir], ports=[8080])
requirements = [{"component_id": "docker", "kind": "docker_engine",
                 "minimum_version": "27.0.0", "scope": "host", "required": True}]
plan = plan_prerequisites(requirements, inventory, locale="ru")
# GUI показывает plan и получает согласие на его digest. Результат consent
# передаётся из подтверждённого пользовательского действия, не генерируется
# автоматически приложением из required_consents.
result = apply_prerequisite_plan(plan, consents=accepted_consents,
                                inventory=fresh_inventory)
```

`collect_inventory` возвращает ОС/release/архитектуру, Linux distribution, текущие права, число CPU, физическую RAM, свободный объём выбранных файловых систем, возможность записи в существующего родителя и результат bind-пробы выбранных localhost-портов. Порт после пробы освобождается: состояние может измениться до запуска сервиса и должно перепроверяться. Не создаются выбранные каталоги и не резервируются порты.

Есть признаки контейнерной среды и Linux внутри WSL; они не доказывают возможность управления хостом. RAM отражает физическую память, а не обязательный cgroup/VM лимит доступной приложению памяти. Состояние firmware virtualization обозначается `not_checked`. Windows/WSL fixtures в тестах не превращаются в доказательство работы на настоящем Windows.

Из окружения читается ограниченный список относящихся к установке имён. В отчёт входят присутствие и источник, структура PATH и наличие управляющих символов; **значения переменных не экспортируются**, даже если имя выглядит безобидно. Не выгружаются все env, Docker context, proxy URL, пароли или конфигурация приложения. На Windows предусмотрено read-only чтение machine/user Environment из registry. На Linux shell profiles не исполняются, а machine/user/service scopes с непроверенным источником явно отмечаются. Состояние окружения службы проверяет её собственный адаптер.

Docker проверяется через точный allowlist диагностических `argv`, с принудительным локальным Unix socket/Windows pipe. Наследуемые `DOCKER_HOST` и удалённый context не направляют probe на чужой сервер. Отдельно проверяется Compose. Пользовательские, rootless и нестандартные endpoints требуют явной интеграции; отсутствие доступа к локальному API означает `manual`, даже если CLI найден. Версия Python/SQLite контроллера имеет `scope="controller"` и не удовлетворяет требованию runtime/database самого продукта.

Детекторы произвольных PostgreSQL/MySQL/Redis и других внешних компонентов не подменены условным `present`: неизвестный detector даёт `unsupported`. Наличие apt/dpkg/dnf/rpm/pacman/zypper либо Windows msiexec/winget отражает только обнаружение менеджера; разрешения на автоматическую установку из этого не следуют.

`SafeProbeRunner` запускает только абсолютный путь и точные аргументы из доверенного Python allowlist, `shell=False`, без stdin, произвольного PATH/PYTHONPATH/LD_PRELOAD и секретов родительского env. Вывод и время ограничены; необработанный stdout не входит в inventory. На POSIX создаётся собственная группа процесса, которая останавливается при timeout/overflow. Windows поведение дочерних процессов и ACL требует отдельной проверки; универсальный Job Object контроллер здесь не реализован. Конфигурацию runner и `program_paths` нельзя брать из непроверенного пользовательского манифеста. Контракт stdlib subprocess/urllib описан в [официальной документации Python](https://docs.python.org/3.14/library/urllib.request.html).

## Статусы и согласия

`check_requirements` выдаёт для каждого компонента `present`, `missing`, `incompatible`, `unsupported`, `manual`, `reboot_required` либо `relogin_required`, причину, доказательство, версии и scope. Только `present` означает `ready`. Сравнение версий числовое; неизвестные prerelease/vendor форматы при заданной границе требуют ручной проверки. Неполная версия WSL не превращается в выдуманную сопоставимую SemVer.

План привязан к discovery fingerprint и canonical SHA-256. Изменение плана вызывает `INTEGRITY_FAILED`, изменение повторной инвентаризации — `PLAN_STALE`, отсутствие согласия на конкретный digest — `CONSENT_REQUIRED`. Само согласие на ручной план не является полномочием выполнить произвольную системную команду. Перед продолжением после ручной подготовки создаётся новый план, показывающий текущее состояние.

Итог `apply_prerequisite_plan`: `READY`, `AWAITING_USER`, `REBOOT_REQUIRED` либо `RELOGIN_REQUIRED`. Во всех случаях `system_changes_performed=False`. Необязательный отсутствующий компонент остаётся явно отсутствующим и не блокирует обязательные. Руководство доступно на RU/EN/DE, содержит ссылки на официальные [Docker Engine](https://docs.docker.com/engine/install/), [Docker Desktop для Windows](https://docs.docker.com/desktop/setup/install/windows-install/), [Compose](https://docs.docker.com/compose/install/) и [WSL](https://learn.microsoft.com/en-us/windows/wsl/install). Инструкции не содержат команд для автоматического исполнения.

## Подписанный граф, лицензии и выбор пользователя

`SignedPrerequisiteCatalog(manifest, target, options, *, verifier, descriptor="installer/prerequisites.json")` использует существующий `SignedPayloadVerifier`. Тестовые ключи допускаются только явно включённым development verifier. Проверяются весь payload, manifest, release, runtime executable, OS/arch/mode и полное target binding каталога, включая scope. `catalog_digest` также включает полный перечень подписанных файлов: обновление другого payload-файла не сохраняет старое согласие на prerequisite plan.

Descriptor имеет строгую schema 1; дополнительные поля, команды и `argv` в JSON запрещены:

```text
schema_version: 1
catalog_id, catalog_version, product_id
target: {os, arch, mode, scope}
entries: [
  {
    requirement: {component_id, kind, required, minimum_version,
                  maximum_version, scope},
    depends_on: [component_id, ...],
    ownership: shared | external | managed_private,
    adapter_id: trusted_registry_id | null,
    artifacts: [ArtifactSpec, ...],
    license: {
      license_id, version, text_path, text_sha256,
      locale: ru | en | de,
      decision: allowed | conditional | blocked | unknown,
      acceptance_required: boolean,
      conditions: [condition_id, ...]
    }
  }
]
```

Граф содержит 1–128 уникальных компонентов; цикл, неизвестная зависимость и повторный artifact ID отклоняются. У одного действия может быть до 16 закреплённых артефактов, например полный набор `.deb`. Автоматический adapter без artifact pins запрещён. Для системной feature-команды требуется отдельно закреплённый проверенный helper, а не произвольный `wsl --install` из манифеста.

Лицензионный текст включён в тот же подписанный inventory и дополнительно сверяется с `text_sha256`. Его можно прочитать через `catalog.license_text(component_id)`; GUI показывает этот текст и версию до согласия. `artifact.license_ref` обязан совпадать с `license_id`. `conditional` требует явно перечисленных условий; `blocked` / `unknown` не допускают автоматическую подготовку и не становятся готовыми вследствие ручной установки. Решение о праве на распространение/коммерческом использовании принимает проверенная release policy, а не coordinator. Наличие поля `allowed` в неподписанном JSON ничего не разрешает.

Пользователь выбирает `auto`, `manual`, `use` либо `skip` для известного component ID. `use` требует уже готового компонента. Required-компонент и зависимость выбранного компонента нельзя пропустить. Невыбранный optional по умолчанию пропускается; явно выбранный optional обязан пройти готовность. Уже готовый компонент не переустанавливается даже при `auto` и сохраняется как `external`, а не объявляется вновь установленной собственностью. Ручная подготовка не создаёт `managed_private` ownership. Исчезновение готового компонента во время ожидания не превращает старое согласие в разрешение на переустановку.

Plan отдельно запрашивает product EULA references, согласие на конкретный review digest, выбранную автоматическую установку, лицензионный текст с версией/хешем, перечисленные условия и повышение прав для соответствующего adapter. Выбор `manual` не разрешает запуск команды. `required_consents` — список для отображения и проверки; приложение не должно автоматически принимать этот список вместо человека.

## Контракт coordinator для общего SDK

```python
from homensai_install.prerequisite_catalog import SignedPrerequisiteCatalog
from homensai_install.prerequisite_execution import PrerequisiteCoordinator

catalog = SignedPrerequisiteCatalog(
    manifest, target, signed_payload_options, verifier=trusted_verifier,
)
coordinator = PrerequisiteCoordinator(
    catalog,
    state_dir=original_owner_prerequisite_state_dir,
    owner_id=stable_opaque_original_owner_id,
    downloads=downloads,
    adapters=trusted_fixed_adapter_registry,
    discover=read_only_inventory,
    markers=trusted_boot_and_login_markers,
    product_consent_refs=product_eula_references,
    event_callback=operation_event_projection,
    reconcile=read_only_installer_reconciliation,
)
plan = coordinator.plan(
    {"docker": "manual"}, locale="ru", offline=False,
)
# accepted_consents поступает из подтверждённого действия пользователя.
state = coordinator.start(operation_id, plan, consents=accepted_consents)
# По кнопке «Проверить снова»: только discover + запись результата в журнал.
state = coordinator.recheck(operation_id)
# Явное продолжение может исполнить следующие согласованные auto действия.
state = coordinator.resume(operation_id)
```

Конструктор создаёт только приватный журнал контроллера. Реестр, discover, marker/reconcile callbacks и owner identity поступают из доверенного bootstrap. `downloads` должен быть настоящим `DownloadManager`, каталог — настоящим `SignedPrerequisiteCatalog`, adapter — `FixedCommandAdapter`. Пользовательский запрос не может переопределить executable path, flags, allowed hosts, environment либо policy exit codes.

| Метод | Результат и побочные эффекты |
| --- | --- |
| `identity()` | Product/target/catalog digest/original owner, без запуска программ |
| `capabilities()` | Повторная проверка signed catalog; реально зарегистрированные `automatic_component_ids`, `manual_recheck`, `durable_resume`, `automatic_reboot=False`, `platform_acceptance="not_verified"` |
| `plan(choices=None, locale="ru", offline=False, local_artifacts=None)` | Read-only discover, graph closure, конкретные компоненты/условия, `plan_digest` и `required_consents` |
| `start(operation_id, plan, consents=...)` | Свежая проверка плана и consent; durable operation/claim; исполнение доступных auto действий по dependency order |
| `status(operation_id)` | Исторический durable snapshot, без discover/download/exec; проверяет original owner/product/target |
| `events(operation_id, after=0, limit=100)` | Последовательные durable события; `limit` 1–1000 |
| `attempts(operation_id, component_id, after=0, limit=100)` | История installer attempts с receipts; новый безопасный retry не стирает прежние pins и reconciliation |
| `recheck(operation_id)` | Только read-only discover/reconcile и журнал; не скачивает, не исполняет и не продолжает auto ветку |
| `resume(operation_id, consents=None)` | Проверяет прежние pins/consents/context; явно продолжает допустимые pending auto действия той же операции |
| `choose_boundary(operation_id, "now" или "later")` | Фиксирует выбор; возвращает `host_action` с `performed=False`; не вызывает shutdown/logoff |
| `cancel(operation_id)` | Durable запрос отмены; прекращает работу на безопасной границе, сохраняет общие зависимости |
| `replace_plan(operation_id, plan, consents=...)` | Новый просмотр/согласия для изменившегося контекста в той же operation; не исполняет новый план автоматически |

`start`/`resume` синхронны: общий API может выполнять их в своём worker и читать `status/events` параллельно. Повторный `start` с теми же operation ID/plan идемпотентен и возвращает snapshot, не запускает installer повторно. Номер операции берётся из общего SDK; resume не создаёт новую product installation.

Plan содержит `product_id`, `target`, `owner_id`, `catalog_digest`, `context_digest`, `choices`, упорядоченные `components`, `offline`, `local_artifacts` и согласия. Сериализованный plan ограничен 512 KiB, product consent refs — 64; в journal записываются только нужные этой операции подтверждения. `local_artifacts` отображает только выбранные signed artifact IDs на явные абсолютные пути. Локальные байты проходят ту же exact size/hash и host policy; offline не подменяет доверие к каталогу. Путь нельзя превратить в дополнительный аргумент команды.

Public state содержит `operation_id`, product/target/owner, `status`, `ready`, `revision`, `steps`, `checks`, `blocking_components`, `required_action`, `error`, временные отметки и последние 100 событий. Полная история доступна через cursor. В snapshot нет полного плана, локальных artifact paths, download URL/query, installer stdout/stderr или значений окружения. `system_changes_started=False` означает отсутствие известного старта, `None` — записанный intent с ещё неизвестным фактом запуска, `True` — подтверждённый started receipt. Это не гарантия готовности компонента; её устанавливает только discover.

Context binding сохраняет product/target/original owner, существенные platform/identity/resources/disks/ports и внешние компоненты. Изменения проверяемых prerequisites ожидаются во время этой операции; свободные байты диска и признак elevated сами по себе не инвалидируют план. После ручной подготовки, долгой загрузки и перед следующим installer повторяются проверки. Материальное изменение возвращает `PLAN_STALE` / `required_action="replan"`. Новый план получает новые согласия через `replace_plan`; завершённые receipts сохраняются. Пропавший, изменённый или повреждённый signed payload блокирует дальнейшие эффекты. Исторический `status` доступен исходной регистрации и при повреждении payload, чтобы не потерять сведения восстановления.

## Исполнение и платформенные политики

До запуска DownloadManager проверяет approved host, размер и SHA-256. Coordinator копирует artifact в приватный каталог конкретной operation/component/attempt, проверяет копию и синхронизирует запись. `FixedCommandAdapter` повторно сверяет каждую копию непосредственно перед исполнением. Windows adapter дополнительно требует `publisher_check(path, spec)` через доверенный OS verifier. Exact hash/size не заменяют Authenticode/publisher policy.

`FixedCommandRunner` принимает только точное совпадение полного `argv`. Executable имеет абсолютный путь и SHA-256, выполняется без shell, stdin и вывода, с ограниченным временем и разрешённым owner environment. Shell executables, `.bat`/`.cmd`, PATH lookup, произвольный parent env, loader variables и credentials запрещены. Путь к artifact подставляется только в целый `{artifact_N}` аргумент; каждый artifact используется один раз. Системный executable также имеет отдельный pin, а executable artifact — подписанный artifact pin. Private directory и повторная проверка уменьшают окно подмены; защита Windows ACL/reparse/OS installer child processes требует реальной платформенной проверки.

`adapter.check` / trusted preflight проверяет актуальную платформу, зависимости, конфликты, место и права до эффекта. Артефакт не может принудить текущий процесс стать administrator. Встроенный runner отказывает без нужных прав. Для UAC/sudo нужен отдельно проверенный ограниченный helper, зарегистрированный bootstrap; journal и owner context остаются у исходного владельца. Не поддерживается передача произвольной elevated команды из HTTP.

Две factories задают конкретные source-политики:

| Factory | Фиксированное действие | Обязательная внешняя проверка |
| --- | --- | --- |
| `docker_desktop_user_adapter` | Закреплённый Desktop installer: `install --user --quiet --accept-license --backend=wsl-2 --no-windows-containers` | Поддерживаемый Windows/build, virtualization, готовый совместимый WSL, конкретный Docker publisher, отдельные Desktop terms и права использования |
| `docker_ubuntu_deb_adapter` | Закреплённый `dpkg --install` с пятью точными файлами: containerd.io, docker-ce, docker-ce-cli, buildx, compose | Поддерживаемые distro/arch, доверенный dpkg pin, полный состав и порядок пакетов, внешние OS dependencies, конфликты и elevation |

Source flags и пакетный состав сверены с официальными [Docker Desktop Windows](https://docs.docker.com/desktop/setup/install/windows-install/) и [Docker Engine Ubuntu](https://docs.docker.com/engine/install/ubuntu/) инструкциями. Это не заменяет acceptance конкретных закреплённых версий. Factories не содержат выдуманных production URL/hash, не используют `latest`, не скачивают convenience shell scripts и не удаляют чужой Docker/containerd. Автоматическая подготовка firmware/WSL, установка rootless policy, изменение групп и настройка user/service окружения не объявлены реализованными этими двумя factories: они требуют собственного signed helper/approved platform adapter либо ручной ветки. Docker API, Compose, доступ исходного пользователя и выбранный rootful/rootless профиль должны быть подтверждены отдельными discover checks. Desktop installer exit 0 не является доказательством запущенного Docker daemon.

## Durable reboot/relogin и неизвестный результат

Операции, single active claim, отдельная история installer attempts и события хранятся в private SQLite с транзакциями и `synchronous=FULL`. OS advisory lock сериализует контроллеры на том же `state_dir`; сам факт освобождения lock не доказывает завершение запущенного installer. Bootstrap должен использовать один authoritative prerequisites directory для операций, конкурирующих за одинаковые зависимости исходного владельца. Обёртка диагностики получает события после commit и не становится источником восстановления.

До команды сохраняется `INSTALLING` intent: attempt, точные artifact pins, adapter policy, время, boot/session markers. После известного завершения сначала фиксируется результат, потом выполняется discover. Дети системного installer не убиваются по timeout; результат остаётся неизвестным. Секретный вывод не сохраняется.

| State | Что произошло | Что продолжает операцию |
| --- | --- | --- |
| `RUNNING` | Загрузка/проверка/installer выполняется | Завершение текущей стадии; доступен параллельный polling/cancel request |
| `AWAITING_USER` | Нужна ручная подготовка, разрешение ошибки либо явное продолжение auto | `recheck`, затем `resume`, при изменившемся плане — `replace_plan` с новыми согласиями |
| `REBOOT_REQUIRED` | Проверенный результат или discover требует reboot | Выбор now/later, реальное изменение boot marker, fresh discover и явный resume |
| `RELOGIN_REQUIRED` | Требуется новая login session | Изменение проверяемого session marker, fresh discover и явный resume |
| `RECOVERY_REQUIRED` | Crash/timeout оставил unknown execution outcome | Доказанное read-only reconciliation либо новый boot + готовый компонент |
| `READY` | Все выбранные, включая выбранные optional, прошли readiness/licence policy | Это результат prerequisites данной операции; core повторяет product preflight перед установкой |
| `CANCELED` | Отмена достигла безопасной границы | Общие компоненты и receipts сохраняются; автоматического удаления нет |

`now`/`later` сохраняет конкретное решение. Actual reboot/logoff выполняется доверенным host UI по явному действию пользователя; coordinator не создаёт автозапуск с повышенными правами и не инициирует reboot сам. После возвращения bootstrap открывает тот же state directory и operation ID. Открытие GUI, новый PID и повторный `resume` не считаются reboot/relogin.

Встроенный Linux marker provider читает kernel boot ID и, если он назначен, audit login session ID; значения хранятся как opaque hashes. Если audit session не назначена, session marker остаётся `None`. Для Windows нужен явно переданный доверенный platform marker provider; заглушка не выдумывает Boot ID из PID/clock. Если старый или новый нужный marker отсутствует, readiness не снимает ожидание перехода. Это отражённое ограничение платформенной интеграции.

Crash между effect и записью result не допускает blind replay даже при видимом `present` в той же boot session. Read-only `reconcile(entry, receipt, inventory)` может вернуть `applied`, `not_applied`, `running` или `unknown`; доказательство не строится только на PID. `applied` всё равно требует fresh readiness. `not_applied` разрешает повтор при последующем явном `resume`; `recheck` его не запускает. Новый boot исключает работу старого installer, но допускает завершение только при подтверждённом готовом компоненте. Удалить unknown receipt, заменить его «успехом» диагностического лога или разблокировать эффект простой заменой плана нельзя.

Cancel во время работы записывается транзакционно без ожидания controller lock. Downloader проверяет запрос на границах порций; перед installer проверка повторяется. Уже запущенный installer получает возможность безопасно закончить; затем следующая зависимость не запускается. При неизвестном результате отмена остаётся pending в `RECOVERY_REQUIRED`. Общие Docker/WSL/runtime не удаляются ни cancel, ни recheck, ни resume. `managed_private` сохраняется как ownership metadata; удаление принадлежит lifecycle core, а не prerequisite coordinator.

## Проверка этого блока

Focused-команда:

```text
PYTHONPATH=/workspace/scratch/51fcf3970f58/test-deps:$PWD \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q -p no:cacheprovider \
  tests/test_prerequisites.py tests/test_prerequisites_execution.py \
  tests/test_prerequisites_runner.py tests/test_downloads.py tests/test_trust.py
```

Результат локального source-прогона 2026-10-25: **115 passed in 7.38s**, Linux / Python 3.12.14. `git diff --check` — без замечаний. Это не результат GitHub Actions и не проверка на release Python 3.14.

Сеть запрещена общей test fixture. HTTP/Range/ETag/TLS ошибки воспроизводятся внедрённым fake transport. Новые тесты используют отдельный ephemeral Ed25519 key, полный подписанный synthetic payload, синтетические artifacts, реальные private copies/SQLite и fake platform installers. Дополнительный тест проводит подписанный synthetic Python artifact через весь coordinator до настоящего fixed-argv subprocess; он пишет только файл в pytest temp directory. Реальные runner-тесты проверяют exact allowlist, отсутствие наследуемых секретов/shell и unknown timeout без убийства installer. Это не установка пакетов ОС.

Покрыты dependency order/optional selection, signed metadata/полный payload binding/licence text, consent separation, reject before download/exec, exit 0 без readiness, ручной recheck без эффекта, durable restart/reboot/relogin, unknown markers, crash после intent/effect, запрет replay, read-only reconciliation, owner/product binding, stale context/replan, параллельная отмена на безопасных границах, process lock/single active claim, offline local artifact verification и диагностический observer failure. Прежние downloader/inventory/trust tests остаются в focused suite.

**Не выполнены и не считаются принятыми:** установка реального Docker/OS package, Windows UAC/Authenticode/ACL/boot-session provider, реальные WSL/virtualization changes, installed Ubuntu dependency/conflict/rootless policy, настоящий reboot/relogin, автономный bootstrap, реальные CDN/proxy и power-loss. Эти acceptance gates остаются `NOT RUN` / `BLOCKED` до соответствующей среды; source/synthetic tests не закрывают их.
