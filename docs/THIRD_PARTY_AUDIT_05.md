# Лицензионный аудит DEV reference bundle 05: обновление фактов

**Результат: `BLOCKED`, `production_eligible: false`.** Проверка выполнена
2026-10-25 для конкретного `reference-dev-bundle-05`. Удаление лишних компонентов
сократило объём дальнейшего review, но не завершило его. Исторические inventory,
receipt и gate для bundle 04 сохранены без изменения.

Это дополнение к [`THIRD_PARTY_AUDIT.md`](THIRD_PARTY_AUDIT.md), где находятся
первичные лицензионные источники, разбор native/Rust компонентов и правила
коммерческого/некоммерческого использования. Повторного интернет-аудита в этом
обновлении не было. Сверены фактические файлы нового bundle, его frozen archive,
исторические source receipts и уже скачанные проверенные официальные wheels.

## 1. Что именно проверено

| Показатель | Bundle 04 | Bundle 05 |
| --- | ---: | ---: |
| Файлы payload в подписываемом inventory | 1465 | 896 |
| Выбранные payload/executable/control artifacts | 1471 | 902 |
| Component candidates для лицензионного review | 102 | 73 |
| Элементы frozen CArchive | 43 | 44 |
| Модули нового frozen PYZ | Не фиксировались этим receipt | 221 |
| Итог production license gate | BLOCKED | BLOCKED |

Для bundle 05 все **896 фактических payload-файлов совпали по именам и SHA-256**
с `inventory.json`: missing = 0, extra = 0, changed = 0. Этот шаг сравнивает bytes
с декларацией. Криптографическую аутентичность publisher signature лицензионный
scanner не объявляет проверенной; она относится к отдельному trust gate.

Frozen executable:

```text
HomenSAI-Reference-DEV
size:   18363304
sha256: dcd23cddf16c4b92b68c549a96acef4e0d36ec375268f3b7e10a240e749fe505
```

Из старого выбранного набора исчезли 572 пути, добавились 3 first-party пути,
изменились bytes 13 существующих артефактов, 886 остались идентичными. Новые пути:
`payload/reference-profile.json`, модули `homensai_reference/profile.py` и
`homensai_reference/storage.py`. Происхождение JSON подтверждено конкретной
записью в `packaging/build_reference.py`; ему не приписана сторонняя лицензия.
Право распространения собственного продукта и EULA остаются отдельной записью
review, а не следствием такого технического mapping.

Файлы evidence:

- `packaging/audit-evidence/reference-dev-bundle-05.inventory.json` — 902 пути,
  точные SHA-256 и component candidates.
- `packaging/audit-evidence/reference-dev-bundle-05.receipt.json` — CArchive/PYZ,
  delta с 04, сопоставление native wheels, отдельный список build-work файлов.
- `packaging/audit-evidence/reference-dev-bundle-05.gate.json` — реальный вывод
  production gate; процесс завершился с кодом **2**.
- `tools/refresh_reference_audit.py` — воспроизводимый read-only scanner именно
  этого перехода к минимальному reference profile. Он не присваивает `APPROVED`.

## 2. Подтверждённое сокращение состава

В payload, отдельном frozen native archive и перечне модулей PYZ проверено
отсутствие `dbm/_dbm/_gdbm`, `ensurepip/pip`, `tkinter/_tkinter`, `idlelib` и
`turtledemo`. Также отсутствуют удалённые каталоги Tcl/Tk, Itcl и Tcl Thread.

Из активного reference inventory исключены 29 прежних кандидатов:

- Berkeley DB, Tcl, Tk и три кандидата библиотек X11, привязанных к удалённому
  Tcl/Tk-составу в исходном mapping.
- Itcl, Tcl Thread и Tk demo assets.
- Ensurepip wheel pip 25.3 и 19 его vendor declarations.

Это результат проверки конкретного web/SQLite profile. Отсутствие известных
файлов и module names не является доказательством отсутствия любых неизвестных
встроенных native bytes. Полнота static/transitive closure всё ещё требует
review, и gate её не принимает как завершённую.

В частности, `libpython3.14.so.1.0` в payload 04, payload 05 и frozen executable
05 имеет одинаковый SHA-256:

```text
e9870cb294d92ee8f4c90644ef825105115887d96bf1075d5673c0ba8f851f9a
```

Его bytes не изменились. Ранее выявленные builtin/native кандидаты, в том числе
libedit и ncurses, остаются в review. Исключение Python-модуля через PyInstaller
не позволяет объявить статически включённую библиотеку удалённой.

## 3. Новая связь native bytes с официальными wheels

