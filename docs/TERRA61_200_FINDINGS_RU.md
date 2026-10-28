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

CP-20261028T155504Z-e22813731df1: local Docker probe на Python3.14.3:
regular=true, mode=0755, nlink=1, size=14480, SHA256
3fbc4d6c766a7a5d56c4bcc80d3325181a05f1b64596eb7cf622f0ec056dd546.
Это не runner evidence. В Linux CI добавлен отдельный read-only probe после
venv; permissions и pin actions сохранены. GitHub результат PENDING.

Обновление 2026-10-28 15:58 UTC: GitHub run 40720848658 на e201d11,
Linux job 122931024953 выполнил probe: Python3.14.9 regular=true, nlink=1,
size=17752, mode=0777, group_or_world_writable=true, unchanged_during_read=true,
SHA256 53c66318fe2966e23d7e361e8b1ec5c08d12cab5ee97b50847539f8645d08073.
Факт: в данном run mode 0777 удовлетворяет условию отказа `st_mode & 0o022`
в verify_regular_file. Linux source: те же 3 FAIL/2386 PASS/1 skip/10 subtests;
Windows portable PASS. Не установлено задним числом состояние runner в run95/101.
Следующий шаг: воспроизвести причину на своём временном файле и проверить
безопасную test fixture без изменения runtime или общего Python runner.

Локальная репродукция в offline DEV Docker: копия Python с неизменным SHA
при mode0777 получает INTEGRITY_FAILED; та же копия при 0755/nlink1 PASS.
Вывод для наблюдаемого runner: test fixture использует общий Python с
неподходящим mode. Предлагаемый точечный fix — собственная приватная копия
fixture с проверенным SHA; production-проверки не менять. Копия и весь тестовый
путь должны быть реально проверены, включая возможные зависимости runtime.
Альтернативы: изменение chmod общего toolcache недопустимо; ослабление
verify_regular_file нарушит защиту; skip скроет поведение. Выбрана приватная
копия только в test fixture, с неизменным SHA и реальным запуском через runner.
Локальный DEV Docker: копия успешно исполнила `python -I -B` с json/sqlite3;
GitHub Python3.14.9 и полный исходный набор требуют отдельного повторного запуска.
CP-20261028T160201Z-52634a2bf6fb: подготовлен test-only fixture patch;
запускаемость собственной копии CPython3.14.3 проверена, CI 3.14.9 ещё нет.
Исходный тест неправильного executable pin сохранён; runtime не изменён.
CP-20261028T160331Z-0a43ef8c1beb: 50 focused source cases PASS/0 skip
в offline DEV, включая три исходно падавших и новые writable/hardlink/symlink.
F-003 остаётся OPEN до CI на Python3.14.9; source для production не менялся.
CP-20261028T161418Z-e7a7c5c83951: GitHub run125 на 4bc2b1f PASS:
Linux source2392/1skip/10subtests, integration236/2skip/5subtests, Windows
portable549/4skip. Probe нового runner: тот же SHA Python3.14.9, но mode0755,
вместо0777 на ранее неудавшемся run117. Следовательно, успешный CI сам по себе
не доказывает эффективность fixture при unsafe source mode; F-003 остаётся OPEN
до контролируемого end-to-end воспроизведения. Причина изменения mode неизвестна.
Дополнительная локальная проверка после изменения общего conftest:
`docker run --rm --name hinstall-terra-200-state-regression --network none --user 1000:1000 --mount type=bind,source=E:/homensai/homensai-install-modull,target=/workspace,readonly --workdir /workspace hinstall-dev:verification python -m pytest -q -p no:cacheprovider tests/test_prerequisites_state_files.py`
завершилась exit 0, 53 PASS за 2.08 s в Linux DEV Python3.14.3. Это отдельный
набор с пересекающейся областью, не установленная платформа и не полная приёмка.

## F-005 — первая попытка воспроизведения не нашла модуль проекта

Категория: тестовая среда. Первоначальный запуск воспроизводителя из
`.local-state/terra-20261028-evidence/ci_mode_reproduction.py` завершился
`ModuleNotFoundError: homensai_install`: каталог checkout не входил в sys.path.
Это отказ harness до проверки защиты. Исправлено только способом запуска через
runpy с явно указанным `/workspace`; исходный FAIL сохранён в этом журнале.

## F-006 — воспроизводитель неверно читал атрибут исключения

Категория: тестовый harness. Вторая попытка получила ожидаемый INTEGRITY_FAILED,
но упала с AttributeError на `error.details`; публичные данные находятся в
`error.context["details"]`. Исправлено в локальном reproducer; третья попытка
подтвердила 0777 отказ и 0755 PASS. Скрипт: `.local-state/terra-20261028-evidence/ci_mode_reproduction.py`,
SHA256 fa5c63dda763bdb012b10c0217459b30406d939dc6f7651eacd4a102bf4f3440.

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
