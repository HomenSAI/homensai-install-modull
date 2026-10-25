# Выбор platform backend через единый API

Автор: Serhii Khomenko. Checkpoint: CP-20261025-007.

`InstallerAPI` остаётся единственным фасадом SDK/CLI/web-мастера. Новая
`homensai_install.backends.BackendRouter` выбирает engine по reviewed target,
сохраняя общий API journal, idempotency key, resource claim, events и explicit
resume. В JSON запроса нельзя передать Python class, daemon endpoint или argv.

| Target scope | Регистрация bootstrap | Поведение без регистрации |
| --- | --- | --- |
| native / user | NativeEngine по умолчанию | Возможности определяет engine и фактический host |
| docker / user | Явный DockerEngine с pinned DockerCLI | Все actions unsupported |
| native / service | Явный полноценный service lifecycle engine | Все actions unsupported |
| docker / service | Не заявлен | Все actions unsupported |

Наличие method/target в API не объявляет его production-ready. Linux user-session
engine и Docker engine не заменяют друг друга. NativeEngine отказывает при
Docker ownership receipt в том же корне; Docker уже отказывал при native receipt.

## Сохранённая операция

`engine_plan.backend_routing` включает version=1, mode/scope ID и полный target.
Он входит в canonical plan digest. Изменение target относительно marker
возвращает PLAN_STALE до engine effect. Прямые старые native/user планы продолжают
работать; прямые старые Docker планы требуют прежнего explicit engine или нового
review. Если Docker registry после перезапуска недоступен, операция остаётся
RECOVERY_REQUIRED; native не получает её execute/resume.

Discover сохраняет оригинальную форму fingerprint_data конкретного engine:
router не меняет hash, который Docker повторно сверяет под lifecycle lock.

## Bootstrap и интерфейс

Параметры и формат локальной конфигурации описаны в INSTALLER_API.md. GUI держит
отдельный проверяемый artifact binding для native и Docker. Terms и plan используют
один и тот же выбранный payload/target; digest условий другого режима не
подставляется автоматически. Копии config/options не меняются из browser request.

## Проверки CP-007

Source contracts проверяют реальный API journal с двумя независимыми fake engines:
dispatch, повтор после нового SDK instance, idempotency, отсутствие fallback,
recovery при пропавшем backend, PLAN_STALE при смене режима, bootstrap error
envelope и защиту native-корня от Docker receipt. GUI tests проверяют разные
подписанные тексты условий и запрет browser artifact/CLI config injection.

Отдельный integration test использует настоящий NativeEngine через router:
install → backup → bad-health update/rollback → stop/start → uninstall-preserve
→ restore. Данные и процессы синтетические, находятся в temporary directories;
HTTP только loopback. Это Linux source acceptance, не frozen installer и не
Windows/Docker/service/reboot приёмка. Точные числа и границы — в
packaging/acceptance-evidence/source-20261025-cp007.json.
