# План SOL 6.1 High: 12 часов, три агента

Дата подготовки: 26 октября 2026, Europe/Berlin. CP-20261026-001 / HIM-009.
Это план следующего запуска; подготовка документа не запускает 12-часовую сессию.

## Исходное состояние и цель

Проверенный GitHub baseline: `c6aff96b9f4359097ba8cb17d44fe429d8e2dfec`,
ветка `codex/universal-installer-20261025`. CP023 — документация, код — CP021.
SDK 0.1.0/API 1.0, Python, локальный web-мастер ru/en/de, Windows/Linux native/Docker.
Источник требований — полный PROMPT; единственный реестр 106 задач — канонический
TODO. Этот план задаёт порядок работы, не создаёт второй список продуктовых задач.
По актуальному handoff: 8 DONE, 64 IN_PROGRESS, 3 BLOCKED, 31 TODO; ранняя панель
TODO от CP018 устарела. Ни один G0–G5 пока не закрыт, production_ready=false.

CP021 подтвердил 2363 source/integration PASS, 3 skip, 15 subtests; installed
synthetic SDK/prerequisites и 24 Chromium-сценария; Windows portable проверки.
Это исторические scoped results, а не новая полная приёмка. Adapter maintenance
подключён только для Linux/native/user start/stop/backup. Migration, независимый
installed reference, Windows native/services и Docker lifecycle требуют работы.

Цель — выполнить все доступные критерии финального продукта и приложить доказательства.
12 часов — рабочее окно, а не обещание закрыть весь остаток. Для финала нужны реальные
Windows 11/Ubuntu 24.04 x64, четыре способа установки, три языка, JobPilot/Rent,
production signing и юридические решения. Если исходники, runner или ключи отсутствуют,
записать конкретный BLOCKED, продолжить независимые работы, не сокращать scope.

## Допуск внешних модулей

Начинать с `AI_START_HERE.md`. Владелец разрешил использовать модули с GitHub
только при проверенных правах распространения и выполненных условиях точной
лицензии; применять `docs/THIRD_PARTY_POLICY_RU.md` и существующий license gate.

## Старт: первые 30 минут

1. Прочитать AGENTS.md, CLAUDE.md, PROMPT, TODO, RESUME, work-state,
   IMPLEMENTATION_STATUS, WORK_LOG, INSTALLER_API, VERIFICATION_BACKLOG,
   verification-status, TARGET_MATRIX, BUILDING и CHECKPOINTS.
2. Сверить branch/HEAD/dirty/index/remotes, `git ls-remote`, активные свои процессы.
   Новые чужие изменения сохранить, не reset/stash без разбора. Старые shutdown
   команды CP023 — история, не активное задание.
3. Записать начало/deadline UTC и рабочую среду в handoff. Проверить доступность
   gpt-6.1-sol/high, двух помощников, Docker/Node/Python, Windows11/Ubuntu VM,
   product sources, signing inputs. Не считывать секреты в общий журнал.
4. Проверить lock, source/integration baseline в изолированном snapshot, воспроизводимо
   сохранить вывод и exit code. Проверить status CI отдельно, не считать очередь PASS.
5. Согласовать договор migration/receipt с помощниками до параллельных изменений.
   Координатор создаёт два отдельных worktree от одного SHA; не использовать старые
   worktree с неизвестным содержимым. Записать ownership и интеграционный SHA.

## Ровно три агента

| Агент | Владелец работы | Граница изменений |
|---|---|---|
| Основной: интегратор + independent acceptance | Контракты, G0/G1, installed reference, общий API, review, Git/CI, release evidence | reference/, packaging/ installed harness, docs/, API exports и канонический TODO; общие файлы только после согласования |
| core_lifecycle | HIM-015/023/036/050/052/054–058/062/063/072: migration, receipts, backup/restore/update/rollback | homensai_install/adapters.py, engine.py и связанные lifecycle tests; не переписывать docs/API экспорты параллельно |
| platform_acceptance | HIM-028–045/046/047/048/105: Windows/Linux/Docker/services, GUI проверка, faults | platform backends, windows/, linux/, docker/, выделенные integration tests; UI/prerequisite файлы по явному согласованию |

Дочерние агенты используют `gpt-6.1-sol` / `high`, без дальнейшей делегации.
Каждому выдавать один проверяемый пакет на 30–90 минут: HIM IDs, входной SHA,
файлы, контракт, критерии, точные разрешённые эффекты, итоговый commit/receipt.
Общий файл имеет одного писателя; при пересечении остановить только конфликтующий
пакет. Только основной агент интегрирует и отправляет общую ветку. Результат агента
содержит diff/commit, тесты, ограничения и следующий пакет. После интеграции root
проверяет объединённое дерево; нельзя складывать пересекающиеся test counts.

## Основной результат и резервная очередь

