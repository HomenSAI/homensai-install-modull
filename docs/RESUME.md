# Продолжение разработки

Checkpoint: `CP-20261025-004`; ветка `codex/universal-installer-20261025`.
Baseline: `a16a55d58e65ccf64ed2ba0c853a0053baea6689`.
Последний подтверждённый GitHub/local до создания CP-004:
`db85d13811ea63b42142f97ca6a1e7fae76ba54c` (CP-003).
Фактический SHA текущего checkpoint определять по Git history/ref и внешней receipt;
не создавать рекурсивный commit ради записи собственного SHA.

## Восстановление контекста

Прочитать CLAUDE.md, полный PROMPT, канонический ToDo, work-state.json,
IMPLEMENTATION_STATUS.md, WORK_LOG.md и INSTALLER_API.md. Проверить branch,
dirty/staged files и активные процессы. Не сбрасывать чужие изменения и не
завершать произвольный PID. Частичный PASS не означает готовность выпуска.

## Среда и сохранение

Облачный Linux x64. Host Python3.12, isolated build/test Python3.14.3.
Windows владельца, Docker daemon и локальный Forgejo/Docker Git не подключены.
Cloud Git не заменяет Git владельца в Docker. GitHub доступен через connector;
HTTPS Git credentials в shell отсутствуют. Канонические commit/tree сверяются с
GitHub после каждого push; исходный snapshot импортирован с проверкой Git hashes.
Транспорт connector сохраняет draft checkpoint ref и затем точный remote commit.
В обычной среде используйте tools/checkpoint.py и docs/CHECKPOINTS.md.

## Последние принятые области

- Source suite1493PASS/1skip/10subtests (Python3.14.3,35.41s), отдельные
  native/API/HTTP26PASS. Исправлена missing diagnostics projection без product_version.
- Frozen Linux x64 bundle05 с896signedpayloadfiles: GUI ru/en/de,
  install/backup/uninstall-preserve/restore/stop, real SQLite+HTTP,
  offline errors/stats/preview+confirmed export PASS.
- Reference real v1/v2/v3, transactional SQLite migrations и failure profiles,
  signed executable ProductAdapter protocol/scaffold и29build-lock/receipt tests.
- Изолированный /tmp frozen GUI v1→v2→v3 PASS с actual version/schema/user file.
- Exact17buildpins/22wheelhashes, source CI workflow для Linux/Windows;
  сам workflow ещё не является фактом remote CI PASS.
- License04/05 gates BLOCKED. 05:896payloadfiles,902selectedartifacts,
  73componentcandidates,29removed; cffi/crypto bytes сверены с official wheels.

## Следующие действия

1. Сохранить и проверить CP-004 в GitHub; local Docker Git остаётся отдельным BLOCKED.
2. Закончить настоящий frozen bad-health automatic rollback и controller SIGKILL
   →fresh browser→explicit resume; записать точные receipts. Source tests этого
   не заменяют. При recovery продолжать существующий operation_id, не новую install.
3. Закончить source DockerCLI/owned-container controller и проверить его отдельно.
   Полный provisioning/update/restore и live daemon acceptance всё ещё открыты.
4. Повторить установленный SDK wheel вне checkout после финальных изменений.
5. Проверить результат source CI на текущем SHA, если запуск доступен.
6. Продолжить Windows native/service, Linux systemd, prerequisite auto-install,
   reboot, clean-host/cross-platform restore и реальную интеграцию JobPilot/Rent.

В workspace large-bundle сценариях дважды замечены лишние скрытые временные
файлы с неизвестным источником, а update на backup:0 потребовал recovery.
Исходные failed fixtures сохранены. Guard не ослаблять, неизвестные файлы не
удалять ради PASS. В новом /tmp fixture последовательные обновления прошли;
это наблюдение не устанавливает причину прежнего сбоя.

## Выпускные ограничения

Production_ready=false. Windows/Docker/system services и остальные product
scenarios не приняты. Source implementations ещё требуют инженерного завершения;
не называть все открытые задачи только внешними блокировками.
Private publisher key, архивные пароли, product secrets, launch URL tokens
не помещать в repo/logs. Production ключ/сертификат и утверждённые юридические
тексты не предоставлены; transitive licenses/notices/security baseline не закрыты.
Не открывать мастер на внешнем интерфейсе. Main/release не публиковать без
соответствующего разрешения. Продолжать независимые задачи при отдельном BLOCKED.

Подтверждено перед CP-004: frozen bad-health update → FAILED_ROLLED_BACK;
фактический HTTP сайт v3.0.0/SQLite3 ready=true и user file сохранены.
