# Сборка, dependency lock и происхождение артефактов

Документ описывает рабочий процесс разработчика HomenSAI Installer. Конечный
пользователь должен получать отдельный проверенный установочный пакет для своей
платформы. Установка Python, pip или build tools из этого документа на компьютере
конечного пользователя не является условием работы будущего установщика.

## 1. Что закреплено и что проверено

`requirements-build.lock` содержит полное замыкание выбранных зависимостей сборки
и тестирования: **17 точных версий и 22 SHA-256 wheel-файлов**. В Linux активны
14 пакетов; в Windows дополнительно нужны `colorama`, `pefile`, `pywin32-ctypes`.
Lock рассчитан на **CPython 3.14 с GIL, x64, Linux с glibc и Windows**. ARM64,
macOS, musl/Alpine, PyPy, free-threaded CPython и исходные sdist в него не входят.

На 2026-10-25 выполнено следующее:

| Проверка | Фактический результат | Предел результата |
| --- | --- | --- |
| Официальная PyPI JSON metadata точных версий | Получена для всех 17 пакетов | Метаданные и HTTPS не заменяют доверие к издателю |
| Выбранные wheel-файлы | Все 22 скачаны с `files.pythonhosted.org`, проверены размер и SHA-256 | Колёса Windows прочитаны как ZIP; их код не запускался |
| Верхний `*.dist-info/METADATA` каждого wheel | Хеш сохранён, зависимости сопоставлены с PyPI | Встроенные native/Rust библиотеки требуют своего аудита |
| PEP 508 closure | Проверена для Linux/Windows и Python 3.14.3/3.14.9 | Это вычисление маркеров, не запуск Python 3.14.9 или Windows |
| Текущий build venv | Linux CPython 3.14.3, все 14 версий совпадают с lock | Это фактически использованный DEV toolchain |
| CI Python 3.14.9 | Версия найдена в официальном `actions/python-versions` для Ubuntu 24.04 и Windows x64 | Workflow ещё должен быть реально исполнен в GitHub |
| Лицензионный production gate | `BLOCKED`, отдельный аудит не завершён | Хеш, SBOM или наличие LICENSE не являются юридическим одобрением |

Проверяемые сведения лежат рядом с исходниками:

- `packaging/build-dependencies.json`: PyPI URLs, SHA сырой metadata, точные wheel
  URLs, размеры, SHA-256, wheel METADATA, target markers и хеши найденных notices.
- `packaging/ci-toolchain.json`: официальные tag → commit для двух GitHub Actions,
  blob SHA их `action.yml` и LICENSE, выбранная запись Python 3.14.9.
- `packaging/runtime-source-receipt.json`: восстановленный ожидаемый SHA ранее
  выбранного CPython-архива и явные границы исторической проверки.
- `tools/check_build_lock.py`: offline проверка согласованности, closure,
  установленных build distributions и, по запросу, скачанных wheel-файлов.
- `tools/build_receipt.py`: запись observed inputs/outputs в локальный JSON.

Диапазоны в `pyproject.toml` остаются контрактом совместимости SDK. При сборке
этого проекта выбираются версии из lock. Успешное разрешение `>=...` в произвольный
день не считается воспроизводимой сборкой.

### Зафиксированные версии

| Пакет | Версия | Использование |
| --- | --- | --- |
| `build` | 1.6.1 | Build frontend |
| `setuptools` | 84.2.1 | PEP 517 backend; wheel command входит в выбранную версию |
| `pyproject-hooks` | 1.3.3 | Связь frontend с backend |
| `packaging` | 26.3 | Versions, markers, wheel tags |
| `pyinstaller` | 6.22.3 | DEV frozen launcher; отдельная лицензионная область bootloader |
| `pyinstaller-hooks-contrib` | 2026.9 | Hooks при freeze |
| `altgraph` | 0.17.5 | Зависимость PyInstaller |
| `cryptography` | 50.0.2 | Проверка подписей и архивная криптография |
| `cffi` | 2.1.1 | Зависимость cryptography |
| `pycparser` | 3.12 | Зависимость CFFI |
| `pytest` | 9.1.1 | Тесты |
| `iniconfig` | 2.3.1 | Зависимость pytest |
| `pluggy` | 1.7.0 | Зависимость pytest |
| `pygments` | 2.21.0 | Зависимость pytest |
| `colorama` | 0.4.6 | Только Windows, build/pytest |
| `pefile` | 2024.8.26 | Только Windows, PyInstaller |
| `pywin32-ctypes` | 0.2.3 | Только Windows, PyInstaller |

