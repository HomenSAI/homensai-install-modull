# Локальный контроллер — шаг 5

© 2026 Serhii Khomenko · HomenS.AI

`shared/ctl.py` использует stdlib и существующие библиотеки манифеста, DataHome,
Docker-рендера, служб, ZIP и UpdatePlan. Контроллер не устанавливает продукт:
он проверяет контракт, показывает план, пишет текстовые заготовки и предоставляет
низкоуровневые операции ZIP. Реальные службы, Docker, миграции, hooks, HTTP,
переключение выпуска и шифрование не исполняются.

```text
python -m shared.ctl --manifest FILE [--json] COMMAND [параметры]
```

Запускать из корня репозитория на Python 3.14+. `main(argv) -> int` возвращает
код без завершения вызывающего процесса; модуль передаёт его в `SystemExit`.
`--manifest` обязателен перед командой. `--json` допустим перед командой или
после неё. `--help` и `COMMAND --help` показывают синтаксис (код 0).
В примерах ниже пути демонстрационные; продуктовые команды и зависимости
в примерах манифестов также не являются готовым payload.

| Код | Значение |
|---|---|
| `0` | Запрошенная локальная операция завершена; отсутствие checkpoint — штатный результат. Завершённый rollback в статусе также читается с кодом 0. |
| `1` | Операционная ошибка: файл отсутствует/недоступен, ZIP повреждён/другого продукта, непустое назначение restore, нехватка места, повреждённое или несовместимое состояние обновления. |
| `2` | Ошибка аргументов/контракта, неподдерживаемая версия/режим/pin WinSW, небезопасный путь, пересечение каталогов либо попытка заменить существующий результат рендера. |

В обычном режиме сообщения и названия списков русские, ошибки идут в stderr.
В режиме JSON stdout содержит один объект, stderr пуст:

```json
{"ok": true, "code": 0, "message": "Манифест корректен", "data": {"product_id": "homensai-jobpilot-pro", "schema_version": 1}}
```

На ошибке `ok: false`, `data: {}`, `code` совпадает с кодом возврата.
JSON сериализуется с ASCII escape для независимости от кодировки терминала;
после JSON-декодирования сообщения остаются русскими. `--help` выводит обычную
справку даже с `--json`. Окружение с секретами не читается. Произвольные argv,
тексты OSError/исключений адаптеров, содержимое/имена/хэши файлов ZIP не печатаются.
Ошибки валидатора содержат только известные пути полей и описание нарушения.
Публичные ID, пути, команды и манифест не должны содержать значения секретов:
распознавания произвольного секрета в разрешённой строке нет. Рендер записывает
публичные команды и ссылки на имена секретов, но не их значения; содержимое
сформированных файлов в stdout/JSON не возвращается.

## Команды и пути

| Команда | Обязательные параметры | Результат / побочные эффекты |
|---|---|---|
| `validate` | Только общий `--manifest` | Проверка JSON/схемы/ссылок; ID и версия. Ничего не пишет, пути продукта не обходит. |
| `plan-install` | `--install-root`, `--platform docker/linux/windows`; для служб v2 — `--mode` | `dry_run: true`, `directories`, `files`. Только списки app/releases/постоянных корней и Dockerfile/compose.yml либо XML/units выбранных компонентов. Не создаёт каталогов и файлов, не читает бинарник WinSW. |
| `render-docker` | `--output-dir` | Только v1: новые Dockerfile/compose.yml в указанном каталоге. Нет сборки, сети или запуска. v2 явно отвергается библиотекой. |
| `render-services` | `--install-root`, `--platform linux/windows`, `--mode`, `--output-dir` | Только v2: XML/units в указанном каталоге. Для Windows необходимы pin в манифесте и `--winsw-file` с совпадающим SHA-256. Бинарник только читается, не запускается. |
| `archive-create` | `--install-root`, `--archive` | Полный **незашифрованный** ZIP всех постоянных корней, включая существующие копии. Архив не перезаписывается; его родитель уже должен существовать. |
| `archive-verify` | `--archive` | Полная проверка состава, CRC/SHA-256 и product_id манифеста, без извлечения и записи. |
| `archive-restore` | `--install-root`, `--archive`, `--target` | Восстановление в существующий отдельный пустой каталог: `target/roots/<имя>`. После записи файлы перепроверяются. Живые пути не заменяются. |
| `update-plan-status` | `--install-root`, `--release`, один или несколько `--mode` | Чтение checkpoint через `UpdatePlan.status()`, без run/lock/mkdir/портов. JSON `state: null`, если транзакция ещё не начата, иначе проверенный state. |

