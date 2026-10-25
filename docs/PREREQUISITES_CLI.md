# CLI подготовки зависимостей

Автор: Serhii Khomenko / HomenS.AI. API `1.0`, development contract.

Команды `prerequisites` обращаются к тому же `InstallerAPI` и durable
`PrerequisiteCoordinator`, что и SDK. Они не содержат отдельного installer,
registry или журнала эффектов. Проверка этого transport не означает приёмку
Windows, Docker, elevation, реальных OS packages или production catalogue.
Невыполненные проверки перечислены в `VERIFICATION_BACKLOG.md` и
`verification-status.json`; наличие CLI-команды не превращает `NOT RUN` в PASS.

## Доверенный bootstrap

Обычный установленный `hinstall` не создаёт prerequisite coordinator. Команда
`hinstall prerequisites capabilities --json` возвращает `configured=false`
с кодом `0`, то есть успешно сообщает отсутствие конфигурации. Остальные
prerequisite-команды без coordinator возвращают `CAPABILITY_UNSUPPORTED`, код `2`.
Ни `--trust-config`, ни plan JSON сами по себе не регистрируют OS installer.

Оболочка конкретного продукта создаёт coordinator из проверенного signed
catalogue, фиксированных adapters, original owner и приватного state directory,
затем передаёт готовый SDK через Python. Например, функция оболочки:

```python
from homensai_install.api.v1 import InstallerAPI
from homensai_install.cli import main


def run_product_cli(coordinator, *, state_dir, trust_config, argv=None):
    api = InstallerAPI.from_bootstrap(
        state_dir,
        trust_config=trust_config,
        prerequisites=coordinator,
        locale="ru",
    )
    return main(argv, api=api)
```

`coordinator`, public-key pins и пути в этой функции — настройки доверенного
кода хоста. CLI не принимает имя Python-модуля, путь к registry, shell command
или произвольный adapter. Каждое новое обращение оболочки подключает тот же
original owner/coordinator state directory и нужную версию подписанного каталога.

При переданном `api` флаги `--state-dir`, `--trust-config`,
`--development-runtime`, `--allow-development`, `--docker-cli-config`
отклоняются с `INVALID_REQUEST`. Конфигурация переданного SDK не подменяется
аргументами процесса. Такой injection не используется для `gui` или
`init-adapter`: эти команды имеют собственный bootstrap и явно отклоняют `api`.

Если `--locale` опущен, prerequisite plan и error envelope используют
`api.locale`; обычный CLI по умолчанию использует `ru`. Явный `--locale ru|en|de`
передаётся планированию и формированию ошибки без изменения общего SDK.
Подписанный текст `license` возвращается в исходной локали каталога; CLI его
не переводит. Остальная product configuration принадлежит переданному SDK.

Ниже `product-installer` означает установленную оболочку продукта,
вызывающую `main(..., api=api)`. Это не имя дополнительного executable,
автоматически создаваемого модулем.

## Фиксированные команды и входы

Каждая команда принимает `--json` и `--locale`. Аргументы JSON-файлов ограничены
общим лимитом 2 MiB и проверкой глубины/числа узлов; duplicate keys, NaN и
неподходящий тип объекта отвергаются. Значения аргументов и содержимое файлов
не включаются в сообщение об ошибке parser.

| Команда после `prerequisites` | Входы и результат |
|---|---|
| `identity` | Product, target, owner и digests зарегистрированного coordinator |
| `capabilities` | Фактическая регистрация и поддерживаемые стратегии; без неё `configured=false` |
| `license COMPONENT` | Проверенный текст условий и его связанная metadata |
| `plan` | `--choices FILE`, `--offline`, `--local-artifacts FILE`; возвращает план без согласия и запуска |
| `start OPERATION --plan FILE` | Новый durable operation; повторяемый `--consent-ref REF` содержит фактически полученные подтверждения |
| `status OPERATION` | Сохранённый результат и консервативная оценка controller; без исполнения |
| `operations` | `--all`, `--limit N`, `--after-id ID`; сводки exact owner/product/target/catalog |
| `events OPERATION` | `--after-sequence N`, `--limit N`; события и актуальный `snapshot` |
| `attempts OPERATION COMPONENT` | `--after N`, `--limit N`; история попыток и их receipts |
| `recheck OPERATION` | Синхронные read-only probes и journal; ничего не скачивает и не устанавливает |
| `resume OPERATION` | Явное синхронное продолжение; необязательный повторяемый `--consent-ref REF` |
| `cancel OPERATION` | Сохранить запрос отмены на безопасной границе |
| `choose-boundary OPERATION now\|later` | Записать выбор reboot/relogin; сам переход не выполняется |
| `replace-plan OPERATION --plan FILE` | Новый просмотр плана и новые `--consent-ref REF`; исполнения нет |

`OPERATION` использует публичный формат `[a-z][a-z0-9_-]{0,79}`. Для каждого
компонента `--choices` задаёт `auto`, `manual`, `use` или `skip`; component IDs
берутся из зарегистрированного каталога. `--local-artifacts` — JSON object
`artifact_id → абсолютный путь`; точные signed pins остаются обязательны.
Произвольные argv, доверенный bool `verified` или иная конфигурация из этих
файлов не принимаются как право исполнить installer.