`pip` здесь является bootstrap-инструментом создания build venv. Его фактическая
версия записывается в build receipt; отдельный pin в этом lock не задаётся. Сам
CPython, его встроенные библиотеки, системный linker, glibc, runner image и Node
в GitHub Actions также являются отдельными входами. Поэтому этот этап фиксирует
**Python build dependencies**, но не заявляет hermetic или побитно повторяемую
сборку целого native installer. Для такого заявления нужны зафиксированный образ
сборщика, runtime/source toolchain и независимая повторная сборка с сравнением.

## 2. Сборка на Linux

Нужен доверенный CPython 3.14 x64 с GIL. Для нового CI выбрана версия 3.14.9;
локальные ранее выполненные проверки относятся к 3.14.3. Не используйте
произвольный неизвестный `python` из каталога скачанного приложения.

Из корня репозитория, в чистом рабочем дереве:

```bash
python3.14 --version
python3.14 -m venv .venv-build
.venv-build/bin/python -m pip --isolated install --require-hashes --only-binary=:all: -r requirements-build.lock
.venv-build/bin/python -m pip --isolated check
.venv-build/bin/python tools/check_build_lock.py --installed
.venv-build/bin/python -m pytest -q tests
.venv-build/bin/python -m pytest -q integration_tests
.venv-build/bin/python -m build --wheel --no-isolation --outdir dist .
.venv-build/bin/python -m build --wheel --no-isolation --outdir reference/dist reference
.venv-build/bin/python tools/build_receipt.py --artifact-dir dist --artifact-dir reference/dist --output build/receipts/source-build.json
```

`tests/` запрещает сетевые обращения. `integration_tests/` запускает настоящие
синтетические процессы и разрешает только loopback в тестируемом Python-процессе.
Дочерний fixture server тоже слушает loopback; эта fixture-политика не является
системным firewall или sandbox для произвольного стороннего кода.

Внешние пакеты устанавливаются только в новый venv. `--require-hashes` действует
для всех зависимостей; `--only-binary=:all:` исключает незафиксированные source
builds. `--no-isolation` заставляет build использовать заранее проверенный
backend из этого venv, а не создавать другой build environment с новыми
разрешёнными версиями. Основание: [pip secure installs][pip-secure],
[build CLI][build-cli].

Сначала должен успешно завершиться `check_build_lock.py --installed`. Он
отказывает при несовпадении версии или неожиданной distribution в build venv.
SDK не нужно предварительно устанавливать через `pip install -e .`: тесты
выполняются из checkout. Если SDK уже установлен в этом venv, создайте чистый
build venv, чтобы не смешивать проверку проекта и окружения сборки.

Для DEV-работы с незакоммиченными изменениями можно явно добавить
`--allow-dirty-dev` к `build_receipt.py`. JSON сохранит `dirty: true` и отметит,
что untracked source files не входят в Git-tracked tree receipt. Это не
production source provenance и не заменяет следующий checkpoint в Git.

## 3. Сборка и переносимые source checks на Windows

Эти команды предназначены разработчику на настоящем Windows x64 с доверенным
CPython 3.14.9. Они не устанавливают службы, базы данных или Docker Desktop.
Пример PowerShell:

```powershell
py -3.14 --version
py -3.14 -m venv .venv-build
& .\.venv-build\Scripts\python.exe -m pip --isolated install --require-hashes --only-binary=:all: -r requirements-build.lock
& .\.venv-build\Scripts\python.exe -m pip --isolated check
& .\.venv-build\Scripts\python.exe tools/check_build_lock.py --installed
& .\.venv-build\Scripts\python.exe -m pytest -q tests/test_manifest.py tests/test_manifest_v2.py tests/test_i18n.py tests/test_trust.py tests/test_render_docker.py tests/test_windows_ps.py tests/test_build_lock.py tests/test_build_receipt.py
& .\.venv-build\Scripts\python.exe -m build --wheel --no-isolation --outdir dist .
& .\.venv-build\Scripts\python.exe -m build --wheel --no-isolation --outdir reference/dist reference
& .\.venv-build\Scripts\python.exe tools/build_receipt.py --artifact-dir dist --artifact-dir reference/dist --output build/receipts/source-build.json
```

