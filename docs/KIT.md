# Установочный комплект продукта

© 2026 Serhii Khomenko · HomenS.AI

Сборщик `python -m shared.kit` превращает проект продукта (JobPilot, Rent и
др.) в **единый подписанный установочный комплект** для Linux x86-64 и
Windows x86-64. Комплект содержит всё нужное для работы без сети и без
установленного Python: переносимый CPython 3.14, зависимости продукта,
установленные заранее для каждой платформы, сам установщик со своим Python
и сценарии `setup.sh` / `setup.cmd`.

| Цель | Службы | Права | Обновление и откат |
|---|---|---|---|
| `linux-x86_64` | systemd пользователя, загрузчик pointer-v2 | обычный пользователь | транзакция API `run-pointer-linux-update` + консолидация |
| `windows-x86_64` | службы Windows через WinSW 2.12.0 (LocalSystem, автозапуск) | администратор | транзакция `UpdatePlan` с файловыми портами |

Docker-профиль в комплект не входит (см. «Ограничения»).

## 1. Подключение проекта

```sh
# в каталоге модуля установки, Python 3.14 с cryptography
python -m shared.kit init /путь/к/проекту --product-id acme-app --product-name "Acme" --port 8800
python -m shared.kit keygen ~/.secrets/acme-keys          # один раз; закрытый ключ хранить вне репозитория
```

`init` создаёт в проекте `homensai-kit.json` и шаблон манифеста
`installer/manifest.json`. Дальше нужно:

1. Положить lock-файлы зависимостей `installer/linux.lock` и
   `installer/windows.lock` в строгом формате pip (`name==version
   --hash=sha256:...` для каждой строки). Подойдут существующие lock продукта
   (например, `requirements/rent-runtime.lock` в Rent).
2. Загрузить wheels: `python -m shared.kit fetch --config проект/homensai-kit.json`
   (единственный шаг, которому нужна сеть; выполняется у издателя). Wheels
   выбираются только по хэшам lock и совместимости с CPython 3.14 цели.
3. Положить в `installer/inputs/` архивы Python Build Standalone (Linux и
   Windows, `install_only_stripped`) и для Windows — `WinSW-x64.exe` 2.12.0 с
   лицензией; закрепить их SHA-256 в `homensai-kit.json`.
4. Описать компоненты и миграции в шаблоне манифеста (раздел 3).

## 2. `homensai-kit.json`

```json
{
  "format": 1,
  "manifest": "installer/manifest.json",
  "sources": [{"from": "src", "to": "src"}, {"from": "migrations", "to": "migrations"}],
  "python_paths": ["src"],
  "targets": ["linux-x86_64", "windows-x86_64"],
  "locks": {"linux": "installer/linux.lock", "windows": "installer/windows.lock"},
  "wheelhouse": "installer/wheelhouse",
  "runtime": {
    "linux":   {"archive": "installer/inputs/cpython-3.14.10+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                "sha256": "4d3c16c5…", "origin": "https://github.com/astral-sh/python-build-standalone/…", "python_version": "3.14.10"},
    "windows": {"archive": "installer/inputs/cpython-3.14.10+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
                "sha256": "c1876a97…", "origin": "https://github.com/astral-sh/python-build-standalone/…", "python_version": "3.14.10"}
  },
  "winsw": {"exe": "installer/inputs/WinSW-x64.exe", "sha256": "05b82d46…", "license": "installer/inputs/LICENSE.txt"},
  "migrations": ["0001-initial", "0002-notes"],
  "modes": ["autonomous"],
  "estimates": {"data": 268435456, "config": 16777216}
}
```

| Поле | Смысл |
|---|---|
| `sources` | что из проекта попадает в выпуск (`from` — относительно конфига, `to` — путь в выпуске); `__pycache__`, `*.pyc`, `.git` пропускаются, ссылки запрещены |
| `python_paths` | каталоги выпуска, добавляемые в `sys.path` перед зависимостями |
| `targets` | платформы комплекта; для каждой нужны `runtime.<os>`, для Windows — `winsw` |
| `wheelhouse` | каталог с wheels (может быть абсолютным путём) |
| `migrations` | **полная упорядоченная история** идентификаторов миграций; установщик выполняет при обновлении те, что ещё не применены |
| `modes` | режимы по умолчанию; иначе первый режим манифеста |
| `estimates` | ожидаемый объём `data`, `config` (и при желании `backup`) в байтах |
| `glibc` | максимальная версия manylinux для Linux wheels, по умолчанию `2.28` |

