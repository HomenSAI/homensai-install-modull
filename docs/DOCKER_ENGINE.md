# Docker: подписанный contract и контроллер принадлежащих установке ресурсов

## Фактическая готовность

В этом выпуске реализован **контроллер уже подготовленной Docker-установки**. Он
выполняет проверку ресурсов, запуск, остановку, проверку работоспособности и
зашифрованное резервное копирование данных. Это отдельный backend SDK, а не
автоматическая установка Docker, Compose или нового продукта.

На рабочем хосте разработки Docker daemon отсутствует. Ни Docker daemon, ни
Docker CLI для обращения к daemon здесь не запускались. Проверки с подставным
runner подтверждают поведение кода; они **не являются приёмкой установки на
Docker**. `production_ready` всегда `false`, `live_acceptance` — `REQUIRED`.

| Возможность | Состояние исходного кода | Граница |
|---|---|---|
| Signed contract и review ресурсов | Реализованы | Чистый plan ничего не создаёт |
| `discover`, `status` | Реализованы | Подписанные bytes, ownership и состояния через inspect |
| `start` | Реализован | Только точные записанные container ID, порядок зависимостей |
| `stop` | Реализован | Обратный порядок, один `SIGTERM`, подтверждённый exit code 0 |
| `health` | Реализован | Собственный контейнер, точное подключение порта, private challenge proof |
| `backup` | Реализован | Все писатели остановлены, AES-256-GCM, исходный набор работающих компонентов восстанавливается |
| `resume` | Реализован для перечисленных операций | Durable journal; неопределённый результат RPC не повторяется вслепую |
| `install`, `update`, `restore`, `rollback`, `repair`, `reinstall`, `uninstall` | `CAPABILITY_UNSUPPORTED` | Нужен provisioning/transaction adapter |
| Windows Docker Desktop, Linux Desktop/VM, rootless, remote daemon | `CAPABILITY_UNSUPPORTED` | Нужны отдельные проверенные path/ACL/namespace adapters |
| Службы host OS и автозапуск | Не предоставляются этим контроллером | Нет установки SCM/systemd и нет автоматического рестарта контейнера |

`DockerEngine.capabilities()` объявляет `start/stop/backup` доступными только
при доступном доверенном локальном daemon и настроенном verifier; для backup
также требуется secret provider. Это доступность development-кода на данном
хосте. Конкретная операция дополнительно требует корректного signed payload и
приватной ownership receipt. Наличие только файла `docker` или похожего имени
контейнера недостаточно.

Старые `shared/adapters_docker.py` и `shared/render_docker.py` остаются отдельным
Compose-v1 интерфейсом. Они не подменяют данный контроллер и не получают его
гарантии автоматически. GUI/CLI выбирает backend только через доверенный
bootstrap; передача HTTP-поля `mode=docker` сама по себе его не подключает.

## Модули и API

- `homensai_install/docker_cli.py` — ограниченный процесс Docker CLI.
- `homensai_install/docker_contract.py` — строгая проверка подписанного JSON и
  чистый `resource_plan()` для будущего provisioner.
- `homensai_install/docker_engine.py` — lifecycle существующих owned resources.
- `tests/test_docker_cli.py` — реальные временные synthetic ELF-процессы,
  ограничения argv/environment/output/timeout и executable pin.
- `tests/test_docker_engine.py` — реальные Ed25519/AES и файловые операции с
  моделируемыми ответами daemon. Сетевые обращения в unit suite запрещены.

Контроллер соответствует engine protocol публичного SDK:

```python
from homensai_install.api.v1 import InstallerAPI
from homensai_install.docker_cli import DockerCLI
from homensai_install.docker_engine import DockerEngine
from homensai_install.trust import load_trust_config

# Все значения ниже выбирает доверенный bootstrap. Их нельзя принимать из
# произвольного HTTP-запроса, environment Docker context или unsigned payload.
runner = DockerCLI(
    executable=approved_absolute_docker_cli_path,
    sha256=approved_docker_cli_sha256,
    endpoint="unix:///run/docker.sock",
    config_dir=existing_private_empty_cli_directory,
)
engine = DockerEngine(
    verifier=load_trust_config(pinned_publisher_keys_path),
    runner=runner,
    secret_provider=application_secret_provider,
)
api = InstallerAPI(api_state_directory, engine=engine)
```

