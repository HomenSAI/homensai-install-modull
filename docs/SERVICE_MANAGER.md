# Linux systemd user: исполняемый platform port

Автор: Serhii Khomenko. Статус: developer SDK, `production_ready=false`.

`homensai_install.service_manager.SystemdUserManager` выполняет регистрацию,
enable/disable, start, graceful stop, status и uninstall **принадлежащих этой
установке** systemd user units. Это конкретный исполняемый adapter, а не только
генератор конфигураций. `NativeEngine`/`UpdatePlan` остаётся единственным
orchestrator install/update/backup/restore/rollback. Этот port не устанавливает
продукт, не создаёт архивы и не объявляет готовность полного service lifecycle.

## Интеграционный интерфейс

```python
from homensai_install.service_manager import SystemdUserManager
from homensai_install.service_systemd import SystemdUserRunner

# Эти зависимости выбирает trusted bootstrap, никогда не HTTP request.
runner = SystemdUserRunner(systemctl="/usr/bin/systemctl", busctl="/usr/bin/busctl")
manager = SystemdUserManager(
    verifier=pinned_verifier,
    runner=runner,
    unit_dir=runner.unit_dir,
    state_dir=install_root / ".hinstall/service-manager",
)
plan = manager.plan(
    manifest, target, options,
    install_root=str(install_root),
    installation_id=installation_id,
    modes=["local"], action="register",
)
review = plan.as_dict()
# Review/consent относится к exact digest, не к произвольному bool.
result = manager.apply(plan, consent=plan.digest)
```

Поддерживаемые `action`: `register`, `enable`, `start`, `stop`, `disable`,
`uninstall`. Для каждого следующего действия создаётся новый plan после
завершения предыдущего. `operation_id` по умолчанию случайный UUID hex; SDK
может передать свой ID из 32 hex символов. `status(plan)` ничего не устанавливает.
`ServicePlan.document` — canonical JSON bytes, `digest` — SHA-256 этих bytes.
Plan можно сохранить как часть lifecycle journal и восстановить
`ServicePlan(saved_bytes)` в новой сессии.

До plan orchestration уже создала `.hinstall/installation.json` и staged
подписанный release в `<install_root>/<paths.releases>/app-<release>`.
Сверяются product ID, installation ID, `uid:<current UID>` и `mode=native`.
Plan содержит только `inventory_file`, `signature_file`, `release`,
`runtime_executable` из options. Исходный `payload_dir` не является runtime:
перед effects проверяется **весь installed release**, включая runtime и
`service-contract.json`, тем же trusted verifier.

Reviewed plan и service receipt также хранят digest полного набора signed
file hashes. Даже заново подписанная замена runtime, notices или другого файла
под прежним release/descriptor требует нового review; старый apply/resume
возвращает PLAN_STALE до manager effects. Подпись подтверждает происхождение,
но не заменяет согласие с конкретными bytes уже просмотренного плана.

`register` может заменить конфигурацию уже принадлежащей этой установке службы
после доказанного полного stop. Проверяются старый receipt, старый unit hash,
его effective manager configuration и cgroup. Новая конфигурация приходит из
нового проверенного release. Выбор active release, применение миграций,
rollback конфигурации и порядок DataHome операций остаются в orchestration.
Продуктовые health checks также остаются там: `active/running` и kernel PID
identity сами по себе не доказывают готовность приложения.

## Границы ОС и запуска

Live runner поддерживает только текущего непривилегированного пользователя
Linux с уже работающим локальным user bus `/run/user/<uid>/bus`. Root, другой
пользователь, remote host, несовпадающие PID/proc namespaces, отсутствующий bus,
опасные paths и неподходящие manager properties дают явный отказ. Служба
помещается в `<passwd home>/.config/systemd/user`. Произвольный XDG_CONFIG_HOME,
system service/machine scope, sudo, включение linger и автоустановка systemd
этим port не поддерживаются. Отсутствие systemd не мешает отдельному
user-session backend.