Основной результат окна — Linux/native/user migration/backup/fresh-empty и
независимый installed reference. Полное закрытие Windows/Docker/services/G4/G5
за это окно не прогнозируется. Таблица ниже — маршрут к финалу с резервными
пакетами; широкие платформенные работы брать только после доказанного core slice.
Агент platform_acceptance сначала делает installed maintenance/reference harness
в packaging/ и reference/; основной агент передаёт ему эти файлы в исключительное
владение и занимается контрактом, независимым review и интеграцией. После готовности
core помощник проверяет новый adapter install/update, а не legacy fixture.
При отсутствии времени платформенная очередь остаётся следующему окну.

Если к шестому часу migration path не проходит focused checks, закончить безопасный
backup binding + installed maintenance, сохранив install/update capabilities=false.
С девятого часа приоритет — интеграция и итоговые проверки, новые большие направления
не начинать. Это контроль риска, а не основание отметить остальные HIM выполненными.

В adapters.py проверка формата backup_receipt_id ещё не доказывает наличие backup.
Соединить её с реальным encrypted archive из NativeEngine._backup и проверкой
installation/release/digest. Существующие integration fixtures сначала устанавливают
продукт через legacy engine; для нового пути нужен настоящий adapter fresh test.

## Расписание от фактического начала

| Время | Основной агент | core_lifecycle | platform_acceptance | Условие перехода |
|---|---|---|---|---|
| 00:00–00:30 | Baseline, contract и две worktree | Аудит migration/receipt | Инвентаризация VM и runnable checks | SHA, baseline и blockers записаны |
| 00:30–02:00 | Contract review, baseline и checkpoint | migration_plan/apply + backup binding + fresh-empty | Независимый installed maintenance/reference harness | Первая интегрируемая порция с regression tests |
| 02:00–04:00 | Интеграция и negative contract review | Adapter install/update v1→v2→v3, UNKNOWN reconciliation | Настоящие start/stop/backup через установленный SDK, без checkout | Receipt связан с source/artifact и installation/release |
| 04:00–06:00 | Review faults, API sync и checkpoint | Crash/cancel/lost ACK, fresh-empty refusal, data integrity | Новый adapter fresh/update lifecycle после появления core commit | Отсутствуют silent fallback/false READY/повтор mutation |
| 06:00–08:00 | Оценить готовый slice; G1 и оставшиеся blockers | Исправления review, restore/rollback только после устойчивого update | Fault tests и actual browser ru/en/de на текущем wheel | При срыве core — закончить безопасный меньший slice |
| 08:00–09:00 | Свести ветки, выбрать итоговый кандидат | Закончить начатые fixes, не расширять scope | Финализировать installed evidence, доступные platform smoke | Все изменения включены в проверяемый snapshot |
| 09:00–11:00 | Полный source/integration, CI, artifact receipts и независимый review | Только подтверждённые regressions кандидата | Installed SDK/prerequisites/browser; Windows subset при доступности | После изменений повторены затронутые checks |
| 11:00–12:00 | Итоговый checkpoint/push/ref/tree, перенос и handoff | Review итоговых bytes/recovery | Независимая проверка матрицы и ограничений | Отчёт о фактически выполненном, готовый resume |

Расписание адаптивное: зависимость важнее часа. Незавершённый unsafe contract
нельзя обходить ради следующего столбца. Если пакет заблокирован средой, агент
берёт доступный harness/regression/backend пакет; не тратит часы на одинаковые ретраи.
После каждого результата назначать следующую работу, не завершать сессию после
одной функции. При полной доказанной готовности раньше deadline можно завершить.

## Обязательные инженерные критерии

- Migration plan закрепляет from/to release, installation, schema/data version,
  adapter/runtime/payload digest и согласия. Apply выполняет только reviewed plan.
- Backup receipt подтверждает реально завершённый согласованный encrypted archive,
  digest, identity и возможность восстановления; произвольный файл или старый
  receipt не разрешает mutation. Fresh-empty отдельно доказывает отсутствие данных;
  неизвестное состояние никогда не трактуется как пустое.
- Crash до/после side effect, утрата ACK/ledger, cancel и repeated execution не дают
  повторной опасной миграции. Primary failure не маскируется recovery failure.
- Independent reference wheel устанавливается вне checkout, выполняет реальный
  процесс/HTTP/SQLite через публичный SDK. Тест отвергает import из source tree.
  Существующий synthetic planning harness не закрывает этот критерий.
- Lifecycle: fresh → start → stop → backup → update v1/v2/v3 → restore → rollback →
  repair/reinstall → uninstall с сохранением данных → recovery. Сравнить данные,
  права, ownership, health/readiness и durable journals, включая fault injections.
- Docker использует собственные pinned resources; чужие containers/images/volumes
  не изменяются. Development контейнер с pytest не считается Docker acceptance.