В интерактивной консоли переходите к следующей команде только после успешного
завершения предыдущей. В CI это проверяется через `$LASTEXITCODE`.

Golden-файлы PowerShell должны оставаться с checked-in LF. Новый Windows CI job
задаёт `core.autocrlf=false` **только на одноразовом hosted runner до checkout**.
Локальная инструкция не меняет глобальную Git-конфигурацию разработчика.

Набор проверяет manifest/schema, локализацию, Ed25519-проверку дерева, текстовые
renderers и build metadata. `test_windows_ps.py` проверяет текст PowerShell, а
не выполнение сгенерированного сценария. Прохождение такого job не подтверждает
UAC, WinSW/SCM, ACL, DPAPI, автозапуск, перезагрузку или установку на чистую Windows.

CP020 расширяет этот профиль `tests/test_process_liveness.py`,
`tests/test_local_artifacts.py` и
`integration_tests/test_windows_owner_liveness.py`: проверяются staging на
синтетических байтах и безопасное наблюдение собственных Windows-процессов.
Нулевой сигнал Windows не используется для проверки PID; ожидающий handle
проверяется через `OpenProcess(SYNCHRONIZE)` и `WaitForSingleObject(..., 0)`.
Это дополнение вызвано воспроизведённым дефектом, а не попыткой исправить
историческую недоступность CI. Windows Server hosted runner не заменяет Windows11.

Для изолированных Linux source-проверок добавлен
`packaging/Dockerfile.verification`. Он закрепляет digest базового CPython image
и Python wheels из hash-lock. Пакеты Debian/Node/C compiler остаются отдельными
входами DEV-среды; побитовая воспроизводимость всего image не заявляется.
Включённый compile probe предотвращает повтор среды с gcc без libc headers.
`packaging/Dockerfile.verification-browser` добавляет Chromium и Playwright
для `packaging/check_prerequisite_upload_browser.py`; это среда проверки
установленного wheel с синтетическими prerequisites, не продуктовый Docker backend.

## 4. Работа без сети после подготовки wheelhouse

На доверенной машине **той же целевой платформы** можно заранее получить пакеты:

```bash
python -m pip --isolated download --require-hashes --only-binary=:all: -r requirements-build.lock --dest build/wheelhouse
python -m pip --isolated install --no-index --find-links build/wheelhouse --require-hashes --only-binary=:all: -r requirements-build.lock
```

Первая команда требует интернет, вторая использует локальные wheels и всё равно
проверяет их SHA-256. Windows-only markers должны вычисляться в Windows.
Параметр `pip --platform` сам по себе не является доказательством правильного
вычисления всех PEP 508 markers при cross-download из Linux.

Если подготовлен полный архив всех **22** выбранных файлов, его можно проверить
без импорта содержащегося в них кода:

```bash
python tools/check_build_lock.py --wheel-dir /absolute/path/to/all-selected-wheels
```

Эта опция проверяет весь snapshot. Обычный wheelhouse, полученный pip для одной
платформы, содержит только нужную этой платформе часть и не удовлетворяет
проверке полного набора из 22 файлов.

## 5. CPython runtime: восстановленный SHA и границы доказательства

В текущем историческом DEV reference использовался Python Build Standalone
20260325, CPython 3.14.3, Linux x86_64 glibc, `install_only_stripped`.
Официальный [release API][pbs-release-api] для asset `381127079` сообщает:

```text
filename: cpython-3.14.3+20260325-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
size:     36239204
sha256:   34e8e7ea5192308f0fe6d93becdf3eeb00a64a1fdbb4beead98d0edb93b68cdd
```

