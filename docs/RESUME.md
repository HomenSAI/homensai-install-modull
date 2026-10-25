# Продолжение разработки

Checkpoint: `CP-20261025-003`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый remote до создания CP-003: `b2d99a2bf75aa8e35328f64c3b8b08dc618bceda`.
Фактический SHA текущего checkpoint определять по Git history/ref и внешней receipt.

## Восстановление контекста

Прочитать CLAUDE.md, полный PROMPT, канонический ToDo, work-state.json и
IMPLEMENTATION_STATUS.md. Не превращать частичный PASS в готовность выпуска.
Проверить branch/dirty files/staged files/активные процессы до новых действий.
Не сбрасывать чужие изменения и не завершать произвольный PID.

## Доступная среда

Облачный Linux x64; host Python 3.12, изолированный build/test Python 3.14.3.
Windows пользователя и Docker/Forgejo не подключены. Cloud Git не заменяет
Git владельца в Docker. GitHub доступен через connector; HTTPS Git credentials
в shell отсутствуют. Точный source snapshot импортирован с проверкой Git hashes.

## Что уже работает

Публичный пакет/CLI/API, strict manifest normalization, планы/согласия/журнал,
Ed25519 inventory с manifest/target/runtime binding, Linux user-session lifecycle,
ошибки/диагностика/локальная статистика и общие строки на ru/en/de.
Новые source tests: 1233 passed, 1 skipped; отдельные real loopback native/API tests.
Нативные службы ОС, Windows/Docker и frozen GUI acceptance пока не приняты.

## Следующая очередь

1. Завершить и проверить loopback GUI: три языка, consent, progress, resume, error help.
2. Собрать reference package с реальным Python runtime, wheel, ru/en/de legal и подписью.
3. Пройти реальную установку через GUI и frozen bootstrap на доступном Linux.
4. Закрыть crash-window process receipt, encrypted-only restore и candidate recheck.
5. Добавить безопасные prerequisite/download и portable Git checkpoint/resume tooling.
6. Продолжить platform service/Docker adapters и JobPilot/Rent integration с явными runner gates.
7. После каждой логической задачи обновлять ToDo/журнал, commit и каждый доступный remote.

## Постоянные ограничения

Не хранить publisher private keys, архивные пароли и product secrets в repo/URL/logs.
Тестовые ключи допускаются только с development marker; production_ready остаётся false.
Не открывать HTTP installer на внешнем интерфейсе. Не публиковать main/release без
разрешения. Каждый недоступный runner/remote фиксировать отдельно и продолжать
независимую работу; никаких фиктивных PASS.

## Дополнение CP-003

GUI/frozen first install принят на Linux; полный GUI04 остановился на recovery integrity violation. Не выдавать его за PASS uninstall. Новая source suite1363PASS1skip; 24integrationPASS. История текущих проверок — WORK_LOG; next clean bundle после exclusion BDB/ensurepip/Tcl.