`--mode NAME` повторяется для каждого выбранного режима; mutual_exclusion и
порядок after проверяются существующей библиотекой. Автоматического выбора
первого режима нет. Для Docker v1 режимы не поддерживаются.

Для v1 команды с DataHome (`plan-install`, `archive-create`, `archive-restore`)
требуют все три явных параметра: `--data-home`, `--app-dir`, `--releases-dir`.
Значение `data_home_env` из окружения процесса не берётся. Весь DataHome v1
архивируется как один корень `data`. Для v2 эти параметры запрещены, расположение
задаёт `paths` манифеста. CLI не создаёт продуктовые каталоги перед архивированием.

`--install-root` и пути адаптера v1 абсолютны и относятся к текущей ОС для
планирования, архива и статуса. Файловые аргументы `--manifest`, `--archive`,
`--target`, `--output-dir`, `--winsw-file` могут быть относительны к cwd.
Относительные пути v2 привязываются к корню установки, переменные не подставляются.
Все реальные файловые пути проходят `checked_path`: запрещены `..`, UNC,
неоднозначные диски/потоки, управляющие символы, конечные точки/пробелы,
зарезервированные имена устройств Windows и ссылки/reparse points в существующих
предках. Общий `checked_path` дополнен stdlib `ntpath.isreserved`, чтобы CLI и
библиотеки одинаково отказывали для NUL/CON/COM1 и недопустимых символов.
Файлы чтения должны быть обычными файлами; корень диска как файловая цель запрещён.

У `render-services` install-root — текст абсолютного пути **целевой** ОС:
допускается формирование Linux units на Windows и XML на POSIX. Лексически
небезопасный корень запрещён; реальные метаданные DataHome проверяются только
для текущей ОС. Для другой ОС доступность/ссылки будущей установки не проверяются;
на хосте записывается только безопасный `--output-dir`. Результат рендера —
тексты для проверки, а не подтверждение существования целевых путей/служб.

Рендер сначала проверяет отсутствие всех выходных имён, затем создаёт каталог
и открывает файлы эксклюзивно. Перезаписи нет. При ошибке записи может остаться
частичный результат; следующая попытка требует другого каталога. Транзакции
публикации нескольких текстов нет. Архив должен находиться вне app/releases
и всех архивируемых корней; restore запрещает пересечение в обоих направлениях
с любым живым корнем, app/releases, включая пустые каталоги.

## Примеры

```powershell
python -m shared.ctl --manifest examples/jobpilot.manifest.json validate
python -m shared.ctl --manifest examples/rent.manifest.json validate --json
python -m shared.ctl --manifest examples/jobpilot.manifest.json plan-install --platform docker --install-root C:\CLI-Demo --data-home C:\CLI-Demo\data --app-dir C:\CLI-Demo\app --releases-dir C:\CLI-Demo\releases
python -m shared.ctl --manifest examples/rent.manifest.json plan-install --install-root C:\CLI-Demo --platform windows --mode autonomous
python -m shared.ctl --manifest examples/jobpilot.manifest.json render-docker --output-dir build/cli-docker
python -m shared.ctl --manifest examples/rent.manifest.json render-services --install-root /srv/demo --platform linux --mode autonomous --output-dir build/cli-units
```

Windows-рендер требует отдельного публичного манифеста v2 с проверенным
`winsw.version/sha256` и `--winsw-file`. Пример Rent не содержит реального pin;
получить рабочий XML из него без дополнения нельзя. Windows-план показывает
только имена будущих XML и не подтверждает pin или готовность артефакта.

Пример низкоуровневого ZIP для уже остановленных вымышленных данных Rent
(все пять корней должны существовать; каталог ZIP и пустой target готовятся
вызывающей стороной):

```powershell
python -m shared.ctl --manifest examples/rent.manifest.json archive-create --install-root C:\CLI-Demo --archive C:\CLI-Review\snapshot.zip
python -m shared.ctl --manifest examples/rent.manifest.json archive-verify --archive C:\CLI-Review\snapshot.zip --json
python -m shared.ctl --manifest examples/rent.manifest.json archive-restore --install-root C:\CLI-Demo --archive C:\CLI-Review\snapshot.zip --target C:\CLI-Recovery
python -m shared.ctl --manifest examples/rent.manifest.json update-plan-status --install-root C:\CLI-Demo --release app-2.0 --mode autonomous --json
```