Точные значения хэшей PBS и WinSW, проверенные 03.11.2026, — в
[examples/kit_demo/homensai-kit.json](../examples/kit_demo/homensai-kit.json) и
`tests/run_kit_windows_e2e.py`. Хэш с той же страницы GitHub не является
независимой проверкой происхождения; для выпуска нужна отдельная проверка.

## 3. Шаблон манифеста

Это манифест v2 ([MANIFEST](MANIFEST.md)) без секции `runtime`: её формирует
сборщик (lock-файлы и wheels с хэшами). Если `paths` и `compatibility` не
заданы, подставляются стандартные (`app`, `releases`, `data`, `backup`,
`services`, `config`, `runtime` в корне установки; цели из `targets`).

Команды компонентов и миграций удобно задавать сокращением `@module`:

```json
{"name": "web", "cmd": ["@module", "acme.web", "--data-dir", "@data", "--port", "8800"], "after": [], …}
"migrations": {"cmd": ["@module", "acme.migrate", "--data-dir", "@DATA_DIR@", "--migration", "@MIGRATION_ID@"], …}
```

Сборщик раскрывает его в запуск `homensai_entry.py` выпуска: модуль
выполняется встроенным Python с `-I -B`, в `sys.path` добавляются
`python_paths` и `pydeps/<платформа>`, а в окружении доступны
`HOMENSAI_PRODUCT_ID`, `HOMENSAI_RELEASE` (версия выпуска) и
`HOMENSAI_RELEASE_DIR`. Обозначения `@data`, `@app`, `@runtime`,
`@release` работают на обеих платформах.

## 4. Требования к продукту

- Web-компонент слушает только `127.0.0.1:<web.port>` и на `web.healthz`
  отвечает HTTP 200 с JSON ровно
  `{"product": HOMENSAI_PRODUCT_ID, "release": HOMENSAI_RELEASE, "healthy": true}`.
- Все изменяемые данные — в каталоге из `--data-dir @data`; выпуск только
  читается.
- **Первая установка не запускает миграции**: продукт сам создаёт актуальную
  схему при первом старте, а все миграции из `migrations` считаются
  применёнными. При обновлении выполняются только новые, перед каждой —
  отдельная копия. Миграции идемпотентны и транзакционны.
- Корректное завершение по SIGTERM (Linux) и по Ctrl+C / остановке службы
  (Windows) в пределах 30 секунд.
- Набор компонентов и runtime (архив PBS) между версиями не меняется;
  установщик проверяет это до любых изменений. Сравнивается и фактический
  состав выбранных при установке режимов (порядок запуска по `after`): выпуск,
  который исключает компонент из выбранного режима, отклоняется
  (`kit.components_changed`) ещё до остановки служб — иначе исключённая служба
  продолжала бы писать во время архива и миграций.
- Для цели `windows-x86_64` хуки `pre_start`, `health_checks` и
  `post_update` должны быть пустыми: сборщик отклоняет такой комплект, а
  установщик Windows — подписанный выпуск с хуками до любых изменений
  (`kit.windows_hooks_unsupported`).
- Секреты не помещаются в манифест, команды и файлы выпуска.

Эталон — [examples/kit_demo](../examples/kit_demo): web и worker, двоичная
зависимость cffi, миграции SQLite.

## 5. Сборка и проверка

```sh
python -m shared.kit build --config проект/homensai-kit.json --version 1.4.0 \
    --signing-key ~/.secrets/acme-keys/publisher-ed25519.pem --output dist/
python -m shared.kit verify dist/acme-app-1.4.0 --expect-key-sha256 <отпечаток>
```

Ключ можно зашифровать (`keygen --passphrase-env NAME`, затем
`build --passphrase-env NAME`). Результат — каталог и `acme-app-1.4.0.tar.gz`:

```
setup.sh, setup.cmd                 запуск установщика (Linux / Windows)
kit.json, README.txt, SHA256SUMS    сведения о выпуске и контрольные суммы
package/acme-app-1.4.0.zip, .sig    подписанный пакет (Ed25519) и подпись
package/publisher-ed25519.pub       открытый ключ издателя
package/inventory.json              перечень состава: файлы, зависимости, лицензии
installer/app/                      код установщика (shared, клиент)
installer/<платформа>/python/       Python установщика с закреплённой cryptography
```

