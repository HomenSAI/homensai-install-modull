# ИИ: начни здесь

Владелец: Serhii Khomenko. Общение и рабочие документы — на русском.
Этот файл описывает вход в работу; актуальное состояние — `docs/work-state.json`
и `docs/RESUME.md`, продуктовые требования — канонические PROMPT и TODO.
Не создавать новый план вместо выполнения уже сохранённого.

## Как понять запрос

- «Начинай», «продолжай», «запускай работу» в контексте разработки этого проекта:
  выполнить этот порядок и перейти к реализации. Не просить повторно определить
  первую задачу. Новый полный запуск использует сохранённый 12-часовой план.
- Возобновление незавершённого окна: сохранить его start/deadline; после истёкшего
  deadline подготовить итоговый handoff, не продлевать окно молча.
- Вопрос, аудит или изменение инструкций: выполнить конкретный запрос. Не запускать
  12-часовую реализацию, системные установки или внешние процессы автоматически.

## Первые действия до изменения кода

1. Прочитать AGENTS.md, CLAUDE.md, RESUME и work-state. Проверить текущий HEAD,
   ветку, dirty/staged файлы и origin. Сверить `git ls-remote` с историей, не стирать
   чужую работу. Исторический shutdown CP023 никогда не повторять.
2. Прочитать полный `HomenSAI_Universal_Installer_PROMPT_RU.txt`, канонический
   `HomenSAI_Universal_Installer_TODO_RU.txt`, `docs/IMPLEMENTATION_STATUS.md`,
   `docs/VERIFICATION_BACKLOG.md`, `docs/verification-status.json`, `INSTALLER_API.md`.
   Затем `docs/SOL61_12H_PLAN_RU.md`, `docs/BUILDING.md`, `docs/CHECKPOINTS.md` и
   `docs/THIRD_PARTY_POLICY_RU.md`. Старые отчёты не переопределяют новые receipts.
3. При полном запуске использовать `SOL61_12H_START_PROMPT_RU.txt` как рабочее
   задание, прочитав его из репозитория: владельцу не нужно вставлять текст в чат.
   Записать UTC start/deadline и назначения в state. Модель всех трёх агентов —
   gpt-6.1-sol/high; основной + ровно два помощника, без вложенной делегации.
4. Поднять/проверить DEV-среду по `docs/START_HERE_RU.md`. На машине владельца
   основные проверки делать в Docker. Зафиксировать baseline и actual capabilities;
   при недоступной VM/лицензии/remote записать конкретный BLOCKED и продолжить
   независимые задачи. Не выдавать source test за clean-host acceptance.

## Первая инженерная задача

Пока текущий `next_step` не выполнен: **migration_plan/apply + настоящий backup
receipt + fresh-empty, затем независимый installed reference lifecycle**.
Если новый checkpoint уже завершил её, брать следующий незавершённый HIM по
зависимостям из TODO/state, а не повторять эту историческую отправную точку.

- Основной агент: зафиксировать контракт, проверить baseline и ownership, выполнить
  независимый review, интеграцию, обновление API/docs/status и checkpoint/push.
- `core_lifecycle`: `homensai_install/adapters.py`, `engine.py` и согласованные
  lifecycle tests; связать migration с существующим проверенным encrypted backup,
  identity installation/release/schema/digest; отдельно доказать fresh-empty.
- `platform_acceptance`: сначала `reference/`, installed harness в `packaging/`
  и выделенные integration tests. Принять настоящий установленный wheel вне
  checkout; после core commit проверить новый adapter fresh/update v1→v2→v3.

Координатор передаёт reference/harness второму помощнику в исключительное владение;
у каждого помощника отдельный worktree. Не запускать параллельные правки одного файла.
Docker/Windows/services — следующая очередь после устойчивого core slice.

Первый результат: regression отвергает чужой/повреждённый/отсутствующий backup
receipt до mutation; валидный reviewed plan применим один раз; lost ACK/UNKNOWN
не вызывает повторную миграцию. Существующий start/stop/backup и legacy installer
не считать доказательством нового adapter install/update.

## Правила каждого следующего шага

Реализовать → выполнить проверки → сохранить исходный FAIL и итоговый receipt →
обновить HIM/ACC, RESUME/state/WORK_LOG → checkpoint с allowlist → GitHub feature
branch → независимая проверка ref/tree → следующая доступная задача.
Не ограничиваться отчётом после одного пакета. С девятого часа интегрировать и
проверять итоговый кандидат; последний час — handoff. Остановку/смену scope владельцем
выполнять сразу. Недоступность модели не разрешает незаметно выбрать другую.

Новые модули можно брать с GitHub по THIRD_PARTY_POLICY_RU. При неизвестной лицензии
не включать компонент в проект/поставку: выбрать разрешённую альтернативу либо
реализовать самостоятельно, сохранив blocker конкретного кандидата.

## Завершение и восстановление

В state оставить точную следующую команду/задачу, незавершённые изменения,
последний verified checkpoint, start/deadline и конкретные blockers. Не хранить
секреты и не записывать собственный будущий SHA в commit. При следующем запуске
восстанавливать контекст из Git, а не требовать историю чата.
Production-ready допускается только по всем G0–G5; число часов и успешный push
не являются доказательством готовности.
