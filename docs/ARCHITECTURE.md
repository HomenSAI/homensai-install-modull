# Границы общего модуля и правила доверия

Автор: Serhii Khomenko. Checkpoint: CP-20261025-008. HIM-008 — IN_PROGRESS:
архитектурные границы описаны; полный выпускной pipeline, Windows/service
интеграция и лицензии ещё не закрыты.

| Слой | Обязанности | Текущая реализация |
| --- | --- | --- |
| Общий API и lifecycle | Валидация, reviewed plan, согласия, ownership/locks, operation journal, staging/recovery, данные/архивы | InstallerAPI, BackendRouter, NativeEngine, DockerEngine, сохранённое shared ядро |
| Platform ports | Доверенные subprocess/manager/daemon, права, process identity, технические prerequisites | DockerCLI, SystemdUserManager/Runner, signed prerequisite coordinator/adapters; Windows contracts отдельно от выполнения |
| Product adapter | Состав данных, schema/migrations, согласованная остановка, health, post-restore | Версионированный executable protocol и signed product manifest/hooks; реальные JobPilot/Rent ещё не приняты |

SDK, CLI и web-мастер используют тот же публичный фасад. Bootstrap выбирает
доверенные verifier/ports/registry; HTTP никогда не выбирает Python class,
произвольную elevated command, trust key или daemon endpoint. Нет нового
независимого updater в PowerShell/bash/NSIS. Существующие legacy entrypoints
сохраняются совместимыми и не объявляются автоматически новым принятым engine.

BackendRouter связывает mode/scope/OS/arch с планом и resume. Неустановленный
backend возвращает CAPABILITY_UNSUPPORTED. Нормализация v1 сохраняет отсутствие
native runtime/hooks; пример продукта не превращается в готовую поставку.

## Владение, процессы и данные

Installation identity включает product/installation/owner/mode. Принадлежность
проверяется по receipts и фактическим ресурсам; имя файла, контейнера, порта
или PID само по себе недостаточно. Native и Docker не присваивают один корень.
Службы используют тот же root lifecycle lock, а native/user дополнительно
отказывает при зарегистрированных units или незавершённом service journal.
`removed:true` без завершённого daemon reload не разрешает user lifecycle.

DataHome отделён от code/runtime. Uninstall удаляет только собственные ресурсы;
пользовательские данные, архивы, recovery и общие prerequisites сохраняются.
Неизвестный writer, чужой listener, symlink/reparse/hardlink или несовпадающий
ownership вызывают отказ до опасного действия. Остановка требует quiescence
потомков/cgroups; принудительный kill продукта не подменяет согласованный backup.

Секреты передаются references через ограниченный provider. Их значения не
входят в argv, планы, события и export. Windows ACL/DPAPI/service credential
loader остаются отдельной незавершённой реализацией, без фиктивного PASS.

## Подпись, просмотр плана и восстановление

Pinned verifier подтверждает exact manifest/target/release/runtime и весь
signed file inventory. Review связывается с полным набором immutable bytes:
повторная подпись изменённого файла под прежним release не сохраняет старое
согласие. Digest идентифицирует просмотренный план; само его наличие не
доказывает пользовательское согласие с лицензией или elevated effect.

Перед необратимой границей сохраняется durable intent, после проверки — receipt.
Неизвестный исход команды требует reconciliation и explicit resume. Recheck
не запускает installer; PID/новое окно не доказывают reboot/relogin. Отсутствие
данных не считается поводом для fresh install. Источник recovery — operation
journal; DiagnosticStore и support export не заменяют его.

PrerequisiteCoordinator управляет только подготовкой платформенных зависимостей,
не product install/update/backup. Его READY требует fresh discovery, а core после
этого повторяет product preflight. API делегирует этот же coordinator; второй
worker/журнал эффектов через facade не создаётся. В standalone GUI auto-flow пока
не объявлен готовым и production registry не создаётся автоматически.

## Допуск зависимостей и текущие gates

Реестр build dependencies: requirements-build.lock и packaging/build-dependencies.json.
Точные source/toolchain/runtime inputs: packaging/ci-toolchain.json,
packaging/runtime-source-receipt.json и acceptance receipts. Правила полного
inventory/notices — docs/THIRD_PARTY_AUDIT*.md и tools/check_licenses.py.
Диапазон optional SDK dependency в pyproject.toml не заменяет release lock.

Unknown/blocked licence decision не допускает автоматическую prerequisite
установку. Conditional decision требует отдельных согласий. Docker Desktop,
Engine/Compose, images, WinSW, Python/frozen runtime и продуктовые зависимости
учитываются отдельно. Автоподключение production registry с выдуманными hashes
или флагом «approved» из HTTP запрещено самим контрактом.

Обязательное включение полного licence checker в production pipeline ещё
открыто. Исторические DEV bundles не выдаются за production artifacts; ключи
издателя, юридические тексты, clean-host и полный четырёхплатформенный lifecycle
остаются реальными gates. Права на main merge, публикацию release или изменение
рабочих JobPilot/Rent не следуют из успешных тестов разработческой ветки.