При подготовке документа заново прочитана официальная release metadata.
Архив для этой проверки не скачивался. SHA первоначальной исторической загрузки
не был сохранён, существующее распакованное дерево с новым архивом не сравнивалось.
Следовательно, **историческое происхождение имеющегося runtime не доказано**.
Новое совпадение файла с этим ожидаемым SHA сможет доказать проверку полученного
сейчас архива, но не исправит задним числом отсутствующую историческую запись.

Для нового runtime нужно сохранить до его исполнения:

1. Точный официальный URL, version/build/target/ABI и независимый ожидаемый SHA.
2. Размер и фактически полученный SHA всего архива, источник ожидаемого SHA.
3. Результат безопасного распаковывания и инвентарь файлов/ссылок.
4. Связь распакованного дерева с архивом, все намеренные exclusions и их причины.
5. Лицензионный и security review всех native/transitive компонентов.

`tools/build_receipt.py` умеет дополнительно проверить **новый локальный файл**:

```bash
python tools/build_receipt.py --artifact-dir dist --output build/receipts/source-build.json \
  --runtime-archive /absolute/path/to/reviewed-runtime.tar.gz \
  --runtime-sha256 REVIEWED_64_HEX_SHA256 \
  --runtime-source-url https://official.example/reviewed-runtime.tar.gz
```

Плейсхолдеры нужно заменить точными проверенными значениями. Receipt фиксирует
`archive_bytes_verified_now: true`, но сохраняет `historical_download_origin:
NOT_PROVEN` и `extracted_runtime_tree: NOT_COMPARED`. Скрипт не скачивает и не
запускает этот архив. URL является заявленным источником; само его наличие не
создаёт publisher attestation.

Python 3.14.3 оставлен как фактически испытанный DEV input. [Официальная страница
3.14.3][python-3143] указывает последующий maintenance release. CI выбран на
3.14.9; перенос runtime на него требует новой сборки, проверки native libraries,
лицензий и lifecycle tests. Конкретные CVE и отсутствие уязвимостей этим
документом не подтверждаются.

## 6. DEV reference bundle

`packaging/build_reference.py` сейчас обслуживает **Linux x64 web/SQLite
reference profile**. Он использует уже подготовленный доверенный runtime,
собирает reference wheel, при `--freeze` применяет PyInstaller и создаёт DEV
подпись для тестов. Production publisher key этот builder не выпускает.

Builder можно вызывать из другого рабочего каталога по абсолютному пути к
скрипту. PyInstaller запускается из проверяемого исходного дерева, чтобы сборка
ресурсов не зависела от каталога вызывающего процесса. После freeze builder
читает фактический архив исполняемого файла: набор и SHA-256 всех ресурсов SDK
должны совпадать с `tool.setuptools.package-data` этого дерева. Схема manifest,
локализации, мастер и офлайн-документация входят в эту проверку. Отсутствующий,
лишний или изменённый ресурс останавливает сборку; одного успешного завершения
PyInstaller недостаточно для `BUILD_REPORT.json`.

```bash
python packaging/build_reference.py \
  --runtime /absolute/path/to/verified-cpython-3.14-runtime \
  --output /absolute/path/to/new-reference-dev-bundle \
  --freeze
```

Runtime должен быть проверен **до передачи builder**: builder исполняет его для
получения версии. Наличие `LICENSE.txt` и версия `3.14.x` сами по себе не
проверяют источник. Этот вызов не является способом безопасно исследовать
неизвестный скачанный Python.

Profile исключает неиспользуемые компоненты вроде ensurepip и Tcl/Tk/dbm в
рамках конкретного SQLite web product. Это не универсальное разрешение удалять
те же зависимости из произвольного подключаемого проекта. Удаление Python
module не удаляет автоматически статически встроенную библиотеку из libpython.

Полный лицензионный инвентарь и незакрытые условия описаны в
[`THIRD_PARTY_AUDIT.md`](THIRD_PARTY_AUDIT.md). `check_licenses.py` является
отдельным fail-closed gate; его ещё необходимо привязать к production pipeline.
Сборка DEV bundle не переводит лицензии в `APPROVED`.

## 7. Что делает подготовленный GitHub Actions workflow

