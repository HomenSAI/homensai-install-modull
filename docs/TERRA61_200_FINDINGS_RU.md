# Находки Terra: очередь из 200 задач

Факты и гипотезы разделяются. Не публиковать секреты или пользовательские данные.

## F-001 — устаревший tracking ref

Категория: документация/синхронизация. Подтверждено: `git status` показывает
ahead 26, но `git ls-remote --heads origin codex/universal-installer-20261025`
перед работой вернул тот же `0b97bbcd6fdbbcfdebea0f83068bc432a3d6b429`, что
локальный HEAD. Это устаревшее локальное представление, не доказательство
26 неотправленных commits. Следующее действие: подтверждать серверный ref напрямую.

## F-002 — старые промежуточные формулировки в handoff

Категория: документация. В `docs/NEXT_AI_HANDOFF_RU.md` и `docs/RESUME.md`
исторические WIP/NOT RUN/RUNNING соседствуют с более поздними PASS. Факт
наличия текста подтверждён чтением; приоритетное исправление — отделить историю
от текущей сводки без потери первичных доказательств. Изменение ещё NOT RUN.

Обновление CP-20261028T155029Z-35a69bd61366: добавлены актуальные сводки
в RESUME/handoff и явная метка исторических checkpoint; исходный текст сохранён.
Проверка: diff/check и просмотр вступлений. F-002 исправлена в этом scope;
машинный state и другие исторические документы проверяются отдельно.

## F-003 — исторический CI FAIL

Категория: проверки/среда. План ссылается на Linux run 40709901833: три FAIL
prerequisites; его фактические jobs и причина повторно проверяются в задачах 021+.
Предположение о конкретном файловом свойстве runner пока не подтверждено.

Проверка baseline 0b97bbc, GitHub run 40713926857, job 122906339902:
Linux source 2386 PASS / 3 FAIL / 1 skip / 10 subtests; integration skipped.
FAIL: `test_real_fixed_runner_is_reached_only_after_signed_pins_and_consent`
получил RECOVERY_REQUIRED вместо AWAITING_USER; `test_real_exact_argv_has_no_shell_or_parent_secrets`
и `test_timeout_returns_unknown_without_claiming_or_killing_system_installer`
получили INTEGRITY_FAILED / prerequisite_executable_not_private_regular.
Windows portable job 122906340522 PASS. Сравнение: run 40353896055 PASS
обеих платформ, runs 40708599284 и 40709901833 FAIL на Linux.
Это не доказывает конкретную причину отказа файла; read-only probe ещё необходим.

`homensai_install/prerequisite_runner.py:38`: общий reason возникает при
необычном типе файла, nlink != 1, размере >512 MiB или group/world-write.
`tests/test_prerequisites_runner.py:18` использует resolved sys.executable и
его SHA, поэтому свойства фактического Python runner существенны. Эти условия —
возможные причины, ни одна из них не подтверждена для GitHub runner без probe.

## F-004 — верхние sync/checkpoint поля относятся к прошлой эпохе

Категория: документация/восстановление. Подтверждено: `work-state.json:3`
содержит CP-20261026-012, `last_confirmed_github_sha` — SDK CP011; внешняя
квитанция CP-20261028T144945Z-e156069e37b1 подтверждает более новый commit
5cd601b и tree cd4df39456bdb9e92ca87923a0b1845abd106b42. Риск: читатель
может принять старое поле за текущий ref. Причина и изначальная семантика поля
не доказаны: предполагается исторический source checkpoint. Использовать Git
и внешние квитанции как источник ref; не записывать будущий SHA своего commit.
Следующее: явно обозначить различие source/artifact/checkpoint/remote, сохраняя
исторические сведения и не нарушая существующих consumers JSON.

Обновление CP-20261028T155158Z-6d4706d4e304: добавлены scope-пояснения рядом
с историческими полями, значения не заменены; JSON валиден. F-004 исправлена
как неоднозначность описания, полная автоматизированная совместимость state
не заявляется. Следующий checkpoint определяется из Git/внешней receipt.
