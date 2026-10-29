# Продолжение после CP-20261026-012

## Очередь Terra 200 задач остановлена владельцем

2026-10-28T16:25:11Z. Завершено 77/200; пункты 084 и 085 выполнены после
последнего checkpoint 75/200. Точный список и незавершённые условия — в
docs/work-state.json и плане. Продолжение на другом компьютере:
docs/TERRA61_200_TRANSFER_RU.md; находки docs/TERRA61_200_FINDINGS_RU.md,
подробности docs/TERRA61_200_PROGRESS_RU.md. Следующий плановый checkpoint
80/200. CI run 40724824553 отменён после успешного синтетического fixture
шага; полный PASS на этот run не распространяется. G0–G5 OPEN.

## Завершённое окно Terra 28.10.2026

Владелец явно запустил двухчасовой ограниченный пакет Terra: начало
14:22:27 UTC, дедлайн 16:22:27 UTC, прекращение добавления сценариев 15:52:27 UTC.
Пакет завершён досрочно в 14:49:40 UTC: два разрешённых installed сценария и
83 targeted source checks выполнены. Собственных тестовых контейнеров не осталось.
Один агент; максимум два installed fault сценария, runtime не менять. Старое
закрытое окно ниже — история. Прогресс: `docs/TERRA61_2H_PROGRESS_RU.md`;
текущее состояние окна: `terra_execution_window` в `work-state.json`.
### История предыдущих checkpoint (статусы относятся к моменту записи)

CP-20261028T143449Z-8878f171e2eb: installed lost-ACK профиль WIP, нужен
изолированный запуск на SDK 6edcbb83; AST PASS, fault сценарий пока NOT RUN.
Дополнение CP-20261028T143633Z-a8c51a2df8c6: первый isolated installed lost-ACK
PASS. Подробности и следующий резервный сценарий — TERRA61_2H_PROGRESS_RU.md.
CP-20261028T143832Z-262d480572a6: tampered-backup профиль WIP, ещё NOT RUN.
Дополнение CP-20261028T143954Z-972d8c3a1cf6: ожидаемый отказ tampered-backup
PASS в isolated DEV. Осталось повторить затронутые проверки финального harness.
Следующий независимый пакет после завершения этого окна: installed update
missing ledger после реального эффекта и unknown reconciliation, затем foreign
receipt/live drift и candidate health/cleanup. Использовать закреплённый SDK,
original public operation и отдельные receipts; не переносить текущие PASS
на другие сценарии. Далее отдельный before-restore/before-rollback protocol.
Проверить состояние source regression по актуальному `work-state.json`/progress
и сохранить фактический результат перед завершением окна.
GitHub CI run 40708599284 (75ed388): Windows portable PASS, Linux source
3 FAIL в prerequisite runner/engine, интеграция skipped. Точное свойство
исполняемого Python, вызвавшее отказ, не установлено; receipt в backlog.
Приоритет следующего окна — сначала диагностировать этот CI FAIL и получить
проверяемое свидетельство о файловом свойстве на runner, не отключая защиту.

Автор продукта: Serhii Khomenko. Работа остановлена по указанию владельца.
Компьютер не выключать; чужие процессы/порты8810/8811/8812 не менять.
Новая работа — только по новой явной команде владельца. Открытие папки её не запускает.

## Сохранённый результат

Работа завершена по указанию владельца в окне до17:00Berlin/16:00UTC. Single-step adapter update Linux/native/user реализован и принят в DEV scope. Полный frozen source/integration: 2625PASS/3skip/15subtests, 1181.13s,208inputs unchanged. Installed public factory→fresh1→update2→update3 на immutable SDK6edcbb83 PASS:70SDK/7reference/161dependency files unchanged; effects1/2/3, source/target runtime+health identity, данные/settings/syntheticcredential сохранны. Все4update encrypted archives действительно decrypt/restored в oracle. Root2datafaultPASS/25contractPASS; core180PASS и отдельно223PASS/1root-exportdeselect (пересекаются с full, не складывать). CI SDKCP011:Linux/Windows source jobs success. Forward journal не делает automatic legacy restore; UNKNOWN/missingledger не разрешает повтор. Public restore/rollback/repair/reinstall/uninstall adapter capabilities остаютсяfalse. BorrowedDEVruntime, это не clean-host/standalone acceptance. Production_ready=false, G0–G5 OPEN. Веб-страница текущей работы опубликована privately; финальная публикация её результатов выполняется после этого checkpoint и хранится внешнейreceipt.

