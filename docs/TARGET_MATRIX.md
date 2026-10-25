# Первая матрица targets и сред проверки

Автор: Serhii Khomenko. Checkpoint: CP-20261025-008.
Основание: PROMPT §7–9, §16; HIM-006/007. Это согласованный scope разработки
и проверок; наличие строки не означает принятую поддержку или готовый выпуск.

## Обязательные четыре варианта

| Target | Базовая ОС и архитектура | Профиль | Конечный artifact | Проверяемые языки |
| --- | --- | --- | --- | --- |
| Windows native | Windows 11 x64 | Текущий обычный пользователь, user-session | NSIS Setup.exe с frozen onedir/runtime и общим мастером | ru / en / de |
| Windows Docker | Windows 11 x64, Linux containers | Тот же пользователь, явно принятый совместимый Desktop/WSL2 | Тот же Setup.exe, отдельный выбранный Docker путь | ru / en / de |
| Linux native | Ubuntu 24.04 LTS x86_64, glibc | Непривилегированный user-session | Самодостаточный графический .run или проверенный эквивалент | ru / en / de |
| Linux Docker | Ubuntu 24.04 LTS x86_64, glibc | Явный локальный engine, отдельная установка/идентичность | Тот же Linux мастер, Docker payload с pinned images | ru / en / de |

В публичном target используются `os=windows/linux`, `arch=x64`,
`mode=native/docker`, `scope=user`. ARM64, Windows Server, другие Ubuntu/Debian,
RPM-системы, musl/Alpine и macOS не входят в первую принятую область. Расширять
её можно отдельными artifacts и проверками, не изменением одной capability.

Python-продукт получает собственный закреплённый runtime. SDK допускает Python
3.12+; build lock предназначен CPython 3.14 с GIL/x64. Это разные требования.
Системный Python конечного пользователя не используется как скрытая зависимость.

## User-session и службы

| Профиль | Реализация в source | Условие допуска |
| --- | --- | --- |
| Linux user-session | NativeEngine, реальные PID/pidfd/HTTP/SQLite lifecycle tests | Fresh recheck, подписанный release, ownership, отдельный DataHome |
| Linux systemd --user | SystemdUserManager, отдельный platform port | Нужна интеграция service engine и настоящий user manager/reboot acceptance; core `native/service` пока unavailable |
| Linux system service | Не заявлен реализованным | Отдельные identity/ACL/secrets/elevation и acceptance |
| Windows user-session | Bootstrap/renderer и contracts; полный engine не принят | Чистый Windows runner, настоящий установленный artifact |
| Windows service | Подписанный WinSW renderer, SCM lifecycle не подключён | Отдельный service account/ACL/credential loader, SCM/reboot acceptance |

Systemd не является prerequisite для Linux user-session. User service не
обещает работу до входа или после logout без явной политики session/linger.
Native/user и зарегистрированные service writers не должны одновременно
управлять DataHome: общий lifecycle lock и service receipt fence проверены.

## Фактические среды текущего запуска

| Среда | Доступ | Что доказано / чего она не доказывает |
| --- | --- | --- |
| Cloud Ubuntu 24.04.3 LTS x64, Python 3.12.14 | Есть | Source tests, установленный SDK, synthetic real Linux subprocess/loopback; не clean end-user host |
| Обычная sandbox shell | Есть | PID namespace не совпадает с mounted /proc; непригодна для положительного process-ownership acceptance |
| Process tests вне этой PID isolation | Есть, соответствие PID/proc проверено | Реальные собственные процессы и kernel liveness; systemd и Docker от этого не появляются |
| GitHub ubuntu-24.04 / windows-2025, CPython 3.14.9 | Workflow настроен | Наблюдавшиеся runs завершились до allocation runner/steps; source CI BLOCKED |
| Windows 11 владельца | Не подключена | UAC/ACL, Setup.exe, Desktop/WSL, UI и reboot NOT RUN |
| Docker daemon / systemd manager | Недоступны в текущей среде | Modeled daemon/manager tests не являются live acceptance |
| Docker Git / Forgejo владельца | Не подключён | Синхронизация в этот remote отдельно BLOCKED; cloud loopback его не заменяет |

Exact historical build toolchain и current SDK compatibility checks записаны
раздельно в BUILDING.md и packaging/acceptance-evidence. GitHub commit не
означает, что runner исполнил проверки.

## Три языка × четыре reference-пути

| Reference путь | ru | en | de |
| --- | --- | --- | --- |
| Windows native | NOT RUN | NOT RUN | NOT RUN |
| Windows Docker | BLOCKED: нет target runner | BLOCKED: нет target runner | BLOCKED: нет target runner |
| Linux native | PARTIAL: CP-005 frozen user-session | PARTIAL: CP-005 frozen user-session | PARTIAL: CP-005 frozen user-session |
| Linux Docker | NOT RUN: live daemon | NOT RUN: live daemon | NOT RUN: live daemon |

CP-007/008 source/SDK/JS checks не переносят исторический frozen PASS на новую
сборку. Все двенадцать сочетаний остаются обязательными для G2; частичный
Linux результат не закрывает строку clean-host/reboot/accessibility.

## Продукты и сценарии

Reference обязан пройти все четыре варианта. JobPilot и Rent подключаются
тонкими manifest/hooks после reference, отдельно на каждом объявленном target.
Актуальный source audit и точные продуктовые SHA — PRODUCT_AUDIT_20261025.md.
Наличие работающей прежней установки продукта не является приёмкой нового SDK.

Для каждого объявленного product/target обязательны: fresh v1; start/stop и
повторный start; v1→v2→v3; bad migration/health rollback; repair; before-rollback;
restore в новый корень; uninstall-preserve→reinstall; offline; чужой порт/процесс;
две учётные записи; crash/resume на опасных границах; полный переносимый архив.
Отдельные service профили добавляют logout/relogin/reboot и restart policy.
Каждый receipt указывает artifact/source hash, ОС, команду, exit/status и границы
своего доказательства. Общий список работ остаётся только в каноническом TODO.