`systemctl` и `busctl` задаются абсолютными paths из trusted SDK; файл и все
его предки должны принадлежать root и не быть доступны на запись группе/всем.
SHA-256 фиксируется при создании runner и перепроверяется перед вызовами.
Paths с symlink отклоняются. Client environment строится заново, без наследования
секретов, DBUS remote address или shell hooks. Нет shell commands.

Manager mutations ограничены `systemctl --user --no-ask-password
--job-mode=fail start/stop <exact-owned-name>` и `daemon-reload`.
Enable/disable изменяет только exact-owned symlink в `default.target.wants`;
чужие symlinks не удаляются. `daemon-reload` перечитывает user manager, поэтому
он явно указан среди effects review plan.

Вывод client ограничен 2 MiB, запросы и mutation имеют deadline. Ошибка не
включает stdout/stderr или environment. По timeout завершается только созданный
installer CLI client; service/cgroup не получает SIGKILL. Неизвестный результат
manager command остаётся pending в journal.

## Ownership и effective configuration

Наличие совпадающего имени, текста unit или файла недостаточно для adoption.
Нужен durable receipt/intent этой installation identity. Unit file должен быть
обычным owned файлом без hardlinks/symlinks и writable group/world permissions.
Проверяются directory ancestors, installation identity, receipt и hash.

Runner получает typed D-Bus properties через `busctl`; не разбирает
неоднозначную строку `systemctl show ExecStart`. Реальная проверка отдельным
private D-Bus подтвердила различие JSON: `call` возвращает body array,
`get-property` — непосредственно typed value. Parser различает оба формата.

Сверяются FragmentPath, отсутствие SourcePath/drop-ins/aliases/transient unit,
NeedDaemonReload, Type, exact executable/argv/cwd, User/Group, RootDirectory/
RootImage, Environment/EnvironmentFiles/PassEnvironment/UnsetEnvironment,
все дополнительные Exec hooks, UMask, NoNewPrivileges, KillMode, KillSignal,
SendSIGKILL, timeout/failure policies, restart delay, stdout/stderr policies.
Чужой override блокирует start/stop/uninstall, даже если основной unit hash верен.

Environment unit содержит только утверждённые технические значения и фиксированный
PATH; PYTHONPATH/PYTHONHOME/LD_PRELOAD/LD_LIBRARY_PATH удаляются. Наследуемое
manager environment допускает только проверенные account/home/runtime значения
и ограниченные locale values. Неизвестные имена, включая product secrets, дают
`CAPABILITY_UNSUPPORTED`; секретные values не попадают в error/receipt.
После start `/proc` подтверждает UID, executable, argv, cwd, ожидаемое environment,
отсутствие неутверждённого наследуемого environment, cgroup и неизменный process
start time. Изменившаяся identity не считается успешным запуском.

## Graceful stop и данные

Units используют `KillMode=control-group`, SIGTERM и `SendSIGKILL=no`.
Stop helper из signed contract должен синхронно дождаться закрытия приложения.
После manager stop port требует inactive/failed state, нулевые MainPID и
ControlPID, отсутствие job, `Result=success` и отсутствие потомков в сохранённом
или текущем cgroup. Для live host нужен cgroup v2: `cgroup.events populated=0`
включает потомков, в отличие от одного MainPID.

`Result=timeout`, crash/signal/exit-code, сохранившийся writer или неизвестный
cgroup после прерванного start не разрешают snapshot. Повторный stop не стирает
этот результат ради PASS; reset-failed/kill/killall не вызываются. Требуется
отдельная диагностированная recovery процедура. `quiescent=true` относится
только к принадлежащим units/cgroups; это не блокировка сторонних процессов,
которые самостоятельно открывают DataHome. Продуктовые quiescence/health gates
остаются обязанностью orchestration. Direct SDK apply сам удерживает общий
lifecycle lock установки, описанный ниже.

