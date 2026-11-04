# Rent в установочном комплекте: пробная сборка и разрыв требований

Serhii Khomenko · HomenS.AI

Дата: 03.11.2026. Проверялся сборщик `python -m shared.kit` из рабочего дерева
модуля (незакоммиченные `shared/kit*.py`) на продукте HomenS.AI Rent
`0.4.0a12` (`/home/serhii/homensai/homensai.rent`, коммит `61c1b306`).
Репозиторий Rent **не изменялся**: всё ниже собрано из копий в
`~/.cache/homensai-rent-kit/`. Требования к продукту — [KIT.md](../KIT.md), раздел 4.

Хост: Parrot 6.4 (Debian 12, glibc 2.36), x86-64, средняя нагрузка 20–34 за
время работы. Все длительности завышены нагрузкой.

## 1. Итог в двух словах

| Вопрос | Ответ |
|---|---|
| Собирается ли комплект из исходной копии? | На момент проверки — **нет**: сборщик отклонял wheel `segno-1.6.6` (раздел 2.1). Исправлено в `shared/kit_wheels.py` 03.11.2026: `.data/data` теперь пропускается. |
| Собирается ли после обхода в scratch-копии? | Да, `verify` проходит (подпись и ключ издателя). |
| Работают ли двоичные зависимости из `pydeps` на Linux? | Да, все 10 проверенных пакетов (раздел 3.2). |
| Импортируется ли Rent из выпуска в раскладке пробной копии? | **Нет**: `ModuleNotFoundError: No module named 'scripts'` (раздел 3.1). |
| С исправленной раскладкой и адаптером (scratch, v2)? | Да, на Linux: импорты, health-контракт, идемпотентный `migrate`, SIGTERM (раздел 3.3). |
| Готов ли Rent к установке комплектом? | **Нет**, нужны изменения на стороне Rent (раздел 4); исправление сборщика (2.1) уже внесено. |

## 2. Сборка

### 2.1. Ошибка сборщика на `segno` (исправлено)

Команда из задания (исходная копия `~/.cache/homensai-rent-kit/project`):

```
python -m shared.kit build --config ~/.cache/homensai-rent-kit/project/homensai-kit.json --version 0.4.0a12 \
  --signing-key ~/.cache/homensai-rent-kit/keys/publisher-ed25519.pem \
  --installer-wheelhouse ~/.cache/homensai-kit-inputs/wheelhouse --output ~/.cache/homensai-rent-kit/dist --no-archive
```

Результат через 13 с: `Ошибка: Wheel содержит данные вне каталогов библиотек`, код 1.

Причина: `segno-1.6.6-py3-none-any.whl` содержит страницу man
`segno-1.6.6.data/data/share/man/man1/segno.1`. `install_wheel()` в
`shared/kit_wheels.py` (строки ~140–148) пропускает `.data/scripts` и
`.data/headers`, но любую запись `.data/data` отклоняет. Среди остальных 16
wheels Rent `.data/` нет ни у кого (проверено перебором архивов).

Это ограничение сборщика, а не ошибка Rent: `.data/data` — штатная схема
wheel (файлы для `<prefix>/`), для импорта она не нужна. Предложение (не
применено, `shared/` не менялся): пропускать `.data/data` так же, как
`scripts`/`headers`, учитывать в `skipped`, а при желании отражать пропуск в
`inventory.json`. Разумная строгость: отклонять только если в `.data/data`
есть `.py`/`.so`/`.pyd` (признак того, что wheel ждёт импорта из `prefix`).

Обход для пробной сборки (только scratch): `~/.cache/homensai-rent-kit/wheelhouse-w`
— копия wheelhouse, где `segno` перепакован без man-страницы (RECORD
исправлен), а в `project-w/installer/*.lock` подставлен новый хэш
`b14a3388…eb88a`. Для настоящего выпуска так делать нельзя: lock перестаёт
соответствовать PyPI.

### 2.2. Результаты сборки

Ключ издателя создан `keygen` в `~/.cache/homensai-rent-kit/keys`
(отпечаток `34961d8b…4b41`, только для пробы).

| | v1: раскладка пробной копии (+обход segno) | v2: исправленная раскладка + адаптер (scratch) |
|---|---|---|
| Конфиг | `project-w/homensai-kit.json` | `project-v2/homensai-kit.json` (раздел 6) |
| Длительность `build` | 593 с | 466 с |
| Код выхода | 0 | 0 |
| `verify --expect-key-sha256` | подпись и доверие подтверждены (91 с) | подтверждены |
| Каталог комплекта | 401 МБ, 1824 файла | 406 МБ, 1824 файла |
| `package/*.zip` | 270 193 078 байт | 275 447 140 байт |
| Файлов в подписанном пакете (`file_count`) | 4746 | 4945 |
| `installer/` | 143 МБ (Linux 93 МБ, Windows 48 МБ, код 1,3 МБ) | так же |
| `package/inventory.json` | 836 КБ | — |

