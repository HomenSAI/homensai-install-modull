# Docker Compose — адаптер шага 7

© 2026 Serhii Khomenko · HomenS.AI

`shared/adapters_docker.py` реализует низкоуровневые порты управления сервисом
и HTTP health для Compose, сформированного `shared/render_docker.py` из
публичного манифеста **v1**. По умолчанию каждый вызов возвращает план без
процессов, сети и записи. **Реальный Docker в этом шаге не запускался;
install/update/restore/rollback и Docker/native-приёмка не выполнены.**

## API

```python
from shared.adapters_docker import DockerComposeAdapter

adapter = DockerComposeAdapter(manifest, absolute_compose_path,
                               runner=fake_runner, fetcher=fake_fetcher,
                               timeout=30, health_timeout=3)
argv = adapter.start()       # list[str], только план
adapter.stop()              # list[str], только план
adapter.ps()                # list[str], только план
adapter.logs()              # list[str], только план
health_plan = adapter.health()  # {"url": ..., "timeout": 3}

# Разрешение задаётся отдельно для каждого вызова. В тестах только фейки.
result = adapter.start(execute=True)  # результат runner, CompletedProcess
healthy = adapter.health(execute=True)  # True только при HTTP 200
```

| Метод | Запланированный argv после общего префикса |
|---|---|
| `start()` / `command("up")` | `up`, `-d` |
| `stop()` / `command("stop")` | `stop` |
| `ps()` / `command("ps")` | `ps` |
| `logs()` / `command("logs")` | `logs` |

Общий префикс: `["docker", "compose", "-p", product_id, "-f", absolute_compose_path]`.
Произвольные действия/опции, build, down, hooks и миграции API не принимает.
`execute` принимает только bool; разрешение одного вызова не сохраняется.
Планирование допускает отсутствующий Compose и не создаёт его каталог.
Исполнение требует существующий обычный файл. Содержимое YAML адаптер не читает.

`runner(argv, **kwargs)` — единственная точка исполнения процессов. По умолчанию
это `subprocess.run`; внедрённый runner получает ровно тот же контракт:

```python
runner(argv, shell=False, timeout=30, check=False,
       stdin=subprocess.DEVNULL, capture_output=True, text=True,
       cwd=str(compose_path.parent), env={"COMPOSE_DISABLE_ENV_FILE": "1"})
```

Окружение родительского процесса не читается и не передаётся. Единственная
фиксированная переменная отключает автоматический Compose `.env`. Настройки
DOCKER_HOST/CONTEXT/CONFIG, proxy, HOME и секреты из окружения не наследуются.
Реальная доступность Docker executable/daemon с таким минимальным окружением
не проверялась. Если нужен иной runner, вызывающая сторона отвечает за его
безопасность, конечный timeout и отсутствие чтения секретов. Программа Docker
и доверенный Compose при явном исполнении имеют собственные эффекты; адаптер
не является песочницей и не анализирует содержимое YAML, payload или Docker
config. Следует передавать только проверенный результат рендера.

Ненулевой returncode, TimeoutExpired и ошибки runner дают `DockerAdapterError`
с фиксированным текстом без stdout/stderr/argv/исходного исключения. Успешный
результат runner возвращается вызывающему коду; вывод ps/logs может содержать
продуктовые данные, поэтому адаптер его сам не печатает и не журналирует.

## Пути и health

Манифест проверяется существующим валидатором; product_id соответствует
`[a-z0-9][a-z0-9_-]{0,62}`. Compose — абсолютный путь текущей ОС к `.yml`/`.yaml`,
проверяемый `checked_path` при создании адаптера и перед каждой командой.
Запрещены обход `..`, UNC, drive-relative/ADS, управляющие символы,
зарезервированные имена Windows, ссылки/reparse points, каталоги вместо файлов
и shell-метасимволы `; | & $` и другие подстановки/кавычки. Пробелы и кириллица
допускаются и сохраняются отдельным аргументом. Shell-строки не строятся.
Гонки злонамеренной смены пути между проверкой и внешним чтением не исключены.

Health URL берётся только из проверенного v1: `http://127.0.0.1:<port>/<path>`,
порт совпадает с манифестом; localhost/DNS, IPv6, другие адреса, credentials,
query и fragment отвергаются. Путь соответствует контракту манифеста, например
`/healthz` или `/health/ready`. Fetcher имеет контракт
`fetcher(url, timeout=health_timeout) -> int`. По умолчанию используется stdlib
urllib с `ProxyHandler({})` и запретом redirect; тело ответа не читается, ответ
закрывается. Успех — только целое `200`; другие статусы, ошибки соединения и
таймауты дают обобщённый `DockerAdapterError`. Оба timeout конечны, больше 0
и не больше 600 секунд. Выполняется один запрос, без polling/retry.