Обычный uninstall останавливает принадлежащие службы, удаляет только собственные
enable links и unit files, затем проверяет manager. DataHome, releases, архивы,
prerequisites и journal сохраняются.

## Durable effects и resume

`state.json` содержит installation binding, monotonic revision, receipts и
operation digest/steps. Перед каждым effect записывается `pending`;
`done` появляется только после проверки результата. JSON и units проходят
flush/fsync, atomic replace и fsync каталога. Первый unit публикуется через
Linux `renameat2(RENAME_NOREPLACE)`: только целый записанный файл и без
перезаписи внезапно появившегося destination. При недоступной операции — отказ.
`apply` сначала получает тот же постоянный lifecycle lock, что и NativeEngine:
`<install_root.parent>/.hinstall-<SHA256(canonical JSON install_root)[:24]>.lock`,
через существующий `shared.update_plan.operation_lock`. Затем он берёт свой
service flock. Общий lock удерживается до завершения всего apply/resume,
включая запись journal и проверку manager effects. Занятый lock вызывает
`LOCK_BUSY` до создания service journal, unit или manager mutation; после
освобождения тот же plan можно выполнить. Файл lock не удаляется/заменяется.

Это сериализует прямой SDK port с native/user без отдельного orchestrator.
Будущая интеграция port внутрь уже заблокированного NativeEngine должна явно
передавать или разделять существующую lifecycle lease. Нельзя вложенно получать
тот же flock через другой file descriptor: текущий `apply` намеренно откажет
с `LOCK_BUSY`. Публичного bypass-флага для обхода lock нет.

Повтор завершённого operation ID возвращает сохранённый результат с
`replayed=true`, не запускает service заново. Это исторический receipt;
актуальное состояние читается через `status`. Другой body с тем же ID
отклоняется. Новая операция со старой revision отклоняется. Незавершённая
операция блокирует другие mutations и требует:

```python
manager.apply(saved_plan, consent=saved_plan.digest, resume=True)
```

Resume читает реальные файлы и manager state. Уже состоявшийся start/stop/
reload/atomic publish/unlink не повторяется вслепую. Неизвестный или чужой
результат блокируется. Journal не удаляется для обхода recovery. После настоящего
process exit source test восстанавливает новый экземпляр manager из тех же
receipts. Это не является испытанием отключением питания.

## Проверки и оставшиеся gates

Scoped tests: `tests/test_service_contracts.py`, `tests/test_service_manager.py`,
`tests/test_service_systemd.py`. Они включают реальные временные файлы, signed
payload tamper, ownership/path/drop-in/argv/environment отказ, exact consent,
CAS, общий с NativeEngine lifecycle lock в обоих направлениях, child writer
при MainPID=0, stop timeout, interruptions между effects и
receipt, настоящий process exit, новый manager resume, bounded subprocess
timeout/output и реальный `/proc` child identity. Private D-Bus regression
проверяет реальный busctl transport JSON, **не systemd lifecycle**.

Sandbox с разными PID/proc namespaces не может выполнять kernel/private-bus
часть: tests явно пропускают её с причиной. В согласованном namespace эти tests
запускаются отдельно. Ни fake manager, ни частный D-Bus не засчитываются как
OS service acceptance.

Открыты live Linux systemd install/update/restore/rollback/uninstall, настоящий
graceful stop продукта, systemd restart policy, logout/login, reboot/autostart,
host ACL/credential cases, несколько target distributions/systemd versions и
NativeEngine интеграция. Windows WinSW/SCM остаётся signed renderer; Windows
adapter и VM acceptance не выполнены. Все исходные readiness gates сохраняются.

Основные источники protocol semantics:

- [systemd D-Bus API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)
- [busctl](https://www.freedesktop.org/software/systemd/man/latest/busctl.html)
- [systemd.kill](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)
- [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