Состав `payload/` (v1, после распаковки 267 МБ):

| Каталог | Размер | Файлов |
|---|---|---|
| `pydeps/linux-x86_64` | 51 МБ | 1168 |
| `pydeps/windows-x86_64` | 40 МБ | 1149 |
| `runtime-linux` (PBS 3.14.10, минимальный профиль) | 78 МБ | 625 |
| `runtime-windows` | 37 МБ | 777 |
| `vendor` (сами wheels, обе платформы) | 30 МБ | 17 |
| `windows` (WinSW + лицензия) | 18 МБ | 2 |
| `src` (app, rent, render) | 14 МБ | 772 |
| `migrations` | 2 МБ | 230 |
| `templates`, `locks`, `systemd-user`, загрузчик | < 0,3 МБ | — |

В v2 `src` — 21 МБ, 1202 файла (добавлены `scripts` (4 файла), `migrations`,
`templates`, `design`, `docs/HELP.md`, `docs/EXCEL_FORMAT.md`, `docs/legal`,
`pyproject.toml`).

Зависимости (одинаковы в v1 и v2; Rent lock, CPython 3.14):

| Пакет | linux-x86_64 | windows-x86_64 |
|---|---|---|
| cffi 2.1.1 | cp314 manylinux2014 | cp314 win_amd64 |
| charset-normalizer 3.5.2 | cp314 manylinux_2_28 | cp314 win_amd64 |
| cryptography 50.0.2 | abi3 manylinux_2_28 | abi3 win_amd64 |
| pillow 12.3.0 | cp314 manylinux_2_28 | cp314 win_amd64 |
| pycryptodomex 3.24.1 | abi3 manylinux2014 | abi3 win_amd64 |
| et-xmlfile 2.0.0, openpyxl 3.1.5, pycparser 3.11, pypdf 6.19.0, pyzipper 0.4.0, reportlab 5.0.1, segno 1.6.6 | py3-none-any | py3-none-any |

Python самого установщика собран из `--installer-wheelhouse`
(`~/.cache/homensai-kit-inputs/wheelhouse`: cffi 2.1.1, cryptography 50.0.2,
**pycparser 3.0**), потому что `requirements-crypto.txt` модуля закрепляет
pycparser 3.0, а lock Rent — 3.11. Конфликта нет: зависимости продукта и
установщика лежат в разных каталогах.

Наблюдения по сборке:

- `vendor` зарезервирован сборщиком под wheels; у Rent есть свой `vendor/`
  (колёса homensai-platform). В автономном комплекте он не нужен, но
  переносить его под этим именем нельзя.
- `python_paths` не допускает `.`: код Rent должен лежать в подкаталоге
  выпуска (`src/`), причём **вместе** со всем, что Rent ищет через
  `Path(__file__).parents[N]` (раздел 4.1).
- Wheels попадают в пакет дважды: распакованными (`pydeps/*`, 91 МБ) и как
  есть (`vendor/`, 30 МБ). Это поведение сборщика, не Rent.

## 3. Проверка выпуска без установки

Пакет распакован в `~/.cache/homensai-rent-kit/smoke*/x`, у
`runtime-linux/bin/python` выставлен `+x`. Запуск — как у служб:
`env -i HOME=… PATH=/usr/bin:/bin runtime-linux/bin/python -I -B payload/homensai_entry.py <модуль> …`.

### 3.1. v1 (раскладка пробной копии): Rent не импортируется

`app.web --help`, `app.bot --help`, `app.scheduler --help`, `app.ctl --help` —
все падают одинаково:

```
File ".../payload/src/app/db.py", line 15, in <module>
    from scripts import check_schema, migrate
ModuleNotFoundError: No module named 'scripts'
```

Пошаговая донастройка распакованного пакета (только для диагностики) дала
следующую цепочку:

1. добавить `src/scripts/{migrate,check_schema}.py` →
   `ModuleNotFoundError: No module named 'freeze_migrations'`
   (`check_schema.py` делает `sys.path.insert(0, scripts/)` и `import freeze_migrations`);
2. добавить `freeze_migrations.py` → `app.web`/`app.bot`:
   `No module named 'scripts.build_import_template'` (импорт из `app/exchange/book.py`).

Кроме того, пробная копия кладёт `migrations/` и `templates/` в корень
выпуска, а Rent ищет их как `src/migrations` и `src/templates`
(`scripts/migrate.py`: `ROOT = parent.parent`; `app/exchange/public_api.py`:
`parents[2] / "templates"`) — это сломало бы `migrate` и шаблон импорта уже
после устранения ошибок импорта.

### 3.2. Двоичные зависимости из `pydeps/linux-x86_64` — работают

Скрипт `~/.cache/homensai-rent-kit/tools/deps_check.py` (импорт и реальная
операция, встроенный Python, `-I -B`, пустое окружение):

