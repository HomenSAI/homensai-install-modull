# Проверка установленного prerequisites SDK/CLI

Автор: Serhii Khomenko. Профиль добавлен в CP-20261025-016.

`packaging/check_installed_prerequisites.py` собирает новый wheel, устанавливает
его в чистый venv вне checkout и запускает `installed_prerequisites_client.py`
как независимые внешние `python -I -B` процессы. Это installed signed synthetic
lifecycle; настоящий OS installer, продуктовый runtime и browser здесь не запускаются.

## Воспроизводимый запуск

Нужны Linux x64, CPython 3.14 build environment по `requirements-build.lock`
и локальный wheelhouse с совместимыми pinned cryptography/cffi/pycparser.
Каждая попытка использует новый work directory и новый receipt path:

```bash
PYTHONDONTWRITEBYTECODE=1 /path/to/build-env/bin/python -B \
  packaging/check_installed_prerequisites.py \
  --work-dir /tmp/hinstall-installed-prerequisites-new-run \
  --receipt /tmp/hinstall-installed-prerequisites-new-run.json \
  --wheelhouse /path/to/pinned-wheelhouse
```

Пути нужно заменить фактическими проверенными путями среды. Для связи с прежним
FAIL добавить `--previous-receipt /path/to/previous.json`; прежняя попытка
не перезаписывается. Скрипт сохраняет PASS/FAIL/BLOCKED receipt с командами,
source/package/client/harness/wheel/dependency hashes и точной областью.

Build и установка выполняются offline. Security wheels выбираются по платформе
и hashes из общего lock, устанавливаются с `--no-index --require-hashes
--only-binary=:all:`. До установки подтверждается отсутствие fallback на checkout
и отсутствующие зависимости. Package bytes сверяются с source/wheel, dependency
bytes — с выбранными wheels; после клиентов сравнение выполняется повторно.

## Исполняемые сценарии

| Фаза | Что подтверждается |
|---|---|
| audit_guard | Отдельный отрицательный контроль запрещает source read, DNS и subprocess до эффекта. |
| tampered | Неверная настоящая Ed25519 подпись отвергается до coordinator/effects. |
| manual_prepare → manual_restore | Сохранённый план и ручное ожидание читаются новым процессом; явный recheck достигает READY по synthetic inventory. |
| auto_prepare → auto_restore | Fixed fake installers соблюдают dependency order и postconditions; отброшенный ответ и повтор start не повторяют effects. |
| cancel_prepare → cancel_restore | Новый процесс видит сохранённый CANCELED и историю. |
| cli_prepare → cli_restore | Настоящий CLI parser с trusted API injection: metadata/license/plan, отказ без согласий, start/status/replay/resume/list/events/attempts. |

Клиенты проверяют, что модули импортированы из установленного site-packages,
а product install/recovery/state directories не созданы. Обычные lifecycle
фазы должны иметь ноль запрещённых audit attempts; ожидаемые отказы отдельного
audit_guard не смешиваются с ними. Transport, inventory и installer
синтетические, но подписи, downloader, планы, journal и locks настоящие.

## Фактический CP-016 результат

`packaging/acceptance-evidence/installed-prerequisites-20261025-cp016.json`:
10 independent clients и 11 CLI calls PASS; 63 module files и 161 dependency
files совпали до/после выполнения. Wheel SHA-256:
`7cdf5f5e73054f3ccfbd2b7f6622cb390e114c1d21efb68b907553ce66a5a507`.
Source parent — CP-015 `fde526435a7061cf9037251bfeb87bcc04bbe3f1`.

Первые два agent harness FAIL сохранены: клиент ошибочно ожидал CLI API version
`1` вместо `1.0`. Это исправление harness; product lifecycle не менялся.
Исторический agent PASS относится к CP-013 package и не подменяет root CP-016 run.
Root повторил профиль на новом wheel с CP-015 runtime bytes.

NOT RUN: настоящий OS prerequisite installer, process power-loss, полный
product lifecycle, current frozen/GUI, Windows/Docker, service/UAC/reboot/relogin
и production acceptance. Общий execution lease и durable byte progress
остаются NOT IMPLEMENTED; следующий разбор — `PREREQUISITES_NEXT_CORE_STEPS.md`.
