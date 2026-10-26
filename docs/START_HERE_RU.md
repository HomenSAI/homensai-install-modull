# Запуск на другом компьютере

В GitHub сохраняются исходники, lock-файлы, план и проверочные сценарии.
Готового production-установщика пока нет. DEV images, build/dist, credentials,
приватные ключи и личные базы не переносятся через Git. Среда воспроизводится
по Dockerfile; первый запуск требует интернет. Поддерживаемая DEV-машина — x64.

## 1. Получить проект

Нужны Git и Docker Engine либо Docker Desktop в режиме Linux containers.
Для работы агента отдельно нужен Codex и вход в аккаунт с доступом к SOL 6.1.
На Windows открыть PowerShell:

```powershell
git -c core.autocrlf=false clone --branch codex/universal-installer-20261025 https://github.com/HomenSAI/homensai-install-modull.git
Set-Location homensai-install-modull
git status --short
git rev-parse HEAD
git ls-remote origin refs/heads/codex/universal-installer-20261025
docker version
```

HEAD должен совпасть с remote ref, status — быть пустым. На Ubuntu использовать
тот же clone и `cd homensai-install-modull`. Не менять глобальную Git-конфигурацию.
Если репозиторий уже склонирован, сначала сохранить/разобрать локальные изменения,
проверить ветку и выполнить `git pull --ff-only`; не применять reset --hard.

## 2. Подготовить DEV-среду и проверить SDK

Из корня checkout:

```powershell
docker build -f packaging/Dockerfile.verification -t hinstall-dev:verification .
docker build -f packaging/Dockerfile.verification-browser -t hinstall-dev:verification-browser .
$repoPath = (Get-Location).Path
docker run --rm --network none --mount "type=bind,source=$repoPath,target=/workspace,readonly" hinstall-dev:verification python tools/check_build_lock.py --installed
docker run --rm --network none --mount "type=bind,source=$repoPath,target=/workspace,readonly" hinstall-dev:verification python -m homensai_install api describe --json
```

Каждую следующую команду выполнять после exit code 0 предыдущей (`$LASTEXITCODE`).
Никакой Python на Windows для этого пути не нужен. Dockerfile закрепляет базовый
Python3.14.3 image digest и Python wheels; системные apt/npm inputs не полностью
закреплены, поэтому побитовая идентичность image не заявляется.

Эквивалент smoke на Ubuntu после тех же двух `docker build`:

```sh
docker run --rm --network none --mount "type=bind,source=$PWD,target=/workspace,readonly" hinstall-dev:verification python tools/check_build_lock.py --installed
docker run --rm --network none --mount "type=bind,source=$PWD,target=/workspace,readonly" hinstall-dev:verification python -m homensai_install api describe --json
```

Source-проверки (PowerShell, переменная repoPath задана выше):

```powershell
docker run --rm --network none --mount "type=bind,source=$repoPath,target=/workspace,readonly" hinstall-dev:verification python -m pytest -q -p no:cacheprovider tests
docker run --rm --network none --mount "type=bind,source=$repoPath,target=/workspace,readonly" hinstall-dev:verification python -m pytest -q -p no:cacheprovider integration_tests
```

На Ubuntu заменить `$repoPath` на `$PWD`. Отдельные Linux namespace/OS сценарии
могут требовать другой test runner; сохранять фактический SKIP/BLOCKED, не добавлять
`--privileged` ради зелёного результата. Для builds/installed harness нужны отдельный
записываемый snapshot и новые внешние work dirs — [BUILDING.md](BUILDING.md).
Не монтировать Docker socket в обычный source-test container.

Smoke проверяет SDK API, а не запускает законченный установщик. Для reference DEV
GUI нужны проверенный runtime и bundle через packaging/build_reference.py; точный
recipe и критерии описаны в BUILDING, TARGET_MATRIX и 12-часовом плане. Не выдавать
старые локальные dist или screenshot за свежую сборку.

## 3. Запустить 12-часовую работу трёх агентов

В Codex открыть этот checkout, выбрать **GPT-6.1 Sol / High**, подтвердить доверие
своему проекту и отправить целиком [SOL61_12H_START_PROMPT_RU.txt](../SOL61_12H_START_PROMPT_RU.txt).
Настройки находятся в `.codex/config.toml`: основной агент + максимум два помощника.
Они не переносят авторизацию и не расширяют права компьютера.

CLI из PowerShell, после установки Codex и входа в свой аккаунт:

```powershell
$taskPrompt = Get-Content -Raw -Encoding utf8 SOL61_12H_START_PROMPT_RU.txt
codex -m gpt-6.1-sol -c 'model_reasoning_effort="high"' -C . $taskPrompt
```

Ubuntu shell:

```sh
codex -m gpt-6.1-sol -c 'model_reasoning_effort="high"' -C . "$(cat SOL61_12H_START_PROMPT_RU.txt)"
```

Перед уходом проверить, что стартовали оба помощника и клиент показывает нужную
модель/High. Выдать необходимые разрешения своей тестовой среде средствами клиента;
этот recipe не отключает sandbox/approval. Компьютер должен оставаться включённым,
сеть и лимиты аккаунта — доступными. Инструкция сама не гарантирует 12 часов
автономного выполнения при остановке клиента или ограничении сервиса.

## 4. Продолжить после обрыва или с другого ПК

После clone/pull отправить: «Продолжай по AGENTS.md, docs/RESUME.md,
docs/work-state.json и docs/SOL61_12H_PLAN_RU.md; проверь remote и checkpoint,
сохрани исходный deadline либо начни новое окно только по моей новой инструкции».
На исходной машине не запускать одновременно второго интегратора этой же ветки.
Локальные ещё не отправленные изменения другого ПК GitHub восстановить не сможет.

Отдельного Forgejo remote сейчас нет; GitHub origin — подтверждённое место переноса.
Адрес `127.0.0.1` другого компьютера не указывает на старый сервер.
Готовность продукта проверять по G0–G5 и evidence, не по наличию commit/push.