Два реально вложенных native файла совпали по SHA-256 с соответствующими
членами официальных wheel-архивов. Каждый внешний wheel перед чтением снова
сверен целиком по размеру и SHA из `packaging/build-dependencies.json`.

| Frozen entry | Проверенный официальный wheel |
| --- | --- |
| `_cffi_backend.cpython-314-x86_64-linux-gnu.so` | `cffi-2.1.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` |
| `cryptography/hazmat/bindings/_rust.abi3.so` | `cryptography-50.0.2-cp311-abi3-manylinux_2_34_x86_64.whl` |

SHA-256 внешних wheels:

```text
cffi:
b0431303acaea1089ad4b3e9ce4e6518193def1118d4073ca848635ee4ea2e96

cryptography:
9dab55f57c74c3cad24c323bacbbd04be4705ba6eb0d92e920b1fc4837ed5079
```

Это усиливает доказательство происхождения **этих двух native members**.
Проверка не доказывает состав каждой другой установленной Python source file,
не разрешает автоматически выбранную лицензионную альтернативу и не закрывает
обязанности по notices/source. Версия libffi, статически включённой в CFFI wheel,
по-прежнему записана как `UNKNOWN`.

## 4. Почему production gate остаётся закрыт

В новой таблице 73 кандидата: один first-party, 60 в консервативной категории
redistributed и 12 build-only. Категория redistributed включает Rust closure
candidates; определение compile-time-only и реально включённых частей требует
окончательного review. Все записи сохраняют `PENDING_REVIEW`.

Gate вернул 352 сигнала. Они включают повторяющиеся проверки каждого компонента;
это **не число установленных нарушений закона или отдельных несовместимых
лицензий**. Существенные незакрытые условия:

1. Нет утверждённого полного component/source inventory и всех review records,
   привязанных к точным bytes этого bundle.
2. Сохранены явные `OR`-альтернативы, требующие выбранной правовой стратегии;
   исключения PyInstaller/GCC/LLVM и Unicode obligations требуют своего review.
3. Notices и возможные source-distribution obligations не подтверждены для
   конечного выпуска. Обнаруженный LICENSE не приравнивается к выполнению всех
   применимых условий.
4. Исходный runtime download SHA исторически не был сохранён. Ожидаемый SHA
   восстановлен по официальному release API в `runtime-source-receipt.json`,
   но происхождение существующего дерева этим задним числом не доказано.
5. CPython 3.14.3 и native libraries требуют актуального security/release review;
   локальные DEV-проверки не являются одобрением production baseline.
6. Product EULA и условия распространения собственного приложения должны быть
   завершены отдельно.
7. В development-каталоге присутствуют **15 build-work файлов**, не заявленных
   как поставляемые артефакты. Gate правильно сообщает `ARTIFACT_UNDECLARED`.
   Для выпуска нужен новый чистый staging с явно определённым составом и новым
   связанным inventory. Нельзя просто игнорировать лишние файлы в release gate.

Не обнаружено нового основания объявить любую GPL-лицензию запретом на
коммерческое использование. Решения зависят от точного произведения, выбранного
варианта лицензии, исключений, способа поставки и выполнения условий.
Ограничительный release policy остаётся fail-closed при отсутствии review.

## 5. Воспроизведение

В подготовленном build venv из [`BUILDING.md`](BUILDING.md):

```bash
python tools/refresh_reference_audit.py \
  --bundle /absolute/path/to/reference-dev-bundle-05 \
  --output /absolute/path/to/new-audit-output \
  --wheel-dir /absolute/path/to/all-selected-wheels

python tools/check_licenses.py \
  --inventory /absolute/path/to/new-audit-output/reference-dev-bundle-05.inventory.json \
  --artifact-root /absolute/path/to/reference-dev-bundle-05 \
  --evidence-root packaging/audit-evidence \
  --production \
  --report /absolute/path/to/new-audit-output/reference-dev-bundle-05.gate.json
```

Output должен находиться вне проверяемого bundle. Существующие receipts скрипты
не перезаписывают; выберите новый output. Scanner читает содержимое CArchive и
ZIP, не запускает проверяемый executable/runtime и не пишет в bundle. Он
использует ранее собранные source evidence и ожидаемый mapping bundle 04;
для другого продукта или состава требуется отдельный полноценный inventory.

Если появляется лишний, отсутствующий или изменившийся payload-файл, receipt
сохраняет `MISMATCH` и перечень различий. Такая запись должна блокировать выпуск
независимо от лицензионных результатов. Удалять неизвестный файл или исключать
его из integrity check ради успешного результата не является частью аудита.