| Пакет | Проверка | Итог |
|---|---|---|
| pillow 12.3.0 | создать изображение, сохранить PNG и JPEG; WebP | ok, webp=True |
| pycryptodomex 3.24.1 | AES-GCM encrypt | ok |
| cryptography 50.0.2 | AESGCM encrypt | ok |
| charset-normalizer 3.5.2 | импорт, `md` скомпилирован (`.so`) | ok |
| cffi 2.1.1 | `_cffi_backend` | ok |
| pyzipper 0.4.0, segno 1.6.6, pypdf 6.19.0, openpyxl 3.1.5 | импорт | ok |
| reportlab 5.0.1 | создать PDF-холст | ok |

Windows-половина (`pydeps/windows-x86_64`, `runtime-windows`) **не
проверялась**: нужна ВМ из KIT.md, раздел 8.

### 3.3. v2 (исправленная раскладка + черновик адаптера, scratch) — работает на Linux

Комплект `dist-v2` собран из `project-v2` (раздел 6: все нужные Rent
файлы под `src/`, адаптер из раздела 5). Сценарий
`~/.cache/homensai-rent-kit/tools/smoke_v2.sh`: каталог данных с
`secrets.env` (0600, только пробный `HOMENSAI_RENT_DEV_KEY`), порт 18820,
пустое окружение, без установки и без systemd.

| Проверка | Результат |
|---|---|
| `app.web`, `app.bot`, `app.scheduler`, `app.ctl` `--help` | все код 0, ошибок импорта нет |
| `migrate --migration rent-schema-0.4.0a12` без базы | `{"applied": 0, "note": "no database"}`, код 0 |
| Первый старт `web` (создание схемы из 230 миграций) | health 200 через **292 с** (нагрузка ~25) |
| `GET /homensai/health` | `200 {"product": "homensai-rent", "release": "0.4.0a12", "healthy": true}` |
| То же с `Host: evil:1` | 400 |
| Прежний `GET /healthz` Rent | 200 (пустая база: проверки «хозяйство не создано» считаются OK) |
| `GET /` | 303 (переход в мастер настройки) |
| `migrate` дважды на созданной базе | оба раза `{"applied": 0}`, код 0 — идемпотентно |
| `bot` без `RENT_BOT_TOKEN` | одна строка в журнал, процесс ждёт, не падает |
| SIGTERM `web` | код 0 за 4,3 с, «Остановлено.» |
| SIGTERM `bot` (в ожидании) | код 0 за 0,7 с |
| SIGTERM `scheduler` через 40 с после запуска | **код 130 и traceback**: процесс ещё импортировал модули (reportlab), `KeyboardInterrupt` из обработчика адаптера не был перехвачен. Исправлено в черновике (импорт внутри `try`); повтор: SIGTERM через 15 с — код 0, через 150 с (в рабочем цикле) — код 0 за 5,6 с |
| PDF-подпроцесс так, как его запускает Rent (`python -B -m app.banking.pdf_worker`, `cwd=src`) | `import pypdf` → `ModuleNotFoundError`; на настоящем PDF воркер отвечает «Не удалось безопасно прочитать PDF-выписку (повреждение или превышение лимита)» — **ошибка зависимостей маскируется**; с `PYTHONPATH=pydeps/linux-x86_64` тот же файл разбирается («Неизвестный … формат PDF-выписки» — ожидаемо для пустого PDF) |

Выводы из v2: Linux-выпуск Rent работает с раскладкой из раздела 6 и
адаптером; первый старт на нагруженной машине долгий (почти 5 минут на
схему), импорт `app.scheduler` — более 40 с. Установка (`setup.sh install`)
и systemd не проверялись.

## 4. Что Rent должен изменить (разрыв с KIT.md, раздел 4)

Порядок — по критичности. «Проверено» — подтверждено запуском выше;
остальное — по чтению кода, без запуска.

### 4.1. Раскладка выпуска (блокирует запуск; проверено)

Rent рассчитывает на корень репозитория рядом с `app/`. Во время работы
нужны (поиск `parents[N]`, `parent.parent`, `from scripts`):

| Что | Кто использует |
|---|---|
| `scripts/migrate.py`, `check_schema.py`, `freeze_migrations.py`, `build_import_template.py` | `app/db.py`, `app/archive/*`, `app/ctl/cli.py`, `app/exchange/book.py`, `app/bot/cli.py` |
| `migrations/` | `scripts/migrate.py`, `app/platform/object_scope.py`, `app/platform/migrations.py` |
| `templates/import/*.xlsx` | `app/exchange/public_api.py` |
| `design/` (css, icons, illustrations) | `app/web/core.py`, `layout.py`, `icons.py`, `object_art.py` |
| `pyproject.toml` (версия) | `app/platform/version.py`, `app/ctl/heartbeat.py`, `app/exchange/export.py` |
| `docs/HELP.md`, `docs/EXCEL_FORMAT.md`, `docs/legal/` | `app/web/screens/help.py`, `app/exchange/book.py`, `render/*_pdf.py` |
| `fixtures/demo/` | только `--demo`/`make-demo` (в комплект можно не включать) |