Подписанный пакет содержит выпуск: исходники, `pydeps/linux-x86_64` и
`pydeps/windows-x86_64` (распакованные wheels), сами wheels и lock-файлы,
`runtime-linux` (минимальный профиль PBS с загрузчиком pointer-v2) и
`runtime-windows`, рецепт служб и WinSW.

Отпечаток ключа издателя (`public_key_sha256` в `kit.json`) публикуйте по
независимому каналу: пользователь сверяет его при установке, а последующие
обновления принимаются только с тем же ключом. Смену ключа пользователь
подтверждает явно: `update --accept-key-sha256 <новый отпечаток>`; после
успешного обновления закрепляется новый ключ, а каждый сохранённый выпуск
проверяется ключом, которым он был подписан.

История миграций берётся не из `kit.json`, а из подписанного
`homensai-release.json` внутри пакета. `kit.json`, `SHA256SUMS` и код
установщика в комплекте не подписаны: их целостность обеспечивает канал
доставки архива. При строгих требованиях обновление можно запускать
сохранённым, уже доверенным установщиком: `<каталог состояния>/installer/setup.sh
--kit /путь/к/новому/комплекту update`.

## 6. Установка у пользователя

Linux (обычный пользователь, нужен `systemctl --user`):

```sh
tar -xzf acme-app-1.4.0.tar.gz && cd acme-app-1.4.0
./setup.sh install [--root DIR] [--modes a,b] [--expect-key-sha256 HEX] [--language ru]
./setup.sh status
```

Windows (командная строка **от имени администратора**):

```bat
tar -xzf acme-app-1.4.0.tar.gz && cd acme-app-1.4.0
setup.cmd install [--root "C:\Program Data\Acme"] [--language ru]
setup.cmd status
```

По умолчанию: Linux — `~/.local/share/homensai/<product_id>`, состояние
установщика `~/.local/state/homensai/<product_id>`; Windows —
`%ProgramData%\HomenSAI\<product_id>` и `%ProgramData%\HomenSAI\.state\<product_id>`.
На Windows эти каталоги принадлежат Administrators, наследование снято, в
DACL только SYSTEM и Administrators; заранее созданный чужой каталог
отклоняется (иначе обычный пользователь мог бы подменить файлы, которые
потом исполняет администратор). Одновременно допускается один запуск
установщика на продукт (блокировка `setup.lock`).