`.github/workflows/source-tests.yml` подготовлен для `push` в `main` и `codex/universal-installer-*`, обычного
`pull_request` и ручного `workflow_dispatch`. Он использует `contents: read`,
`persist-credentials: false`, не получает production signing keys и ничего не
публикует. В workflow нет package uploads, production release или автоматической
установки системных prerequisite packages.

Официальные tag refs и LICENSE прочитаны на конкретных commits:

| Action | Проверенный tag | Закреплённый commit | LICENSE |
| --- | --- | --- | --- |
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` | MIT |
| `actions/setup-python` | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` | MIT |

Это соответствует рекомендации GitHub закреплять полные commit SHA
([secure use reference][github-security]). Смена upstream tag не изменит эти
pins. Сам commit pin не доказывает отсутствие дефектов в action; изменения
требуют review. Источники и blob IDs сохранены в `ci-toolchain.json`.

Linux job выполняет весь `tests/`, затем реальные `integration_tests/` с
пользовательскими процессами/loopback. Windows job выполняет ограниченный
переносимый source subset. Оба собирают SDK/reference wheels без isolation и
создают локальный receipt. GitHub hosted runner image не закреплён по content
digest, поэтому job не считается hermetic сборщиком. Receipt остаётся на runner;
внешней публикации или автоматической выгрузки артефактов здесь нет.

Добавление файла workflow в репозиторий и наличие YAML не означают, что job уже
выполнился. До реального run нужны статусы **NOT RUN**. Если GitHub App не имеет
права записывать workflows, этот конкретный upload будет отдельным access
blocker; остальные исходники и документация от него не зависят.

## 8. Изменение lock и release gates

При обновлении зависимости выполняйте один reviewable checkpoint:

1. Выберите точную версию по официальному PyPI JSON endpoint и upstream release.
2. Сохраните SHA сырой metadata; проверьте `Requires-Python`, `Requires-Dist`,
   extras/markers и `yanked`. Добавьте полное замыкание для каждой целевой ОС.
3. Выберите только нужные ABI/architecture wheels. Получите официальные размеры
   и SHA-256; скачайте в отдельный staging и сравните bytes **до установки**.
4. Сопоставьте верхний wheel METADATA с index metadata. Vendor METADATA внутри
   setuptools не должен приниматься за METADATA самого setuptools.
5. Обновите snapshot и lock совместно. Изменившийся license/notice/transitive
   состав требует нового review; старый `APPROVED` по другим bytes не переносится.
6. Выполните checker, тесты, сборку wheel, CI на доступных платформах и сохраните
   receipts. В этом checkpoint зафиксируйте commit и следующий шаг восстановления.

Production release должен отказывать при UNKNOWN/UNAPPROVED, конфликте лицензий,
неполном runtime inventory, непроверенном происхождении исполняемых файлов и
непройденных обязательных platform gates. DEV signing, совпадающий SHA и успешные
unit tests не заменяют эти условия.

### Матрица дальнейшей приёмки

| Runner / среда | Что необходимо выполнить | Что не заменяет этот gate |
| --- | --- | --- |
| Чистая Windows x64 VM, native | UAC/ACL, управление службой, реальный install → launch → update → backup → restore → rollback, reboot/autostart, repair, uninstall с сохранением данных | Golden PowerShell или portable Windows source tests |
| Windows x64 + Docker Desktop/WSL2 | Согласия и prerequisites, реальный daemon/Compose, тома/права/порты, restart, обновление и сохранение данных | Проверка строки `docker --version` |
| Поддержанный Linux с service manager | Пользовательский и machine scopes, systemd/service ownership, autostart после reboot, жизненный цикл и удаление | Linux user-session process integration |
| Linux + Docker Engine | Реальные images/digests/Compose/health, постоянные volumes, backup/restore/update, uninstall без потери данных | Render Compose YAML или mock Docker client |
| Offline installer | Чистая VM без сети, runtime/dependencies/проверка подписей из пакета, отказ при неполноте | Сборка wheel из готового venv |
| Production supply chain | Reviewed licenses/notices/source obligations, trusted signing, key rotation/revocation, runtime security review, final exact inventory, independent rebuild where required | DEV key, unsigned build receipt или SBOM без review |