Параметры constructor `fetcher` и `host_probe` предназначены для доверенных
интеграций и unit fixtures. Они не являются options публичного запроса. Замена
этих зависимостей на функции, безусловно возвращающие успех, уничтожает
соответствующую гарантию проверки.

Обычные options: `install_root`, `payload_dir`, `inventory_file`,
`signature_file`, `release`, `runtime_executable`, необязательные `modes`,
`work_dir`, `archive_path`, `backup_password_ref`. Набор `modes` должен совпадать
с receipt: менять состав подготовленных компонентов эта версия не умеет.

`discover(manifest, target, options)` ничего не запускает и не изменяет в
продукте. `plan(action, manifest, target, inventory, options)` возвращает
ресурсы, стадии, fingerprint и согласие `action:<action>`. Публичный API
дополнительно требует принятия конкретного неизменяемого плана. Пароль в plan
не извлекается; secret provider вызывается перед фактической операцией и до
остановки продукта.

`execute(plan, emit, cancel_requested)` и `resume(...)` используют общий
install-root lock независимо от каталога состояния конкретного API-клиента.
События содержат stage, component ID и измеряемое завершение. Во время
неизвестной длительности `total=null`; фиктивные проценты не вычисляются.

## Trust и формат Docker contract

Весь payload проверяется Ed25519 и полным SHA-256 inventory. Подпись связывает
исходный manifest v2, target, release, contract и license evidence. Ключи
внедряются trusted bootstrap; ключ из самого payload доверенным не становится.

Для совместимости с существующим **inventory schema 1**:

```json
{"runtime_executable": "docker-contract.json"}
```

В Docker backend это **selector подписанного JSON descriptor**. Контроллер
никогда не делает `chmod` этого JSON и не исполняет его, системный Python или
другие команды продукта на host. Будущая inventory schema 2 должна выделить
`backend_descriptor` в самостоятельное поле. Target обязан явно содержать
`mode: docker`, `os: linux|windows`, `arch: x64|arm64`, `scope: user`. Pure loader
может проверить Windows descriptor, но execution на Windows этим не включается.

Структура `docker-contract.json`:

```json
{
  "schema_version": 1,
  "backend": "docker-managed",
  "platform": "linux/amd64",
  "network": {"internal": true},
  "storage": {
    "strategy": "filesystem_quiesced",
    "all_writers_managed": true
  },
  "web": {
    "component": "web",
    "container_port": 8080,
    "probe_cmd": ["/app/bin/health-probe", "--private-listener"]
  },
  "license_review": "legal/images.json",
  "components": [
    {
      "name": "web",
      "image": "registry.example/product@sha256:<64 lowercase hex>",
      "image_id": "sha256:<64 lowercase hex>",
      "user": "1000:1000",
      "mounts": [
        {"root": "data", "target": "/homensai/data", "read_only": false},
        {"root": "config", "target": "/homensai/config", "read_only": true}
      ],
      "memory_bytes": 268435456,
      "pids_limit": 128,
      "stop_timeout_seconds": 30,
      "require_docker_health": true
    }
  ]
}
```

Это схема с placeholder digest, не готовый image или команда установки.
Неизвестные поля отклоняются. Число компонентов ограничено 32; они должны
совпадать с manifest. `cmd` каждого компонента задаёт точные container
Entrypoint/Cmd. `probe_cmd` тоже подписан. Пользователь контейнера — явный
не-root UID:GID. Пути mount могут относиться только к `data`, `config`,
`runtime`, `services`, `backup` данного DataHome; назначения находятся под
`/homensai/`, без пересечений и traversal. Для каждого компонента обязательны
`/homensai/data` и `/homensai/config`.