- Windows11 не заменяется Windows10/Server, Ubuntu24.04 — Debian DEV-контейнером.
  Source mocks, Node VM и browser tests имеют разные scopes. Reboot — только VM.
- Безопасность: подпись/manifest/hash substitution, path traversal/symlink, порт/PID
  ownership, секреты в support export, tampered archives, stale plans и replay.
- GUI: настоящие screens/actions/focus/keyboard/errors/progress ru/en/de во всех
  четырёх режимах. Readiness не равен успешному завершению скачивания.

## Проверки и доказательства

Команды подготовки другой машины: [START_HERE_RU.md](START_HERE_RU.md).
Строгий Python build lock рассчитан на CPython 3.14 x64 с GIL; локальный Python3.12
подходит для checkpoint tooling, но не подменяет build interpreter.

В Linux DEV-среде после `python tools/check_build_lock.py --installed`:

```sh
python -m pytest -q tests
python -m pytest -q integration_tests
python -m build --wheel --no-isolation --outdir dist .
python -m build --wheel --no-isolation --outdir reference/dist reference
python tools/build_receipt.py --artifact-dir dist --artifact-dir reference/dist --output build/receipts/source-build.json
```

Далее `packaging/check_installed_sdk.py` (--work-dir, --receipt),
`packaging/check_installed_prerequisites.py` (+ --wheelhouse) и
`packaging/check_prerequisite_upload_browser.py` (+ --wheelhouse, --node-path,
--browser, --checkpoint). Использовать новые внешние work dirs для каждой попытки,
фактические пути и текущий checkpoint. Сначала прочитать `--help`/код harness.
Новые installed ProductAdapter/lifecycle сценарии дописать — не выдавать старые
synthetic clients за их выполнение. Native Windows subset брать из workflow
`.github/workflows/source-tests.yml`, если файл переименован — найти фактический workflow.

Receipt: UTC, HIM/ACC, source SHA + dirty/hash inputs, artifact SHA256, OS/arch,
Python/tools, mode/scope/locale, точная команда, expected/observed, exit code,
PASS/FAIL/BLOCKED, raw log/JUnit и пределы результата. Отдельно хранить ранний FAIL.
Если тест отсутствует — NOT IMPLEMENTED; если не запускался — NOT RUN.
Финальный полный прогон — на стабильных bytes, без параллельных edits; изменение
кода после проверки требует повторить затронутые проверки итогового кандидата.

## Checkpoint и непрерывность

После каждого пакета и не реже 30–45 минут: обновить соответствующие HIM/ACC,
RESUME/work-state/WORK_LOG, diff review, allowlist checkpoint, GitHub и проверка ref/tree.
Не менять исторические receipts. Sync pending до фактической проверки push.
Служебный JSON самого commit хранится в Git directory, не создавать рекурсивный commit.
Две agent worktree интегрируются последовательно; конфликт разрешать с повторной
проверкой. Не использовать force push. Remote Forgejo отсутствует — отдельный
SYNC_BLOCKED; это не причина блокировать GitHub и доступную разработку.

При compaction перечитать handoff, не начинать заново. После падения/лимита сервиса
сначала сверить remote и локальные незавершённые изменения. Перенос чата не требуется:
источник восстановления — репозиторий и receipts. В state писать start/deadline,
assignment, последний verified SHA, текущий тест и конкретный next action.
Не обещать автоматические 12 часов при отключённом ПК, лимите аккаунта или потере
сети. Не создавать watchdog, бесконечные ретраи, keep-alive или обход квот.

## Финальная приёмка

G0/HIM-010: безопасная среда, актуальные inputs и проверенный checkpoint.
G1/HIM-024: установленный публичный SDK с независимым настоящим reference.
G2/HIM-049: четыре установки × три языка, fresh/start/stop/uninstall.
G3/HIM-073: полный lifecycle/recovery/API/diagnostics и данные после faults.
G4/HIM-085: актуальные JobPilot/Rent и независимая повторная интеграция.
G5/HIM-100: воспроизводимые поставки, production trust, notices/licenses,
документация, приёмка и сохранённый handoff по каноническому TODO.
Все должны иметь собственные доказательства; только тогда production_ready=true.
Иначе финальный результат — проверенный DEV-кандидат + точный остаток с владельцем
блокировки и следующим действием. Истечение 12 часов не закрывает ни одну задачу.

## Настройка модели

`.codex/config.toml` задаёт gpt-6.1-sol/high и два дочерних потока (основной агент
в лимит не входит). Подтвердить, что клиент загрузил project config, проект trusted,
модель доступна аккаунту, UI/global overrides не изменили модель. Настройки не дают
дополнительных прав и не гарантируют продолжительность сессии.
Основание: [OpenAI Docs: Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Models](https://learn.chatgpt.com/docs/models), проверено 26.10.2026.