ZIP не исполняет `backup.cmd`/`restore.cmd` и **не выполняет** требование Rent
`backup.encrypted: true`. JSON всегда обозначает `encrypted: false`. Это отдельный
инструмент ZIP, он не подключён как backup/restore-порт UpdatePlan. Остановку
всех писателей, согласованность БД и исключение параллельного изменения путей
обеспечивает вызывающая сторона. Необходимы ФС с hard links и свободное место;
при ошибке восстановления частичный каталог остаётся. Ограничения архива,
подписей/ACL/DPAPI и гонок между stat/open — в [ARCHIVE.md](ARCHIVE.md).

Статус требует **исходные** манифест, целевой release, набор mode и порядок
миграций. По умолчанию единственная миграция `manifest`; повторяйте
`--migration-id NAME` для исходного списка либо задайте `--no-migrations` для
пустого списка. Неверный digest плана и нарушенные инварианты state дают код 1.
Чтение видит предыдущую или следующую атомарно сохранённую версию checkpoint,
не захватывает operation.lock и не разрешает конкурирующее управление.

`UnimplementedUpdatePorts` явно закрывает все методы UpdatePorts, а
`UnimplementedDockerPorts` — build/start/stop. Любой вызов поднимает
`UnimplementedPortError` с русским сообщением без аргументов. Команд исполнения
install/start/stop/update/rollback нет; такие аргументы дают код 2. Механизм
UpdatePlan остаётся библиотекой для будущих адаптеров, см. [UPDATE.md](UPDATE.md).

## Проверки

Тесты `tests/test_ctl.py` вызывают `main(argv)` и используют `tmp_path`:
оба примера, JSON/русский текст/коды, dry-run без записи, pin/режимы, отсутствие
значений синтетических секретов, полный ZIP round-trip, несовместимый продукт,
запрет живого restore, reparse/опасные пути/перезапись, проверенный статус без
эффектов, все незавершённые порты. Процессы в новых тестах запрещены, сеть
блокирует общая fixture. Symlink проверяется реально, если ОС разрешает его
создание; reparse проверяется также синтетическими метаданными без привилегий.

Использован прежний документированный WorkspaceTemps: он предоставляет
`tmp_path` через обычный `Path.mkdir()` внутри worktree, без изменения ACL и
без исключения файловых сценариев. Bytecode, кэш и сторонние plugins отключены.
Точная команда полного запуска:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTHONIOENCODING = 'utf-8'
@'
from pathlib import Path
import uuid
import pytest
class WorkspaceTemps:
    @pytest.fixture
    def tmp_path(self):
        path = Path.cwd() / 'build' / 'step5-pytest' / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path
raise SystemExit(pytest.main(['-q', '-r', 's', '-p', 'no:cacheprovider', '-p', 'no:tmpdir'], plugins=[WorkspaceTemps()]))
'@ | py -3.14 -B - | Tee-Object -FilePath build/step5-pytest-output.txt
```

Полный локальный запуск на Windows/Python 3.14, pytest 9.1.1: код 0.
Точный stdout:

```text
............................s........................................... [ 10%]
.................................................................s...... [ 21%]
...................s......s............................................. [ 32%]
........................................................................ [ 43%]
........................................................................ [ 53%]
........................................................................ [ 64%]
........................................................................ [ 75%]
........................................................................ [ 86%]
........................................................................ [ 97%]
...................                                                      [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_archive.py:290: ОС не разрешает создание symlink без дополнительных привилегий
SKIPPED [1] tests\test_ctl.py:461: ОС не разрешает создание symlink без дополнительных привилегий
SKIPPED [1] tests\test_datahome.py:96: Проверка пути Windows на POSIX
SKIPPED [1] tests\test_datahome.py:171: ОС не разрешает создание symlink без дополнительных привилегий
663 passed, 4 skipped in 79.07s (0:01:19)
```

Промежуточные проверки: только CLI до расширения сценариев —
`82 passed, 1 skipped in 5.50s`; CLI вместе с DataHome до последнего сценария
русского плана — `127 passed, 3 skipped in 5.62s`. Оба запуска дали код 0;
полный итоговый запуск выше включает все прежние и новые тесты.

Docker/native install/update/restore/rollback продукта не проверяются этими
тестами. Docker-проверка по CLAUDE.md не выполнялась: прямое задание запрещает
внешние службы. **Готовых установщиков по-прежнему нет.**