Службы Windows называются `homensai.<product_id>.<компонент>` (например,
`homensai.acme-app.web`); обёртка WinSW лежит в `<корень>\app\services\` под
тем же именем. Точка не входит ни в `product_id` (`[a-z0-9][a-z0-9_-]*`),
ни в имя компонента (`[a-z][a-z0-9_-]*`), поэтому ID разных пар продукт/компонент
не совпадают (прежняя схема `<product_id>-<компонент>` давала один ID для
`audit-a`/`b-web` и `audit-a-b`/`web`). Перед регистрацией, запуском,
остановкой и удалением установщик читает `ImagePath` уже существующей службы
из реестра SCM (`HKLM\SYSTEM\CurrentControlSet\Services\<ID>`) и действует
только если он указывает на обёртку этой установки; чужая служба с тем же ID
не изменяется, команда отклоняется до любых действий
(`kit.windows_service_foreign`). Установки, сделанные сборками со старой схемой
ID, нужно удалить прежним установщиком и установить заново.

В каталоге состояния хранятся ключи
операций, копии установленных пакетов и сохранённый установщик
(`installer/setup.sh` / `setup.cmd`) для `status` и `uninstall` без исходного
комплекта.

Обновление запускается из комплекта **новой** версии:

```sh
HOMENSAI_ARCHIVE_PASSWORD=… ./setup.sh update     # Linux; без переменной пароль запрашивается
set HOMENSAI_ARCHIVE_PASSWORD=… & setup.cmd update  &:: Windows
```

Перед заменой выпуска создаётся проверенный зашифрованный полный архив
(AES-256-GCM, пароль не сохраняется — храните его отдельно). Если запуск
или проверка здоровья новой версии не удались, данные восстанавливаются из
архива и запускается прежняя версия (код выхода 4). На Windows перед архивом
останавливается весь выбранный набор служб прежней версии (по её собственному
манифесту), а откат запускает прежнюю версию с её командами. На Linux после
транзакции установщик выполняет консолидацию: удаление с сохранением данных и
переустановку активной версии, чтобы следующая транзакция начиналась с
чистого состояния (службы кратко перезапускаются).

Удаление: `uninstall` — с сохранением данных, копий и настроек;
`uninstall --purge` — полностью, с вводом ID продукта. Повторная `install`
той же версии в тот же корень использует сохранённые данные.

Если после сбоя обновление было начато, повторный `update` сначала читает
состояние транзакции: незаписанная транзакция запускается заново тем же
ключом, завершённая — только учитывается, начатая — продолжается
(`--reconcile-stop` — после потерянного подтверждения остановки).

Коды выхода: `0` — успех, `1` — ошибка (повтор той же команды продолжает
прерванную операцию с сохранёнными ключами), `2` — неподдерживаемая система
или аргументы, `3` — отменено пользователем, `4` — обновление откатилось.

## 7. Проверки

- Модульные: `tests/test_kit.py`, `tests/test_kit_setup.py`, `tests/test_kit_windows.py`.
- Отладка: `HOMENSAI_SETUP_DEBUG=1` печатает трассировку непредвиденной ошибки
  установщика; `HOMENSAI_API_TRACE=/абсолютный/файл` записывает исключения
  модулей `shared` в процессе API (только для разработки; сообщения могут
  содержать пути).
- Linux, чистый Debian без Python и без сети, реальный user-systemd:
  `python3 tests/run_kit_e2e.py --run` (установка с прерыванием, 1→2,
  откат, 2→3, удаление с сохранением, переустановка, полное удаление).
- Windows 10 в ВМ dockur/windows с KVM и агентом заданий:
  `python3 tests/run_kit_windows_e2e.py --run` (тот же сценарий со службами
  WinSW). Подготовка ВМ — раздел 8.

## 8. Тестовая Windows-ВМ

Нужны `/dev/kvm`, Podman (или Docker) и ~60 ГБ места. В каталоге
`~/.cache/homensai-winvm` создаются `oem/install.bat` (регистрирует агент,
создаёт задачу `HSIAgent` при входе), `oem/agent.ps1` (выполняет
`\\host.lan\Data\jobs\*.ps1` и пишет `.rc`) и пустой `shared/jobs`:

```sh
podman run -d --name homensai-winvm -e VERSION=ltsc10 -e RAM_SIZE=3G -e CPU_CORES=2 \
  --device /dev/kvm --device /dev/net/tun --cap-add NET_ADMIN -p 127.0.0.1:8006:8006 \
  -v ~/.cache/homensai-winvm/storage:/storage -v ~/.cache/homensai-winvm/oem:/oem \
  -v ~/.cache/homensai-winvm/shared:/shared docker.io/dockurr/windows:latest
```

Экран установки — http://127.0.0.1:8006. Это тестовая среда разработчика,
не приёмка на чистой физической системе.

## 9. Ограничения

- Замена runtime (другой архив PBS) и изменение набора компонентов, в том
  числе состава выбранных режимов, при обновлении не поддерживаются — нужна
  переустановка с переносом архива.
- На Windows хуки `pre_start`, `health_checks`, `post_update` не
  поддерживаются: непустые хуки отклоняются при сборке комплекта с целью
  `windows-x86_64` и до любых изменений при установке и обновлении (поздние
  проверки в эффектах транзакции оставлены как защита). Службы работают от
  LocalSystem.
- На Windows установщик не перехватывает службу, ID которой занят другой
  программой (проверка `ImagePath`); конфликт устраняет администратор.
- Docker-комплект не реализован: подписанный Compose-план, start/stop и
  health есть в API ([DOCKER-ADAPTER](DOCKER-ADAPTER.md)), обновление и
  восстановление контейнеров — нет. Нативный комплект покрывает те же ПК без
  Docker.
- Подпись подтверждает издателя и целостность, но не отсутствие вредоносного
  кода; лицензии runtime и зависимостей требуют отдельной проверки
  (`package/inventory.json`).
- Секреты продукта (`secrets[]`) комплект не запрашивает и не передаёт.