Manifest с product secrets или непустыми `hooks.pre_start`, `health_checks`,
`post_update` отклоняется до появления credential/hook adapter. Legacy
`migrations.cmd`, `backup.cmd`, `restore.cmd` в этом профиле не выполняются:
установка уже подготовлена, а backup использует остановленный filesystem.
Legacy Python lock/wheel metadata не приводят к установке host Python или pip.
Самодостаточность runtime обеспечивает содержимое утверждённого image.

### Лицензии образов

Подписанный `legal/images.json` содержит `schema_version: 1`,
`status: "approved"` и `images` с записью для каждого component ID. Запись
содержит точные `image`, `image_id`, `commercial_use: true`,
`redistribution: true`, `sbom_file`, `notices_file`. Оба файла должны входить в
полный подписанный inventory. Отсутствующее или отрицательное решение запрещает
подключение. Файл разрешения — результат внешнего проверенного release-процесса,
**не автоматическое юридическое заключение контроллера**. Подпись не заменяет
проверку лицензий всех слоёв и приложенных обязательных notices/source offers.

Docker Desktop имеет отдельные условия и в части коммерческих сценариев требует
платной подписки; лицензия Engine/Moby не делает Desktop автоматически свободно
распространяемым. Модуль не скачивает Desktop, не принимает его соглашение за
пользователя и не включает его в дистрибутив. См. [официальные условия Desktop](https://docs.docker.com/subscription-billing/desktop-license/).

## Подготовка и ownership receipt

До подключения контроллера доверенный provisioner обязан создать каталоги,
получить и проверить утверждённые образы, создать isolated bridge и контейнеры
строго по `resource_plan`, настроить права bind-каталогов и зафиксировать IDs.
В данном выпуске этот provisioner отсутствует. Контроллер не усыновляет
контейнеры по label, имени, занятому порту или похожему compose project.

Файл `<install_root>/.hinstall/docker-installation.json` принадлежит текущему
пользователю, имеет права 0600 и единственную hard link. Каталог `.hinstall`
имеет 0700. Receipt содержит ровно:

- `schema_version: 1`, `mode: docker`, `product_id`, `release`;
- `installation_id` — 32 lowercase hex, `owner` — текущий UID;
- `daemon_id` и SHA-256 канонического local endpoint в `endpoint_sha256`;
- `manifest_sha256`, `contract_sha256`;
- `modes` и `containers: {component_id: full_64_hex_container_id}`;
- `network: {id: full_64_hex_network_id, name: deterministic_resource_name}`.

Изменение receipt между планом и исполнением блокируется. Нативная
`installation.json` в том же control directory означает конфликт backend.
Отсутствие receipt приводит к отказу, а не поиску «подходящих» чужих контейнеров.

Каждый ресурс имеет метки `io.homensai.installer.*`: installation, product,
release, contract, component, для контейнера ещё hash ожидаемой спецификации.
Контроллер одновременно сверяет точный ID, эти метки, имя и фактическую
конфигурацию. Образ сверяется по registry digest, config image ID, OS/arch и
attestation локального доверенного daemon. Mutable tags не разрешаются.

## Исполнение и изоляция

Docker CLI выбирается абсолютным путём с закреплённым SHA-256. Перед каждым
вызовом проверяются байты, тип ELF, owner/permissions и защищённость родителей.
Linux запускает проверенный открытый inode через `/proc/self/fd`, чтобы
замена pathname после hash не выбрала другой файл. Shell не используется.

CLI получает явные `--host` и `--config`; directory конфигурации должен быть
приватным и пустым либо содержать единственный приватный `config.json` с `{}`.
Environment не наследует Docker contexts, proxies, auth headers, Python/loader
переменные. `stdin=DEVNULL`. Совокупный stdout+stderr ограничен 1 MiB; stderr
не возвращается и не записывается. Каждый CLI child ограничен timeout; timeout
или overflow останавливает только собственный процесс CLI. Это **не означает**,
что daemon отменил уже полученную команду, поэтому результат помечается как
неопределённый и согласуется последующим inspect.

Разрешены только ограниченные формы `info`, `image/container/network inspect`,
`container ls`, `container start`, `container kill --signal=SIGTERM` и
`container exec` для подписанного probe. Нет pull/build/create/rm/prune,
глобальной остановки, socket mount или привилегированных exec options.

До CLI проверяется Unix peer через `SO_PEERCRED`: root-owned `dockerd` должен
работать в тех же mount и network namespaces, что и контроллер. Это исключает
подмену реальных host-path данных путями внутри внешнего container/VM или
простым socket forwarder. Rootless/Desktop/другие пространства имён требуют
дополнительного adapter. Этот профиль не повышает привилегии и не добавляет
пользователя в группу Docker.

Фактические контейнеры обязаны иметь readonly root filesystem, numeric non-root
user, `cap-drop ALL`, `no-new-privileges`, private PID/IPC/UTS/cgroup namespaces,
ограничения memory/pids, `restart=no`, `log-driver=none`. Дополнительные devices,
capabilities, groups, links, volumes-from, extra-hosts, sysctls, host namespace
или неожиданные mounts запрещены. Единственный временный writable tmpfs —
`/tmp` с noexec/nosuid/nodev и фиксированным пределом. Все постоянные bind mounts
сверяются по source/destination/RW/propagation.

Сеть — owned local internal bridge без чужих участников. На host публикуется
только `127.0.0.1:<manifest.web.port>` нужного web-компонента. Внутри контейнера
`HOMENSAI_WEB_HOST=0.0.0.0` относится к его отдельному network namespace.
HostConfig port intent и фактические NetworkSettings bindings проверяются
раздельно. Сторонние подключения к сети и wildcard-публикация блокируют работу.

### Проверка веб-сервера

Один HTTP 200 недостаточен. Контроллер создаёт случайный challenge, выполняет
подписанный `probe_cmd --challenge <nonce>` **внутри точного owned container** и
сравнивает результат с JSON через опубликованный loopback порт. Оба ответа
должны содержать product ID, installation ID, release, `ready: true`, тот же
challenge и одинаковый 64-hex proof.

Product adapter обязан формировать proof, например HMAC-SHA-256, с отдельным
секретом текущего серверного экземпляра. Этот секрет хранится внутри процесса
или защищённой runtime-области и не передаётся в публичном ответе. Private probe
должен обращаться к **этому же серверному listener**, а не самостоятельно
возвращать «готов» или вычислять публично известную функцию от challenge.
Контроллер проверяет протокол; корректность подписанного product probe входит
в доверие к утверждённому продукту и проверяется отдельной live-приёмкой.

HTTP-запрос не использует proxies и redirects, ограничивает размер ответа и
timeout. Состояние контейнеров, портов и instance timestamps сверяется до и
после ответа. Readiness повторяется в пределах manifest `health_wait`;
оставшийся бюджет передаётся CLI/HTTP. При явной ошибке ownership/integrity
повтор не скрывает нарушение. После прерывания cached health не превращается
в новый успех без повторной проверки.

## Backup и восстановление прерванной операции

Backup относится к **данным**: все пять постоянных DataHome roots. Образы,
container layers и секрет шифрования в него не добавляются. Результат явно
содержит `contains_images=false`, release, contract hash, путь и SHA-256 архива;
`restoration_supported=false` отражает отсутствие Docker restore adapter.
Для будущего восстановления отдельно понадобятся соответствующие доверенные
descriptor/images и provisioning transaction. ZIP без шифрования не принимается.

До остановки проверяются secret reference и выбранный backup directory. План
сохраняет исходный набор работающих компонентов. Далее в обратном порядке
отправляется один SIGTERM и ожидается нормальное завершение каждого писателя.
Ненулевой exit, OOM, ошибка или timeout запрещают snapshot. `docker stop` здесь
не используется, поскольку после его timeout daemon может перейти к SIGKILL;
см. [Docker stop](https://docs.docker.com/reference/cli/docker/container/stop/)
и [Docker kill / explicit signal](https://docs.docker.com/reference/cli/docker/container/kill/).

Перед и после snapshot контроллер проверяет всех доступных контейнеров daemon
(не более 256), не читая environment чужих контейнеров. Любой foreign RW bind,
пересекающий DataHome, блокирует backup, даже если контейнер сейчас остановлен.
Сверка `/proc/self/mountinfo` выявляет также bind aliases другого pathname.
Foreign RW named volumes без доказательства отсутствия алиасов консервативно
блокируются. Persistent roots и recovery directory не могут оказаться одним
физическим filesystem-поддеревом под разными именами. Hard-linked пользовательские
файлы требуют отдельного storage adapter и не архивируются этим профилем.

После остановки сохраняется fingerprint полного filesystem snapshot. AES-256-GCM
archive создаётся существующим проверенным `shared/archive_crypto.py`, полностью
проверяется обратным чтением и связывается с intent операции. Перед повторным
запуском перепроверяются hash, аутентификация, состав snapshot и отсутствие
новых писателей. Обычные host-процессы вне управляемого продукта не являются
Docker resources: signed `all_writers_managed=true` — обязательное условие
интеграции, а не обещание обнаружить злонамеренный root или произвольный внешний
писатель. Изменение DataHome во время snapshot дополнительно выявляется сверкой
полного inventory до и после чтения.

Durable journal располагается в `.hinstall/docker-operations/`. Он сохраняет
намерение до mutating RPC и подтверждение после inspect. При обрыве:

1. Повторяется проверка подписи, receipt, daemon, namespaces и конфигурации.
2. Тот же plan/token продолжает durable стадии; другой token не обходит
   незавершённую операцию.
3. Running container после прерванного start признаётся только по полному ID и
   спецификации. Если start был отправлен, но контейнер уже остановлен, команда
   не повторяется автоматически.
4. Незавершённый SIGTERM не отправляется второй раз; контроллер ждёт и проверяет
   результат. Неопределённость возвращает `RECOVERY_REQUIRED`.
5. Уже опубликованный до crash archive принимается только после полной
   криптографической проверки и совпадения snapshot intent. Существующий чужой
   файл не перезаписывается.
6. Отмена происходит на durable границе. После cancel/ошибки в середине backup
   компоненты могут оставаться остановленными; `resume_required` и journal
   определяют продолжение. Самовольного запуска поверх неизвестного snapshot нет.
7. Перед новым `SUCCEEDED` сверяется точный ожидаемый набор работающих
   контейнеров; health выполняется заново. Старый phase checkpoint не выдаёт
   ложный `HEALTHY` или `STOPPED` после внешнего изменения состояния.

## Обязательные следующие этапы приёмки

Нужно реализовать и проверить atomic provisioning с compensation: установка
доверенного prerequisite Docker, разрешённое получение image, создание путей и
ACL/UID ownership, create container/network, запись receipts, migration adapter,
обновление/rollback, restore и удаление только принадлежащих установке ресурсов.
Для Docker Desktop/Windows требуется отдельное подтверждение host↔VM paths,
named-pipe ACL и локальной идентичности web listener. Ни pure resource plan,
ни synthetic ответы не закрывают эти этапы.

Live matrix должна включать Linux x64/arm64, реальные CLI/daemon версии,
различия inspect-полей и tmpfs/mounts, reboot, process/CLI kill после каждого
checkpoint, полный диск, недоступный daemon, закрытый/чужой порт, неточный
image/label/mount/namespace, отказ SIGTERM, foreign writer и повреждённый архив.
Отдельно проверяются все слои image SBOM и условия коммерческого использования.

Официальные источники для принятых технических границ:

- [Pin image by immutable digest](https://docs.docker.com/reference/cli/docker/image/pull/).
- [Docker daemon socket and contexts](https://docs.docker.com/engine/security/protect-access/).
- [Container execution options](https://docs.docker.com/reference/cli/docker/container/run/).
- [Docker Desktop on Linux uses a VM](https://docs.docker.com/desktop/setup/install/linux/).