`--limit` по умолчанию равен `100`: для `operations` допустимо `1..100`, для
events/attempts — `1..1000`. Курсоры sequence/attempt неотрицательны.
`operations` исключает `READY`/`CANCELED`, если не передан `--all`; следующая
страница начинается с `next_cursor`, переданного как `--after-id`.

Файл `--plan` содержит либо исходный SDK plan object, либо ровно успешный
CLI envelope с полями `api_version`, `request_id`, `result`. Поэтому stdout
команды plan можно сохранить и передать start/replace-plan:

```sh
product-installer prerequisites plan --choices choices.json --json > reviewed-plan.json
product-installer prerequisites license COMPONENT --json
product-installer prerequisites start OPERATION --plan reviewed-plan.json --consent-ref ACTUAL_CONSENT_REF --json
```

В примере `COMPONENT`, `OPERATION` и `ACTUAL_CONSENT_REF` заменяются реальными
значениями. `--consent-ref` повторяется для каждого обязательного подтверждения,
полученного после показа плана и точных условий. CLI не добавляет
`required_consents` автоматически и не имеет флага «принять всё».
Файл с `error`, дополнительными полями envelope или другой API version
не считается успешным планом. Сам план повторно проверяет общий coordinator.

У `resume` отсутствие `--consent-ref` сохраняет ранее записанные согласия;
явно переданные ссылки повторно проверяются SDK. `replace-plan` требует
согласий нового плана и не запускает installer самостоятельно.

## Завершение процесса и коды возврата

Start/resume всегда вызываются с `background=False`. CLI не возвращается,
пока общий drive не достигнет результата, паузы или границы восстановления.
Опции `--background` нет. Установка не оставляется в daemon thread, который
погибнет сразу после выхода краткоживущего CLI.

stdout содержит ровно один JSON envelope с `api_version`, `request_id` и
`result` либо `error`. `--help` — отдельный человеческий режим. Shell-код нужно
читать вместе с типом команды и JSON, а не интерпретировать всякий `0` как
готовность prerequisites или установленного продукта.

| Код | Значение |
|---|---|
| `0` | Для start/resume/recheck/status/events: только `status=READY` и `ready=true` без записанной ошибки. Для read/plan/control-команд — успешный запрос, как описано ниже |
| `1` | `RECOVERY_REQUIRED`, failed, записанная ошибка операции или неизвестное/неполное состояние, которое нельзя объявить готовностью |
| `2` | Структурированный отказ: неверные args/JSON, отсутствие coordinator, неподтверждённые consents, чужая identity и другие `InstallerError` |
| `3` | Незавершённый результат: `RUNNING`, `QUEUED`, `AWAITING_USER`, `REBOOT_REQUIRED`, `RELOGIN_REQUIRED` без записанной ошибки |
| `4` | `CANCELED` для start/resume/recheck/status/events |

Identity, capabilities, license, plan, operations и attempts возвращают `0`
при успешном чтении/планировании. Например, список может содержать операции,
требующие recovery, а `capabilities` может сообщать `configured=false`.

Cancel, choose-boundary и replace-plan возвращают `0`, если control request
принят. Это не готовность: cancel может сохранить `wait_for_safe_boundary`,
choose-boundary — оставить `REBOOT_REQUIRED`/`RELOGIN_REQUIRED` с
`host_action.performed=false`, replace-plan — вернуть `AWAITING_USER`.
Потребитель проверяет `status`, `ready`, `required_action`, `error`, `execution`
и продолжает polling. Для events outcome берётся из `snapshot`.

Отмена не убивает неизвестный installer и не удаляет общие компоненты. Если
другой controller ещё исполняет installer, CLI сохраняет cancel request и
возвращается, а работа останавливается на безопасной границе.

## Обрыв, повтор и приватность

После потери ответа используется прежний operation ID. Повтор start с тем же
планом читает существующую запись и не возобновляет effects. Для продолжения
нужен явный resume. Status/events/operations также ничего не исполняют.

При `BaseException` или завершении процесса синхронный CLI не сообщает
фиктивный JSON success: durable intent остаётся в общем journal. После
возобновления status может показать `RECOVERY_REQUIRED`; `INSTALLING`/`UNKNOWN`
и точные receipts сохраняются до reconciliation. Отсутствие controller не
доказывает, что внешний installer ничего не сделал.

Повторный синхронный start читает старую запись внутри controller lock.
В таком snapshot после обрыва состояние может остаться `RUNNING` с
`execution.state=unknown`, код `3`. Это не новый запуск; отдельный status после
освобождения lock показывает доступную оценку восстановления. Не следует
создавать новый operation ID, чтобы обойти эту границу.

Список operations не содержит private plan, consent refs, локальные пути,
dispatch token или детальные receipts. Он фильтруется до pagination по
original owner/product/target/catalog. Каталог другой версии не показывает
старую операцию; её активный claim при этом остаётся. Bootstrap восстанавливает
прежний подписанный каталог и state directory, не снимает claim догадкой.

## Проверка transport

`tests/test_prerequisites_cli.py` использует signed synthetic catalogue,
настоящий coordinator/SQLite и инъецированный платформенный fixture. Проверяются
CLI lifetime, consent refusal, paused/error codes, повтор start, durable intent
после обрыва, приватность, bounded JSON, bootstrap boundary и error redaction.
Это source contract, а не приёмка реальных OS installers или installed wheel.

```sh
python -m pytest -q tests/test_prerequisites_cli.py
```