Основная ветка: `codex/universal-installer-20261025`, origin HomenSAI/homensai-install-modull.
Последний подтверждённый до финального checkpoint SHA: `6edcbb83a018e72d235cc3eb192864e238a82755` (CP011), tree
`d4dbbe44e48b5a9c26510f2aa0e1ad17e4080a4a`. SHA CP012 определить через Git history,
remote ref и внешнюю checkpoint receipt. Не делать рекурсивный commit о собственном SHA.

Принятый SDK: source `6edcbb83a018e72d235cc3eb192864e238a82755`, wheel SHA256
`44bba27101bf1397ad3ebd5379f1e4adcf921b035cd2559979dc14daa8a90f0d`. Первый отдельный wheel имел SHA
`4ac177efafae60cf8d235c4a2e039e6ddca8997661ea45529ca7b229ff606bd6`;
70 package files обоих wheel совпали с freeze. Побитовая воспроизводимость ZIP не заявлена.
Финальный Git package tree совпадает с этими70 SDK source files; post-suite harness
delta добавляет только сохранение receipt bodies/hash полей отчёта и отдельно проверен installed run.

## Первое следующее действие

На immutable SDKCP011 добавить independent installed update fault profile: lostACK/UNKNOWN/missingledger, tampered/foreign backup, live data drift и candidate health/cleanup failure через original public SDK operation. Затем реализовать отдельный verified before-restore/before-rollback backup protocol и adapter restore/rollback; capabilities не включать заранее. Windows11/Ubuntu24cleanhost, productDocker/services, GUI12locale-target сочетаний, JobPilot/Rent и production trust/licences остаются отдельными обязательными gates. Новое окно — только по явной команде владельца.

Сначала прочитать AI_START_HERE/AGENTS/CLAUDE, полный PROMPT/каноническийTODO,
RESUME/work-state и verification backlog/status. Старое окно08:25–20:25UTC
архивировано, новый run02 был14:35:31–16:00UTC (Berlin15:35–17:00CET).
Не продлевать закрытое окно молча. При новом разрешённом запуске архивировать его
start/deadline, записать реальные новые времена и использовать сохранённый план.

## Контракт, который нельзя ослаблять

- Update review schema2 закрепляет source/target adapters и source identity отдельно;
  public binding сохраняет7полей, apply data ровно3поля. Fresh review остаётсяschema1.
- Archive и backup:0 выдают настоящие encrypted BackupReceiptStore refs; перед effect
  повторяются decrypt/restore/signed recovery/identity и сравнение всех live roots.
  Исключены только bytes точного own update journal, safety файла тоже проверяется.
- Scoped quiesce связывает native token, phase key и release. Source completion
  не снимает UNKNOWN другой фазы; cleanup UNKNOWN reconciles до start.
- UpdatePlan policy закреплена digest, rollback journal при preserve policy отвергается.
  Health failure сохраняет новые данные/forward journal, primary/cleanup codes раздельны.
- SIGKILL — source process proof; installed happy lifecycle — отдельный scope.
  Настоящие installed faults, power-loss, clean-host и restore/rollback не приняты.

## Материалы восстановления

Главные receipts в packaging/acceptance-evidence:
source-20261026-cp012.json/.log/.xml;
installed-update-forward-20261026-cp011.json и неизменённый first-pass;
core-update-execution-final-20261026-run02.json;
update-independent-root-20261026-run02.json; github-ci-20261026-cp011.json.
Исходные FAIL/RED/PENDING сохранены отдельно, новые результаты их не стирают.

Агентские refs сохранены координатором и независимо проверены:
core `codex/core-lifecycle-20261026-run02` → f4fec48e59cbc5e54111312c12f44b5a8bc10f3e;
acceptance `codex/platform-acceptance-20261026-run02` → d87ee11c782a9936d79f3d70026e236c4883b7b0.
Это инженерные ветки; продолжать основную feature-ветку, не применять уже интегрированное повторно.
Worktree внутри .local-state/agents/core02 и acceptance02; оба чистые/остановлены.
Перед уборкой проверить unique commits и dirty/untracked, не выполнять автоматический reset/clean.
Отдельный Docker Git remote отсутствует, SYNC_BLOCKED. GitHub feature refs подтверждены.

Веб-страница: https://homensai-installer-work-20261026.hoseskhom.chatgpt.site
Sites project appgprj_6adf6a1e6b4c819195800ccc83cd3731, owner-private.
Подробности в WORK_STATUS_SITE_RU.md; финальная deploy receipt вне tracked tree.
Секреты/ключи/venv/build/dist/личные БД в Git не включены; shutdown не выполнять.