Минимальное решение без правки кода Rent — `sources` с `to: "src/…"` для
каждого пункта (готовый конфиг — раздел 6). Лучше на будущее: собрать
служебные модули (`migrate`, `check_schema`, `freeze_migrations`,
`build_import_template`) в пакет внутри `app/` и читать ресурсы через
`importlib.resources`, чтобы раскладка не зависела от `parents[N]`.

### 4.2. Проверка здоровья по контракту комплекта (блокирует install/update)

Сейчас `GET /healthz` (`app/web/screens/health.py`) отдаёт подробный отчёт
`health.evaluate()` и 503 при любом сбое — включая «бот ни разу не подавал
пульс», «архив не делался», «нет места». Комплект же требует HTTP 200 и
JSON **ровно** `{"product": HOMENSAI_PRODUCT_ID, "release": HOMENSAI_RELEASE, "healthy": true}`
(`shared/health.py`: множество ключей и значения сравниваются точно;
`shared/kit_windows.py`: сравнение словаря целиком).

- Нужен **отдельный путь** (предложено `/homensai/health`), а прежний
  `/healthz` оставить для `app.ctl status/doctor`.
- `healthy` — готовность, а не полный отчёт: база открывается, схема
  актуальна (`db.pending_migrations == 0`), папка данных доступна на запись.
  Пульсы бота/планировщика и архив в него включать нельзя: при обновлении
  комплект ждёт `health_wait` секунд, а бот без токена не поднимется
  никогда, и обновление будет откатываться.
- Проверка заголовка Host (`allowed_hosts` = `127.0.0.1:port`,
  `localhost:port`) совместима: `urllib` комплекта шлёт `127.0.0.1:<port>`.
- `HOMENSAI_RELEASE` (из `--version` сборки) должен совпадать с
  `pyproject.toml`; в адаптере можно дополнительно сверять с
  `app.platform.version.VERSION` (не сделано — решение за владельцем).

### 4.3. Каталог данных одним аргументом

Комплект передаёт `--data-dir @data`. У Rent три параметра: `--db ФАЙЛ`,
`--archive`/`--data-dir ПАПКА` (по умолчанию `HomensaiRent_Data` рядом с
базой). Предложенная раскладка в `@data`: `rent.sqlite3`, `files/`
(бывшая `HomensaiRent_Data`). Её задаёт адаптер, сами
`app.web/app.bot/app.scheduler` не меняются.

### 4.4. Миграции: модель Rent ↔ идентификаторы комплекта

Rent: 230 SQL-файлов (`migrations/0001_base.sql`, `0002_…` и
`migrations/<модуль>/NNNN_*.sql`), учёт в `schema_version` и
`module_migrations` с sha256; `scripts/migrate.py apply_pending()` применяет
все ожидающие, каждую в своей транзакции `BEGIN IMMEDIATE`, проверяет
checksum уже применённых и `foreign_key_check`.

Комплект: `homensai-kit.json → migrations` — упорядоченная история
идентификаторов; при первой установке все считаются применёнными, при
обновлении для каждого нового ID выполняется
`python homensai_entry.py <модуль> … @DATA_DIR@ @MIGRATION_ID@` с отдельной
копией перед каждым (`shared/kit_setup.py`, `shared/kit_windows.py`).

Предлагаемое соответствие — **один ID комплекта на выпуск со сменой схемы**:

- ID вида `rent-schema-<версия>` (например `rent-schema-0.4.0a12`);
  список только дописывается, порядок не меняется (иначе
  `kit.migrations_diverged`).
- Команда для любого ID = «применить все ожидающие миграции Rent»
  (`apply_pending`). Повтор безопасен: применённые файлы пропускаются по
  учёту Rent. Если в одном обновлении несколько новых ID, первый применит
  всё, остальные — пустые проходы (копия перед каждым всё равно делается).
- Первая установка: базы нет → `app.web` создаёт актуальную схему сам
  (`cli.open_database` → `db.migrate_up`) — это уже совпадает с KIT.md.
  Команда `migrate` без базы возвращает 0 (проверено, 3.3).
- Риск: выпуск добавил SQL-файлы, а новый ID в `homensai-kit.json` не
  дописан → `app.web` откажется стартовать («Схема базы отстала») → health
  не пройдёт → откат. Безопасно, но неприятно. Нужна проверка при сборке
  (например, тест Rent, что число файлов миграций изменилось ⇒ последний ID
  в `homensai-kit.json` равен версии из `pyproject.toml`). Не реализовано.
- На Windows миграция запускается с окружением только `SystemRoot` и `PATH`
  (`shared/kit_windows.py`, `migrate`): секретов там нет. SQL-миграциям Rent
  они не нужны; `aead-migrate` (перешифрование полей) через этот путь
  запускать нельзя — только отдельной командой владельца, как сейчас.

### 4.5. Секреты (доставляет комплект)