Эти gates должны записывать точный commit, artifact SHA, ОС/build, команды,
ожидаемые/фактические результаты и причины пропусков. Недоступная среда означает
`BLOCKED` или `NOT RUN` с конкретным основанием, а не `PASS`.

[pip-secure]: https://pip.pypa.io/en/stable/topics/secure-installs/
[build-cli]: https://build.pypa.io/en/stable/reference/cli.html
[github-security]: https://docs.github.com/en/actions/reference/security/secure-use
[pbs-release-api]: https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20260325
[python-3143]: https://www.python.org/downloads/release/python-3143/


## Наблюдение GitHub Actions на CP-004

Workflow был успешно записан и запущен на commit
`9e0cb1d1ede5c288fa9c43f4048d4d42194dc924`:
https://github.com/HomenSAI/homensai-install-modull/actions/runs/40170554823 .
Оба задания завершились `failure` до назначения исполнителя (`runner_id=0`,
`steps=[]`). Endpoint логов вернул404. Поэтому source tests в GitHub в этом run
**не исполнялись**, статус приёмкиCI — BLOCKED. Причина остановки не присутствует
в доступном ответе; не приписывать её биллингу, YAML или инструментам без evidence.
Receipt: `packaging/acceptance-evidence/github-ci-20261025-cp004.json`.
Локальные Linux проверки относятся к собственным receipts и этим run не заменяются.

## Проверка установленного wheel в CI

Job `linux-installed` создаёт build venv, wheelhouse и все клиентские каталоги
в `$RUNNER_TEMP`, вне checkout. После загрузки закреплённых wheel build и
установка используют `--no-index`. Клиенты запускаются вне исходников и
проверяют происхождение установленных модулей и неизменность package bytes.
Выполняются SDK/CLI, prerequisites и 16 reference profiles: последовательные
updates, семь update fault scenarios, четыре recovery scenarios и
`uninstall-restore`, `recovery-root-resume`, `repair` и `reinstall`.

`tools/summarize_installed_checks.py --include-fault-profiles` требует все
18 успешных квитанций с точным `GITHUB_SHA`, ожидаемым profile и одинаковым
SHA-256 SDK wheel. Missing/stale/FAIL, неправильный профиль или иной wheel
завершают job ошибкой. Отказ одного reference profile не пропускает остальные;
каждый использует собственный work directory и имя receipt. Общая сводка
выполняется также после отказа предыдущего шага.

`uninstall-restore` проверяет registered fresh v1→update v2→uninstall с
сохранением данных→restore snapshot v1. Source v2 исполняется из проверенной
копии вне заменяемых roots с прежним ledger и новым quiesce key. Отдельно
проверяются отказ при повреждённом retained receipt, настоящий полный
`before_restore` и сохранность данных, настроек, credentials и схемы.
Это Linux DEV acceptance, без закрытия release gates.

`recovery-root-resume` повторяет установленный lifecycle с реальным `SIGKILL`
после замены целого data root и до записи подтверждения. Новый public factory
продолжает исходную операцию. Проверяются отсутствие повторного копирования
data, единственный source quiesce, отсутствие source hooks на смешанной БД,
единственные target restore/post_restore и неизменность safety ciphertext.
Независимый decrypt/restore этого архива сравнивает все пять roots с полным
before-image, включая пустые каталоги. Этот профиль проверяет одну точку
прерывания целого root; он не доказывает mid-file или power-loss recovery.

`repair` и `reinstall` проверяют замену управляемого кода той же подписанной
версии и сохранение текущей схемы, пользовательских данных, настроек, ключей
и installation identity. Повреждённый исходный код не должен исполняться;
полный `before_maintenance` archive проверяется фактическим восстановлением.
`reinstall` включает возвращение после завершённого удаления с сохранёнными
данными и заново переданным проверенным candidate. Успех этих профилей
не означает приёмку Windows, Docker, служб или аппаратного отключения питания.

Четыре installed harness используют `SOURCE_DATE_EPOCH` из проверяемого commit
для ZIP timestamps. Совпадение отдельных wheel подтверждается их receipts;
герметичность всей сборки native installer этим не заявляется.
