# Лицензионный аудит DEV reference bundle 04

Автор проекта: Serhii Khomenko. Дата фактической проверки: **25 октября 2026 года**.

## Решение для этого артефакта

**Production release: BLOCKED.** Проверен существующий `reference-dev-bundle-04`, а не абстрактный список желаемых зависимостей. Найдены компоненты с дополнительными условиями распространения и пробелы в доставке notices/подтверждении происхождения. Ни один реальный компонент этой проверки не получил искусственный статус юридического `APPROVED`.

Самая существенная находка — **Berkeley DB 6.0.19 с лицензией Sleepycat**, статически включённая в `_dbm.cpython-314-x86_64-linux-gnu.so`. Проверены `_dbm.library == "Berkeley DB"`, строка версии в самом бинарном файле и текст лицензии из закреплённого upstream ref `20260325`. Лицензия разрешает использование и распространение при выполнении условий, включая доступность исходников DB и сопровождающего ПО, использующего DB. Это не запрет коммерческой деятельности; однако такая зависимость не получает автоматического разрешения для универсальной закрытой коммерческой поставки. Для минимального веб-примера с SQLite выбрано исключение `_dbm`/BDB из следующей сборки. Bundle 04 остаётся историческим артефактом с этим компонентом. [Первичный текст конкретной сборки](https://github.com/astral-sh/python-build-standalone/blob/20260325/LICENSE.bdb.txt), [идентификатор и условия Sleepycat](https://spdx.org/licenses/Sleepycat.html).

Дополнительно обнаружены **pip 25.3 внутри ensurepip**, несмотря на удаление установленного pip, и **libgcc_s.so.1 внутри frozen executable**. Обе находки меняют фактический состав распространяемого продукта. Удаление `site-packages/pip` не удаляет `ensurepip/_bundled/pip-25.3-py3-none-any.whl`; PyInstaller является инструментом сборки, но его bootloader, loader и runtime hooks входят в executable и рассматриваются отдельно.

Эта работа устанавливает факты, применяет консервативную политику проекта и фиксирует незакрытые условия. Она не подменяет решение правообладателя/уполномоченного reviewer и не присваивает юридическое одобрение на основании названия лицензии.

## Граница и воспроизводимые доказательства

Проверены:

- `work/reference-dev-bundle-04`: payload, frozen executable, manifest/inventory/control files; `build-work` отмечен как рабочая область сборки, которую нельзя незаметно включать в релиз.
- `work/installer-venv`: CPython 3.14.3, установленная metadata и license files **14 сторонних Python distributions**, PyInstaller 6.22.3, cryptography 50.0.2.
- `work/python-runtimes/cpython-3.14.3-linux-x86_64-gnu`: BUILD `20260325`, сохранённый CPython `LICENSE.txt`, реальные встроенные модули и native dependencies.
- CArchive frozen executable через `PyInstaller.archive.readers.CArchiveReader`: **43 записи**, извлечённые в память, без запуска executable и без изменения бинарных файлов.
- ELF `DT_NEEDED` посредством `readelf`, безопасное чтение version markers посредством `strings`, версии уже используемого доверенного runtime через Python API.
- Первичные лицензии/рецепты закреплённых upstream refs через read-only GitHub; license metadata точных установленных wheels; supplier SBOM cryptography.

Машиночитаемые материалы находятся в `packaging/audit-evidence/`:

| Файл | Содержание |
| --- | --- |
| `reference-dev-bundle-04.inventory.json` | 1471 выбранный файл с SHA-256 и 102 записи кандидатов для review; все реальные review states остаются `PENDING_REVIEW` |
| `reference-dev-bundle-04.receipt.json` | Версии, license-file hashes build environment, состав CArchive, recipe pins, runtime probes, vendor graph pip |
| `upstream-source-receipts.json` | 21 primary-source receipt: URL, ref, Git blob SHA, где сохранён — SHA-256 текста |
| `reference-dev-bundle-04.gate.json` | Реальное выполнение fail-closed проверки: `BLOCKED`, exit code 2, `production_eligible=false` |

102 записи — это консервативный review inventory, включающий build tools, потенциально встроенные native libraries, supplier Rust graph и vendor graph pip. Это **не утверждение**, что все 102 компонента независимо присутствуют в конечном машинном коде. Для proc-macro/build crates и условных native dependencies окончательный scope должен быть подтверждён анализом точного артефакта; неизвестность сохраняет блокировку.

Для четырёх значимых native entries frozen executable сохранены hashes:

| Встроенный файл | SHA-256 |
| --- | --- |
| `libpython3.14.so.1.0` | `e9870cb294d92ee8f4c90644ef825105115887d96bf1075d5673c0ba8f851f9a` |
| `cryptography/hazmat/bindings/_rust.abi3.so` | `1e5d23fa66915204b9b67202a46993844d81cda56f6ad52cead2def37617ee29` |
| `_cffi_backend.cpython-314-x86_64-linux-gnu.so` | `7d1eff7fbb874ee72228fd426103da2b9eef528979156077bdec736d5b4f61a2` |
| `libgcc_s.so.1` | `d93224d2b0dab4247598be683adca02f5cf00586f99c187579cd7e92058fb7cb` |

`uv python list` сообщил URL исходного runtime:

```text
https://releases.astral.sh/github/python-build-standalone/releases/download/20260325/cpython-3.14.3%2B20260325-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
```

Hash первоначально скачанного архива **не был сохранён**. Текущие файлы связаны с локальным signed inventory, но их криптографическая связь с исходным upstream archive этим не доказана. Нельзя заменить отсутствие download receipt утверждением «источник проверен». Для production следует получить проверяемый исходный архив/attestation, сохранить digest и сопоставить итоговые байты после документированных преобразований.

## Фактически распространяемые ключевые компоненты

| Компонент / версия | Факт присутствия и лицензия | Что остаётся выполнить |
| --- | --- | --- |
| CPython 3.14.3, PBS build 20260325 | Runtime и frozen libpython; полный сохранённый `LICENSE.txt`, recipe указывает `Python-2.0`/`CNRI-Python` | Сохранить весь файл с историческими notices, описание изменений сборки/сокращения, incorporated software notices, provenance и security review |
| Berkeley DB 6.0.19 | `_dbm` содержит конкретную version string; Sleepycat | Исключить из минимального профиля либо утвердить source obligations и совместимость всей модели распространения |
| cryptography 50.0.2 | Python modules и `_rust.abi3.so` в executable; `Apache-2.0 OR BSD-3-Clause` | Явно выбрать разрешённую ветвь, учесть встроенные OpenSSL/Rust libraries и их notices |
| OpenSSL 4.0.3 в cryptography | Supplier `sbom.json` и runtime backend version | Apache-2.0; проверить copyright/NOTICE/change requirements и provenance этого wheel |
| CFFI 2.1.1 | `_cffi_backend` в executable; точный wheel LICENSE — `MIT-0` с оговорками для отдельных файлов | Проверить отдельно embedded libffi: точная версия в receipt пока неизвестна; нельзя распространить MIT-0 на чужой libffi |
| PyInstaller bootloader/loader 6.22.3 | Встроены в executable | `GPL-2.0-or-later WITH Bootloader-exception`; подтвердить применимость исключения к реально используемым неизменённым файлам |
| PyInstaller runtime hook 6.22.3 | `pyi_rth_inspect` | Apache-2.0, отдельная от bootloader область условий |
| Contrib runtime hook 2026.9 | `pyi_rth_cryptography_openssl` | Apache-2.0; GPL обычных build hooks не переносится автоматически на этот файл |
| libgcc_s.so.1, Ubuntu package 14.2.0-4ubuntu2~24.04.1 | Реальный native entry frozen executable; hash сопоставлен с системным файлом | `GPL-3.0-or-later WITH GCC-exception-3.1`; проверить условия исключения, применимые obligations к распространению самой runtime library и notices |
| pip 25.3 в ensurepip | Wheel размером 1 778 622 байта присутствует в payload | MIT верхнего пакета не покрывает автоматически 19 vendored dependencies; удалить ненужный ensurepip либо отдельно завершить vendor review |
| Tcl/Tk 9.0.3, Itcl 4.3.5, Thread 3.0.4, demos/assets | Реальные `.so`, `.tcl`, изображения и static stubs | TCL license проверена для Tcl/Tk recipe; Itcl/Thread/per-file exceptions пока UNKNOWN; удалить ненужную GUI-подсистему из web-only reference |

Первичные источники: [лицензирование PyInstaller 6.22.3](https://pyinstaller.org/en/v6.22.3/license.html), [COPYING.txt](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/COPYING.txt), [cryptography 50.0.2 LICENSE](https://github.com/pyca/cryptography/blob/50.0.2/LICENSE), [OpenSSL 4.0.3 LICENSE](https://github.com/openssl/openssl/blob/openssl-4.0.3/LICENSE.txt), [GCC 14.2 runtime exception](https://github.com/gcc-mirror/gcc/blob/releases/gcc-14.2.0/COPYING.RUNTIME). Точные локальные wheel license hashes сохранены в receipt; неудачное получение CFFI version tag не заменено ссылкой на произвольный `main`.

Документация PyInstaller разрешает выпуск коммерческих executable с условиями собственных зависимостей. Для обычного неизменённого embedded bootloader она не требует включать PyInstaller license/credits в приложение. Это положение не является общим освобождением для всех third-party libraries и не отменяет правила распространения изменённого PyInstaller либо его исходников. Runtime hooks имеют отдельно указанные Apache terms.

GCC exception допускает определённые сочетания с независимыми, в том числе proprietary modules; наличие слова GPL не означает запрет продажи. Вместе с тем исключение нельзя механически распространить на любой файл, поставку самого компилятора либо иную комбинацию. Gate требует специального review, а не делает вывода о необходимости раскрывать весь продукт.

## Статические библиотеки внутри libpython

Установленный runtime имеет `_ssl`, `_hashlib`, `_sqlite3`, `pyexpat`, `_bz2`, `_lzma`, `_ctypes`, `_curses`, `_curses_panel`, `readline`, `_decimal`, `_uuid`, `_zstd` в `sys.builtin_module_names`. Поэтому исключение одноимённых `.py` из упаковки или `--exclude-module` не удаляет соответствующие машинные байты из `libpython3.14.so.1.0`.

Рецепт PBS `20260325` задаёт следующие версии и лицензии. Для части версии независимо подтверждены runtime probes; для остальных recipe pin остаётся кандидатом до полной binary/source сверки. [Первичный закреплённый recipe](https://github.com/astral-sh/python-build-standalone/blob/20260325/pythonbuild/downloads.py), [описание licensing/архивов PBS](https://gregoryszorc.com/docs/python-build-standalone/main/running.html).

| Компонент | Версия recipe / фактический probe | Лицензия recipe |
| --- | --- | --- |
| OpenSSL Python ssl | **3.5.5**, подтверждён probe | Apache-2.0 |
| SQLite | **3.50.4**, подтверждён probe | Public-domain dedication; отдельная проверка provenance |
| zlib | **1.3.1**, подтверждён probe | Zlib |
| Expat | **2.6.3**, подтверждён probe | MIT |
| mpdecimal | **4.0.0**, подтверждён probe | BSD-2-Clause |
| libedit | 20240808-3.1; backend `editline` подтверждён | BSD-3-Clause |
| ncurses/panel | 6.5 | X11 |
| bzip2 | 1.0.8 | bzip2-1.0.6 |
| libffi Python ctypes | 3.4.6 | MIT |
| liblzma из XZ | 5.8.1 | 0BSD для используемой liblzma; другие XZ части имеют свои условия |
| libuuid | 1.0.3 | BSD-3-Clause |
| zstd | 1.5.7 | BSD-3-Clause branch |

Здесь существуют **две разные копии OpenSSL**: 3.5.5 в Python и 4.0.3 в cryptography. Обновление одной не обновляет другую. Аналогично libffi внутри CFFI wheel нельзя автоматически приравнять к libffi 3.4.6 из PBS recipe.

Граф исходного CPython сам по себе тоже не является графом этого бинарного PBS. Например, CPython `v3.14.3/Misc/sbom.spdx.json` перечисляет Expat 2.7.3 и mpdecimal 2.5.1, тогда как реальные probes данного runtime показывают 2.6.3 и 4.0.0. В исходном SBOM есть HACL* commit `8ba599b2f6c9701b3dc961db895b0856a2210f76` с `NOASSERTION`; это указание на дальнейшую проверку, а не разрешение. [CPython source SBOM](https://github.com/python/cpython/blob/v3.14.3/Misc/sbom.spdx.json).

Базовый `LICENSE.txt` Python не заменяет все notices OpenSSL, libedit, ncurses и прочих встроенных библиотек. Полный PBS `PYTHON.json`/набор `LICENSE.*` не найден в проверенном install-only runtime. Upstream recipe и выбранные license texts получены отдельно и зафиксированы receipts; они пока не добавлены в пользовательскую поставку как полный проверенный notice pack.

Текущий `readline.backend` — `editline`, `_gdbm` не входит в список встроенных модулей. Это согласуется с современным PBS подходом, который использует libedit и отключает GDBM для уменьшения GPL-зависимостей. Этот факт не устраняет отдельно найденную BDB/Sleepycat в `_dbm`.

## Build environment и граница распространения

| Distribution | Версия | Декларация/точный текст в установленном пакете | Scope в проверке |
| --- | --- | --- | --- |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Runtime и build |
| cffi | 2.1.1 | MIT-0, отдельные исключения возможны | Runtime backend и build |
| PyInstaller | 6.22.3 | GPL-2.0-or-later; embedded exception; Apache для определённых файлов | Builder отдельно; embedded части отдельно |
| pyinstaller-hooks-contrib | 2026.9 | GPL-2.0-or-later build hooks; Apache-2.0 runtime hooks | Разделено по файлам |
| pytest | 9.1.1 | MIT | Build/test only |
| pluggy | 1.7.0 | MIT | Build/test only |
| iniconfig | 2.3.1 | MIT | Build/test only |
| Pygments | 2.21.0 | BSD-2-Clause | Build/test only |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Build only |
| altgraph | 0.17.5 | MIT | Build only |
| build | 1.6.1 | MIT | Build only |
| pyproject_hooks | 1.3.3 | MIT | Build only |
| setuptools | 84.2.1 | MIT верхнего пакета; vendor closure отдельно | Build only |
| pycparser | 3.12 | BSD-3-Clause | Есть в venv, не найден в текущем frozen graph как runtime package |

Исходные `METADATA`, license-file hashes и `Requires-Dist` каждой версии сохранены в receipt. Эта таблица не распространяет лицензию верхнего пакета на весь vendor tree. Если venv, build-work, Docker image с build layer или source archive с инструментами попадает пользователю, scope необходимо пересчитать: «используется только при сборке» не означает «можно не учитывать при распространении сборочной среды».

### Rust graph cryptography

В точном wheel есть supplier CycloneDX с **39 component records**. В нём преобладают MIT/Apache/BSD alternatives, однако существенны три формы:

- `self_cell 1.3.0`: `Apache-2.0 OR GPL-2.0-only`. Допускается явно выбранная Apache ветвь после проверки условий; запрещено считать весь пакет GPL-only по подстроке.
- `unicode-ident 1.0.24`: `(MIT OR Apache-2.0) AND Unicode-3.0`. Вторая обязанность не исчезает после выбора MIT/Apache.
- `target-lexicon 0.13.5`: `Apache-2.0 WITH LLVM-exception`. `WITH` связан с конкретной лицензией и требует проверки применимости.

`cc`, `find-msvc-tools`, `pkg-config`, proc-macro crates и другие записи supplier graph могут относиться к build-time ветке. Их отделение от встроенных bytes пока не завершено, поэтому inventory консервативен. Выбор ветвей, copyright notices и полнота actual compiled graph остаются `PENDING_REVIEW`. [Закреплённый Cargo.lock](https://github.com/pyca/cryptography/blob/50.0.2/Cargo.lock).

### Vendored pip в ensurepip

Внутри присутствующего wheel `vendor.txt` перечисляет CacheControl 0.14.3, distlib 0.4.0, distro 1.9.0, msgpack 1.1.2, packaging 25.0, platformdirs 4.5.0, pyproject-hooks 1.2.0, requests 2.32.5, certifi 2025.10.5, idna 3.10, urllib3 1.26.20, rich 14.2.0, Pygments 2.19.2, resolvelib 1.2.1, setuptools 70.3.0, tomli 2.3.0, tomli-w 1.2.0, truststore 0.10.4 и dependency-groups 1.3.1. Тексты многих лицензий находятся внутри wheel, но их наличие не равно завершённому review. В inventory эти 19 записей не наследуют автоматически MIT от pip; выбранное исправление минимального профиля — убрать ненужный ensurepip целиком.

## Что можно убрать и что этим не исправляется

Для **конкретного web + SQLite reference profile** можно исключить `_dbm`, `_tkinter`, ensurepip/pip, tkinter/idlelib/turtledemo, Tcl/Tk/Itcl/Thread, GUI demos/images, неиспользуемые headers/static stubs/pkgconfig/man pages после smoke/lifecycle проверки. Это не должно становиться безусловной политикой для любого подключаемого продукта: иной проект может нуждаться в исключённом компоненте.

Уже исключённый `share/terminfo` убирает данные и filename collisions, но не встроенную ncurses. Исключение `readline` и `_curses` из PyInstaller analysis не удаляет соответствующие native modules из поставляемой libpython. Чтобы избавиться от их машинных байт, нужна другая проверенная сборка runtime или его воспроизводимая пересборка с нужной конфигурацией.

Нельзя считать planned pruning доказательством отсутствия: следующий bundle требует нового SHA inventory, CArchive/native inspection, проверки функциональности и собственного audit receipt. Этот документ не переименовывает bundle 04 в исправленный bundle 05.

## Политика и executable release gate

`packaging/license-policy.json` задаёт версионируемую политику для коммерческого и некоммерческого использования. `candidate` означает возможность одобрения после доказательств, `review` — обязательную дополнительную проверку условий, `deny` — установленный конфликт с выбранной моделью, например явное NC-ограничение. GPL/LGPL/MPL/Sleepycat не классифицируются как blanket commercial bans.

`tools/check_licenses.py` проверяет:

1. Синтаксис SPDX-подобных `AND`/`OR`/`WITH`, скобки и ограничение сложности. Ветвь `OR` выбирается явно; из `AND` нельзя выбросить обязанность; исключение `WITH` не теряется.
2. Exact component version, scope, отдельное одобрение, cleared conflicts, transitive closure, обязательства и hash доказательств.
3. Привязку review к версии/лицензии/файлам компонента и review релиза к digest всего inventory. Старое approval не применяется после изменения версии или байтов.
4. Доставку необходимых notice files в release artifact и их hash. Произвольный `license_ref` либо URL не удовлетворяет этому требованию.
5. Полное совпадение файлов staging directory с declared artifacts: undeclared/missing/changed bytes, symlink и special files блокируют проверку.
6. Отсутствие неправильно помеченных build-only/host-system компонентов среди реально поставляемых файлов.
7. Отдельные reviews полного scope, transitive closure, source provenance, security baseline и product licensing.

Policy и review inventory являются доверенными, проходящими code review данными. Скрипт не подтверждает личность reviewer, не проверяет право авторства юридического заключения и не способен сам обнаружить все скрытые зависимости внутри бинарного файла. Поэтому вручную выставленное `APPROVED` без содержательного review недопустимо; наличие проверенного closure review — самостоятельное условие. SHA-256, подпись и SBOM защищают/описывают данные, но не создают разрешения правообладателей.

Пример выполнения для проверенного DEV-каталога:

```text
python tools/check_licenses.py \
  --inventory packaging/audit-evidence/reference-dev-bundle-04.inventory.json \
  --artifact-root /workspace/scratch/5f15f7cb0238/work/reference-dev-bundle-04 \
  --evidence-root . \
  --production
```

Реальный результат: **exit code 2, BLOCKED**, 102 review records, 1471 declared artifacts, 482 отдельных сигнала проверки. Это число включает повторные незакрытые обязанности, 15 undeclared build-work files и не является числом найденных юридических нарушений. Ошибки данных/неизвестность блокируют production одинаково; audit mode не превращает blocked результат в production eligible.

Для настоящего релиза gate следует запускать на **чистом окончательном staging directory** после копирования notices и до подписи/публикации. Утилита пока не подключена автоматически к существующему builder: подключение — явная следующая интеграционная задача; standalone gate не выдаётся за уже обязательную проверку каждого пути сборки.

## Условия снятия блокировки

Нужно проверить исправленный минимальный артефакт, убрать либо отдельно разрешить BDB и лишние компоненты, восстановить источник/достоверный download receipt runtime, закрыть native/Rust/vendor closure, собрать реальные third-party license/notice texts, явно выбрать OR-ветви и выполнить применимые source/exception obligations. Для proprietary product EULA необходимо сохранить права, которые third-party лицензии дают получателю; общее пользовательское соглашение не может отменить их.

Python 3.14.3 остаётся историческим тестовым pin. На дату проверки [официальная страница 3.14.3](https://www.python.org/downloads/release/python-3143/) указывает, что она заменена 3.14.9. Это основание открыть security/release gate и оценить обновление, а не утверждение без проверки о конкретной CVE этого бинарного пакета. Проверка актуальных advisory должна включать обе копии OpenSSL, Expat, liblzma и остальные точные native версии.

После изменений потребуются свежие lifecycle/GUI проверки целевого профиля и отдельные установленная Windows/Docker acceptance. Лицензионный gate не заменяет эти испытания и не означает, что продукт готов к эксплуатации.

## Тестирование checker

```text
python -m pytest -q tests/test_license_policy.py
26 passed in 0.21s
```

Среда: Linux, Python 3.14.3. Проверяются отказ для UNKNOWN/UNAPPROVED/CONFLICT, NC restrictions, корректное применение OR/AND/WITH, GPL exception без blanket ban, build/runtime misclassification, обязательства, реальные file hashes, изменённые notices/evidence, stale approval, extra/missing files, symlinks и CLI exit code. Все `APPROVED` в тестах относятся только к искусственным fixtures с отметкой `SYNTHETIC-TEST-ONLY`; это не разрешение распространять реальные audited components.