Rent нужны `HOMENSAI_RENT_DEV_KEY` (без него рабочая база не стартует —
`sealing.require_working_key()` в web/bot/scheduler), `RENT_BOT_TOKEN`,
`BACKUP_PASSWORD`, при оповещениях `RENT_SMTP_PASSWORD`,
`RENT_ALERT_BOT_TOKEN`. Сейчас они берутся из окружения процесса и (на
Windows) из `HKCU\Environment`; ни переменные профиля пользователя
(`systemctl --user`), ни `HKCU` (служба под LocalSystem) службам комплекта
не видны.

Решение — раздел `secrets` в `homensai-kit.json` ([KIT.md, 6.1](../KIT.md#61-секреты-продукта)):
установщик запрашивает значения до изменений (или берёт из переменных
окружения с теми же именами при `--yes`), хранит их в файле владельца вне
корня установки (Linux `~/.config/homensai/homensai-rent/secrets.env`, 0600;
Windows `%ProgramData%\HomenSAI\.secrets\homensai-rent\secrets.env`, только
SYSTEM и Administrators), а `homensai_entry.py` передаёт объявленные имена в
окружение каждого компонента и миграции. Коду Rent и адаптеру читать файл не
нужно — достаточно `os.environ`, как сейчас. Смена значения —
`setup.sh secrets ИМЯ` с перезапуском служб.

Предлагаемое объявление (раздел 6.1): `HOMENSAI_RENT_DEV_KEY` — обязательный;
`RENT_BOT_TOKEN`, `BACKUP_PASSWORD`, `RENT_SMTP_PASSWORD`,
`RENT_ALERT_BOT_TOKEN` — необязательные (бот без токена ждёт перезапуска).
Что нужно принять осознанно:

- файл секретов **не входит** в зашифрованный архив обновления;
  `HOMENSAI_RENT_DEV_KEY` нельзя терять (без него данные не расшифровать),
  поэтому копия ключа хранится вне компьютера;
- `uninstall` файл сохраняет, `uninstall --purge` удаляет вместе с данными;
- генерировать `HOMENSAI_RENT_DEV_KEY` (`generate: true`) можно только для
  новой базы: для переноса существующих данных нужен прежний ключ.

### 4.6. Остановка по SIGTERM и Ctrl+C / остановке службы

Состояние Rent — по коду; поведение с адаптером — измерено на Linux (3.3):

| Компонент | Сейчас в Rent | С адаптером |
|---|---|---|
| `app.web` | ловит только `KeyboardInterrupt`; SIGTERM — завершение по умолчанию (без `finally`, пульс не останавливается) | SIGTERM/SIGBREAK → `KeyboardInterrupt`; код 0 за 4,3 с |
| `app.bot` | только `KeyboardInterrupt`; long polling `getUpdates` с `POLL_TIMEOUT = 25` с | то же; в ожидании без токена — код 0 за 0,7 с. С токеном во время `getUpdates` **не проверено**; на Windows прерывание блокирующего чтения сокета может ждать до конца таймаута (25 с < `stoptimeout` 30 с — впритык) |
| `app.scheduler` | SIGTERM → `event.set()`; ожидание `event.wait(60)` прерывается сразу | код 0 за 5,6 с; SIGTERM во время долгого импорта тоже нужно перехватывать (ошибка первого черновика, 3.3) |

SIGTERM во время первого старта `web` (создание схемы, до 5 минут под
нагрузкой) обработчиком адаптера не перехватывается — процесс завершается по
умолчанию. Каждая миграция Rent — своя транзакция, поэтому база останется
целой, но **с частью схемы**; следующий старт увидит «Схема базы отстала» и
откажется работать, а команду `migrate` комплект при установке не вызывает.
Нужно: создавать новую базу во временном файле (`rent.sqlite3.creating`) и
переименовывать после последней миграции, либо в адаптере при отставшей
схеме **новой** базы (нет данных хозяйства) догонять схему самостоятельно.
Не реализовано.

WinSW в комплекте: `<stoptimeout>30 sec</stoptimeout>`
(`shared/kit_windows.py`); после него процесс снимается принудительно.
SQLite в режиме WAL переживает принудительное завершение (незавершённая
транзакция откатывается), поэтому риск — не порча базы, а выход за 30 с и
«грязный» код завершения.

### 4.7. Первая установка: гонка web ↔ bot/scheduler

`after: ["web"]` задаёт только порядок запуска, а базу создаёт `app.web`.
`app.scheduler` без файла базы завершается (`parser.error`), `app.bot` сам
применяет миграции без копии (`_open → migrate_up`) — две программы могут
одновременно создавать схему. Нужно: bot и scheduler ждут готовности схемы
(в адаптере — до 120 с), а не создают её.

### 4.8. Бот без токена

`app.bot` без `RENT_BOT_TOKEN` выходит с кодом 2 → перезапуски службы
(`restart.max = 5` за 600 с) → служба остаётся упавшей. Health смотрит
только web, поэтому установка «успешна», а в журнале — цикл падений.
Предложение: адаптер ждёт без падения и пишет одну строку в журнал; либо
отдельный режим `web-scheduler` без бота (режимы выбираются при установке и
между версиями не меняются).

### 4.9. Подпроцесс разбора PDF не видит зависимостей

`app/banking/pdf.py` запускает
`[sys.executable, "-B", "-m", "app.banking.pdf_worker", …]` с `cwd=src`.
Загрузчик `homensai_entry.py` добавляет `pydeps/<платформа>` в `sys.path`
только своего процесса, дочерний процесс его не наследует → `pypdf` не
импортируется (проверено, 3.3). Хуже того, воркер ловит любое исключение и
сообщает «повреждение или превышение лимита», так что владелец увидит
неверную причину. Исправление в Rent: при
`HOMENSAI_RELEASE_DIR` запускать
`[sys.executable, "-I", "-B", <RELEASE_DIR>/homensai_entry.py, "app.banking.pdf_worker", …]`
(или передавать `PYTHONPATH` из текущего `sys.path`).

### 4.10. Прочее

- Собственный установщик Rent (`app.ctl install/update/…`, `rent.toml`,
  `services.py`, `worker.py`) в комплекте не используется; `app.ctl
  status/doctor --db …` остаются полезны для диагностики.
- `heartbeat.file` манифеста комплектом, по поиску в `shared/kit*.py`, не
  читается; Rent пишет пульсы в таблицу `service_heartbeats` — это не
  мешает.
- Секции `backup`/`restore` манифеста обязательны по схеме, но сценарии
  комплекта их не вызывают (поиск по `shared/kit*.py`); полный архив делает
  сам установщик. В шаблоне ниже они указывают на будущие команды адаптера
  `backup`/`restore`, которых в черновике **нет**.
- Порт 8820 и `127.0.0.1` совпадают с Rent; другой адрес Rent не принимает.
- Состав компонентов (web, bot, scheduler) и архив PBS между версиями
  менять нельзя — для Rent это значит: платформенный режим
  (`app.core_server`) — отдельный продукт/комплект, не режим этого.

## 5. Предлагаемый адаптер `app/homensai_adapter.py`

Один новый модуль в Rent, существующие входы не меняются. Черновик лежит в
`~/.cache/homensai-rent-kit/project-v2/app/homensai_adapter.py` и прошёл
смоук v2 (раздел 3.3). В Rent **не применён**.

Команды: `web --data-dir D --port N`, `bot --data-dir D`,
`scheduler --data-dir D`, `migrate --data-dir D --migration ID`.

Ключевые части:

```python
DB_NAME, FILES_DIR = "rent.sqlite3", "files"
HEALTH_PATH = "/homensai/health"
# Секреты (HOMENSAI_RENT_DEV_KEY, RENT_BOT_TOKEN, …) уже в os.environ: их передаёт
# homensai_entry.py комплекта из файла владельца (раздел 4.5).

def stop_as_keyboard_interrupt() -> None:
    def handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)

def health_wsgi(app, db_path: Path, data_dir: Path, port: int):
    product = os.environ.get("HOMENSAI_PRODUCT_ID", "homensai-rent")
    release = os.environ.get("HOMENSAI_RELEASE", "dev")
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    def wsgi(environ, start_response):
        if environ.get("PATH_INFO") == HEALTH_PATH and environ.get("REQUEST_METHOD") in ("GET", "HEAD"):
            if environ.get("HTTP_HOST", "") not in hosts:
                start_response("400 Bad Request", [("Content-Length", "0")])
                return [b""]
            healthy = _schema_ready(db_path) and os.access(data_dir, os.W_OK)
            body = json.dumps({"product": product, "release": release, "healthy": healthy}).encode()
            start_response("200 OK" if healthy else "503 Service Unavailable",
                           [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
            return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]
        return app(environ, start_response)
    return wsgi

def cmd_web(args):
    db_path, files = _paths(args.data_dir)          # D/rent.sqlite3, D/files
    sealing.require_working_key()                   # WorkingKeyError → SystemExit
    cli.open_database(db_path)                      # нет файла — актуальная схема; отставшая — отказ
    app = WebApp(lambda: db.connect(db_path), port=args.port, archive=files, db_path=db_path)
    server = cli.serve(health_wsgi(app, db_path, args.data_dir, args.port), cli.HOST, args.port)
    stop_pulse = threading.Event()
    heartbeat.start_thread(lambda: db.connect(db_path), "web", stop_pulse)
    stop_as_keyboard_interrupt()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_pulse.set()
        server.server_close()
    return 0

def cmd_bot(args):
    db_path, files = _paths(args.data_dir)
    stop_as_keyboard_interrupt()
    try:
        _wait_for_schema(db_path)                   # до 120 с, базу создаёт web
        if not os.environ.get("RENT_BOT_TOKEN", "").strip():
            print("RENT_BOT_TOKEN не задан; бот ждёт перезапуска службы.", file=sys.stderr)
            threading.Event().wait()
        return bot_main(["--db", str(db_path), "--data-dir", str(files), "--scheduler-managed"])
    except KeyboardInterrupt:
        return 0

def cmd_scheduler(args):
    db_path, files = _paths(args.data_dir)
    stop_as_keyboard_interrupt()
    try:
        _wait_for_schema(db_path)
        from app.scheduler.cli import main as scheduler_main   # импорт под нагрузкой > 40 с
        return scheduler_main(["--db", str(db_path), "--data-dir", str(files)])  # дальше свой SIGTERM → event.set
    except KeyboardInterrupt:
        return 0

def cmd_migrate(args):
    db_path, _ = _paths(args.data_dir)
    if not db_path.exists():
        return 0                                    # первая установка: схему создаст web
    conn = db.connect(db_path)
    try:
        done = runner.apply_pending(conn, runner.load_all())   # scripts.migrate
    except runner.MigrationError as exc:
        print(f"Миграция не выполнена: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps({"migration": args.migration, "applied": len(done)}))
    return 0
```

Что ещё нужно в Rent помимо адаптера: исправление 4.9 (PDF-подпроцесс),
проверка «новые SQL-файлы ⇒ новый ID» (4.4), тесты адаптера (health-контракт,
SIGTERM, `migrate` дважды, бот без токена), раздел в `docs/INSTALL.md` про
`setup.sh secrets`. Перед применением — решение владельца по 4.5 и 4.8.
Черновик в `project-v2` ещё содержит прежний `load_secrets` из
`@data/secrets.env` — при применении его нужно убрать.

## 6. Готовые шаблоны для Rent

Предполагается, что `homensai-kit.json` лежит в корне репозитория Rent, а
`installer/` — рядом. Lock — существующий
`requirements/rent-runtime.lock` (в пробе он скопирован как
`installer/linux.lock` и `installer/windows.lock`; файлы одинаковые). Пути к
PBS/WinSW — абсолютные из `~/.cache/homensai-kit-inputs` (так было в пробе);
для репозитория их лучше перенести в `installer/inputs/` вне Git.

### 6.1. `homensai-kit.json`

```json
{
  "format": 1,
  "manifest": "installer/manifest.json",
  "sources": [
    {"from": "app", "to": "src/app"}, {"from": "rent", "to": "src/rent"}, {"from": "render", "to": "src/render"},
    {"from": "scripts/migrate.py", "to": "src/scripts/migrate.py"},
    {"from": "scripts/check_schema.py", "to": "src/scripts/check_schema.py"},
    {"from": "scripts/freeze_migrations.py", "to": "src/scripts/freeze_migrations.py"},
    {"from": "scripts/build_import_template.py", "to": "src/scripts/build_import_template.py"},
    {"from": "migrations", "to": "src/migrations"}, {"from": "templates", "to": "src/templates"},
    {"from": "design", "to": "src/design"},
    {"from": "docs/HELP.md", "to": "src/docs/HELP.md"}, {"from": "docs/EXCEL_FORMAT.md", "to": "src/docs/EXCEL_FORMAT.md"},
    {"from": "docs/legal", "to": "src/docs/legal"},
    {"from": "pyproject.toml", "to": "src/pyproject.toml"}
  ],
  "python_paths": ["src"],
  "targets": ["linux-x86_64", "windows-x86_64"],
  "locks": {"linux": "requirements/rent-runtime.lock", "windows": "requirements/rent-runtime.lock"},
  "wheelhouse": "installer/wheelhouse",
  "runtime": {
    "linux": {"archive": "installer/inputs/cpython-3.14.10+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
              "sha256": "4d3c16c59ed0802a3d1d34d8bce504b15ceeaff39d62dadecc6a9fa803412aed",
              "origin": "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10%2B20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
              "python_version": "3.14.10"},
    "windows": {"archive": "installer/inputs/cpython-3.14.10+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
                "sha256": "c1876a9795d3a94ee49a1499a69cd91e26ca3e2829593f93fa6e4db90d05402b",
                "origin": "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10%2B20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
                "python_version": "3.14.10"}
  },
  "winsw": {"exe": "installer/inputs/WinSW-x64.exe", "license": "installer/inputs/LICENSE.txt",
            "sha256": "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"},
  "migrations": ["rent-schema-0.4.0a12"],
  "modes": ["autonomous"],
  "estimates": {"data": 1073741824, "config": 16777216},
  "secrets": [
    {"name": "HOMENSAI_RENT_DEV_KEY", "required": true, "prompt": "ключ шифрования данных Rent (храните копию вне компьютера)"},
    {"name": "RENT_BOT_TOKEN", "required": false, "prompt": "токен Telegram-бота"},
    {"name": "BACKUP_PASSWORD", "required": false},
    {"name": "RENT_SMTP_PASSWORD", "required": false},
    {"name": "RENT_ALERT_BOT_TOKEN", "required": false}
  ]
}
```

`locks` с одним файлом на обе ОС допустим, если lock содержит хэши wheels
обеих платформ (у Rent так). В пробе использовались отдельные копии — с
общим файлом сборка **не запускалась**. До исправления 2.1 сборка с
исходным `segno` не пройдёт.

### 6.2. `installer/manifest.json` (шаблон, без `runtime`)

```json
{
  "schema_version": 2,
  "product_name": "HomenS.AI Rent",
  "product_id": "homensai-rent",
  "archive_before_replace": true,
  "components": [
    {"name": "web", "cmd": ["@module", "app.homensai_adapter", "web", "--data-dir", "@data", "--port", "8820"], "after": [],
     "heartbeat": {"file": "heartbeats/web.json", "interval": 60, "timeout": 180}, "restart": {"delay": 10, "max": 5, "window": 600}},
    {"name": "bot", "cmd": ["@module", "app.homensai_adapter", "bot", "--data-dir", "@data"], "after": ["web"],
     "heartbeat": {"file": "heartbeats/bot.json", "interval": 60, "timeout": 180}, "restart": {"delay": 30, "max": 5, "window": 600}},
    {"name": "scheduler", "cmd": ["@module", "app.homensai_adapter", "scheduler", "--data-dir", "@data"], "after": ["web"],
     "heartbeat": {"file": "heartbeats/scheduler.json", "interval": 60, "timeout": 180}, "restart": {"delay": 30, "max": 5, "window": 600}}
  ],
  "web": {"host": "127.0.0.1", "port": 8820, "healthz": "/homensai/health"},
  "secrets": [],
  "modes": [{"name": "autonomous", "components": ["web", "bot", "scheduler"]}],
  "mutual_exclusion": [],
  "migrations": {"cmd": ["@module", "app.homensai_adapter", "migrate", "--data-dir", "@DATA_DIR@", "--migration", "@MIGRATION_ID@"],
                 "backup_before_each": true, "new_db_no_backup": false},
  "backup": {"cmd": ["@module", "app.homensai_adapter", "backup", "--data-dir", "@data"], "encrypted": true},
  "restore": {"cmd": ["@module", "app.homensai_adapter", "restore", "--data-dir", "@data"]},
  "update": {"staged_releases": true, "pointer_file": "active.json", "state_file": "update.json", "health_wait": 180,
             "backup_before_rollback": true, "retain_previous_release": true, "retain_runtime": true},
  "hooks": {"pre_start": [], "health_checks": [], "post_update": []}
}
```

`secrets` манифеста пуст намеренно: секреты объявлены в `homensai-kit.json`
(4.5), непустой `secrets` шаблона сборщик отклоняет. `health_wait` (ожидание health при обновлении) поднят до
180 с: под нагрузкой один импорт `app.scheduler` занял больше 40 с (3.3);
при обновлении схема не создаётся заново, поэтому 292 с первого старта сюда
не относятся. После установки сборщик ждёт health фиксированно — 30 с на
Linux (`shared/kit_setup.py`, `health(..., wait=30)`) и 60 с на Windows
(`shared/kit_windows.py`), и при неудаче лишь сообщает «состояние
неизвестно»; для Rent с его первым стартом это ожидаемо и не ошибка, но
владельцу стоит это объяснить в README комплекта. В собранном пробном v2 стояло `"health_wait": 90`; на результат смоука это не влияло (сценарий обновления не запускался).

## 7. Что не проверено

- Установка, обновление, откат, восстановление и удаление Rent комплектом
  (`setup.sh install/update`, `tests/run_kit_e2e.py`) — не запускались;
  по правилам модуля без них установщик для Rent готовым считать нельзя.
- Windows целиком для Rent (pydeps, WinSW, остановка службы, ACL `@data`).
  Доставка секретов службам LocalSystem и миграциям проверена на
  демонстрационном продукте (`tests/run_kit_windows_e2e.py`), не на Rent.
- Сборка с единым lock-файлом и с исходным `segno` после исправления 2.1.
- Полные тесты Rent не запускались (вне задачи; машина перегружена).
- Время одного прохода планировщика и поведение бота при SIGTERM во время
  `getUpdates` на Windows.

## 8. Где лежат артефакты пробы

- `~/.cache/homensai-rent-kit/project`, `project-w`, `project-v2` — копии проекта
  (исходная, с обходом segno, с исправленной раскладкой и адаптером).
- `~/.cache/homensai-rent-kit/dist/homensai-rent-0.4.0a12` (v1),
  `~/.cache/homensai-rent-kit/dist-v2/homensai-rent-0.4.0a12` (v2).
- `~/.cache/homensai-rent-kit/logs/` — журналы сборок и смоука;
  `~/.cache/homensai-rent-kit/tools/` — `strip_data.py`, `deps_check.py`, `smoke_v2.sh`.
- Ключ `~/.cache/homensai-rent-kit/keys` — только для пробы, не для выпуска.