Эти методы не реализуют полный Protocol `UpdatePorts`: v2-компоненты,
durable key/receipts, pre_start/health hooks, остановка всех внешних писателей,
архив, migration и rollback требуют отдельного адаптера. Прямого подключения
к `FilesystemPorts` или `UpdatePlan` нет; v2 явно отвергается, как и рендером.
HTTP 200 и нулевой код Compose не доказывают принадлежность установки,
идентичность выпуска, фактическую остановку всех писателей или готовность
install/update/restore/rollback. Разные установки с одинаковым product_id
не изолированы по Compose project; владение и сериализацию обеспечивает
вызывающая сторона. Сборка образа и проверка digest также не реализованы.

## CLI: только план

```powershell
py -3.14 -B -m shared.ctl --manifest examples/jobpilot.manifest.json docker-plan --compose-file build/jobpilot/compose.yml --action up
py -3.14 -B -m shared.ctl --manifest examples/jobpilot.manifest.json docker-plan --compose-file build/jobpilot/compose.yml --action logs --json
```

`--action` обязателен: `up`, `stop`, `ps` или `logs`. JSON сохраняет существующий
конверт CLI, `data` содержит ровно `dry_run: true` и `argv: list[str]`. Текст
содержит русский статус и JSON-массив argv для сохранения границ аргументов.
Относительный `--compose-file` разрешается относительно cwd; файл может ещё
не существовать. Ничего не записывается, runner/fetcher не вызываются.
Флага `--execute` и команд исполнения в CLI нет. Общие коды: 0 — план построен,
2 — неверный ввод, 1 — ошибка локального I/O. V2 не поддерживается.

## Проверки

`tests/test_adapters_docker.py` проверяет каждый argv и execute-gate,
ошибки/таймауты всех команд, default subprocess.run через подмену callable,
непередачу синтетических env-секретов, product/path/action injection,
повторную проверку reparse, отсутствующий Compose и отсутствие записи в dry-run,
health success/failure/timeout через внедрённый fetcher, запрет proxy/redirect
и закрытие ответа default fetcher через фальшивый opener. CLI проверяется
в текстовом/JSON режимах для всех действий и явно отвергает `--execute`.
Реальные процессы в новом наборе запрещены; общая fixture запрещает сеть.

Адресная проверка адаптера и существующего CLI: код 0,
`229 passed, 1 skipped in 7.98s`; пропуск — symlink без привилегий Windows.

Полный запуск использует прежний WorkspaceTemps: обычный mkdir внутри worktree,
без изменения ACL и без исключения файловых тестов. Bytecode, кэш и сторонние
pytest plugins отключены. Команда из корня репозитория:

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
        path = Path.cwd() / 'build' / 'step7-pytest' / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path
raise SystemExit(pytest.main(['-q', '-r', 's', '-p', 'no:cacheprovider', '-p', 'no:tmpdir'], plugins=[WorkspaceTemps()]))
'@ | py -3.14 -B - | Tee-Object -FilePath build/step7-pytest-output.txt
exit $LASTEXITCODE
```

Полный локальный запуск на Windows/Python 3.14 завершился с кодом 0.
Точный stdout (копия `build/step7-pytest-output.txt`):

```text
........................................................................ [  8%]
........................................................................ [ 16%]
................s....................................................... [ 24%]
.....................................................s.................. [ 32%]
.......s......s......................................................... [ 40%]
........................................................................ [ 49%]
........................................................................ [ 57%]
........................................................................ [ 65%]
........................................................................ [ 73%]
........................................................................ [ 81%]
........................................................................ [ 90%]
........................................................................ [ 98%]
...............                                                          [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_archive.py:290: ОС не разрешает создание symlink без дополнительных привилегий
SKIPPED [1] tests\test_ctl.py:461: ОС не разрешает создание symlink без дополнительных привилегий
SKIPPED [1] tests\test_datahome.py:96: Проверка пути Windows на POSIX
SKIPPED [1] tests\test_datahome.py:171: ОС не разрешает создание symlink без дополнительных привилегий
875 passed, 4 skipped in 251.12s (0:04:11)
```

Включены все прежние тесты и 132 новых сценария адаптера/CLI. Три symlink-теста
пропущены из-за прав Windows, один Windows-на-POSIX неприменим на этом хосте.
`git diff --check` и отдельная проверка изменённых/новых файлов на конечные
пробелы и завершающий перевод строки завершились с кодом 0.

Контейнерные проверки из CLAUDE.md в этом шаге не выполняются: прямое задание
запрещает внешние службы и реальный Docker. Это локальные тесты на фейках;
**готовых установщиков и Docker/native-приёмки по-прежнему нет**.
