# Docker provisioning: границы исходного кода после CP-005

Интегрировано в CP-20261025-008; автор: Serhii Khomenko.

## Выполненный участок

В `DockerEngine` добавлены `install` и `uninstall` с сохранением данных. Они
проходят через тот же `execute/resume`, root lock, стадии и journal, что
start/stop/backup. `DockerLifecycle` предоставляет фазы; второго orchestrator
в нём нет. Публичные сигнатуры engine protocol не изменены.

`plan("install", ...)` возвращает конкретные `installation_id` и
`provisioning_spec`, full pinned references/IDs, directory/network/container
параметры и требования согласия. Дополнительные поля общего backend router в
`engine_plan` допустимы и входят в неизменяемый hash binding всего плана.

| Участок | Реализованное поведение |
|---|---|
| Fresh preflight | Подпись, full inventory, image reference/config ID/arch, legal evidence, UID:GID, private empty root, namespace/daemon identity |
| Образы | Только заранее существующие точные RepoDigest references; `--pull=never`, без build/pull/import |
| Файлы | Каталоги 0700; JSON/copy temporary 0600 с первого open; fsync файла → replace → fsync directory; parent fsync после mkdir |
| Ownership | Full IDs, private receipt, signed spec, exact labels/argv/mounts/ports/isolation, before-create IDs |
| Immutable release | Canonical полный payload map закреплён в reviewed plan/journal; новые receipts schema 2 содержат `payload_sha256` |
| Partial install | Durable INTENT до RPC; новый ID фиксируется после inspect; другой plan не обходит незавершённый journal |
| Resume | Не повторяет неопределённый RPC вслепую; проверяет identity и прежние checkpoints; повторяет health |
| Recovery bytes | Весь signed payload, inventory/signature и metadata сохраняются до первого create RPC |
| Потеря source cache | Только исчезнувший source можно заменить проверенной owned retained копией без изменения plan options/binding |
| Uninstall | Graceful reverse stop; только собственные container IDs; network последней; без force, volumes и shared image removal |
| Недоступные data/images | Может сохранить пути и удалить доказанно собственные resources; runtime integrity отмечается `OWNERSHIP_ONLY` |
| Завершение удаления | Отсутствие IDs проверяется через inventory; tombstone связан с private receipt/journal; данные и recovery files остаются |

### Условия выполнения

Профиль ограничен Linux rootful daemon, работающим в тех же mount/net namespaces
и доступным через проверенный local Unix socket. Container user должен быть
не-root и точно совпадать с UID:GID installer. Privileged user/group/chown
fallback отсутствует. `--mount` paths с CSV-разделителями и неподдерживаемые
argv отклоняются до создания ресурсов.

Для install/start/backup нужен Engine 28.0.0 или новее. Причина — опубликованное
ограничение localhost port publishing в более ранних Engine: соседний L2 host
может получить доступ к такому порту. Stop/uninstall не требуют этой версии,
чтобы не блокировать безопасную остановку/удаление существующей установки.
Источник: [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/).

Rootful Docker и доступ к socket не предоставляются автоматически. `capabilities`
объявляет development implementation при наличии доверенных зависимостей;
конкретный plan отдельно проверяет platform/payload/user constraints.

## Проверенные source-сценарии

Тесты используют настоящие Ed25519, SHA-256, AES и файловые операции. Daemon —
явная ограниченная stateful модель, сетевые обращения unit suite запрещены.
Synthetic ELF в CLI suite проверяет реальный subprocess boundary, argv,
environment, timeout, output bounds и executable pin; это не Docker binary.

Основные сценарии:

- install → ordered start → private health → signed recovery copy;
- uninstall → reverse graceful stop → только owned removal → неизменные данные;
- crash после network/container create, start и remove;
- crash на prepare/retain/provision/receipt/start/health checkpoints;
- неизвестный RPC до эффекта: повтор не отправляется;
- cancel после provisioning и resume прежней установки;
- дубликат network name, чужой network consumer, заменённый DataHome inode;
- отдельные kind-qualified intents для компонента с именем `network`;
- чужие `container:<name|ID prefix|full ID>` references блокируются до остановки
  namespace donor; неизвестная ссылка не разрешается эвристически;
- unsafe/missing image, дополнительные image volumes/OnBuild, drift нового
  container до записи ownership;
- полная потеря исходного cache после первого create, восстановление из
  повторно проверенных signed bytes с прежним binding;
- повреждение исходных/retained bytes и подписи не маскируется;
- действительная повторная подпись изменённых notices после review/checkpoint
  или успешного install не подменяет pinned полный payload;
- незавершённая temporary copy, permissive umask, первый непубликованный journal;
- race между fresh preflight и root lock;
- public API consent/review/submit и полный install→uninstall через тот же engine;
- полный filesystem при записи подтверждения create оставляет прежний INTENT
  для inspect/reconcile без второго create;
- отказ CLI grammar от force, volumes, host network, root user, дополнительных
  capabilities, secret env, дублирующих labels, mutable image и CSV expansion.

Воспроизводимый targeted запуск из checkout с установленными test dependencies:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -B -m pytest -q -p no:cacheprovider \
  tests/test_docker_engine.py tests/test_docker_cli.py tests/test_docker_provisioning.py
```

Точный результат финального запуска фиксируется в общем checkpoint/WORK_LOG
после интеграции ветки. Число unit tests не означает пройденную live-приёмку.

## Оставшиеся ограничения и gates

**`production_ready=false`; live Docker acceptance — BLOCKED в текущей среде.**
Docker executable/daemon здесь отсутствуют; Windows/VM runner также отсутствует.
Не утверждается успешная установка какого-либо реального продукта.

| Возможность / gate | Состояние |
|---|---|
| Linux реальный daemon x64/arm64 | BLOCKED: нужен disposable runner и approved product images |
| Windows Docker Desktop / VM path/ACL mapping | BLOCKED; данный executor не выполняется на Windows |
| Docker prerequisite installation | Не реализована этим backend |
| Host attestation для обычного non-root пользователя | Требует live-проверки `/proc` доступа либо проверенного host adapter; docker group сама по себе не закрывает gate |
| Получение/import/offline transport images | Не реализованы; полные образы не входят в recovery/data archive |
| Update, restore, rollback, repair, reinstall | `CAPABILITY_UNSUPPORTED` |
| Migration/product hooks и product secrets | Не поддерживаются данным узким contract |
| Автозапуск/supervision после reboot | Не предоставляются (`restart=no`) |
| Прекращение failed install с compensation | Отдельного abort adapter нет; неясные эффекты сохраняются для reconcile/resume |
| Install поверх сохранившихся данных | Не допускается автоматически; требуется будущий reinstall/migration adapter |

Реальная matrix должна подтвердить inspect-поля и lifecycle состояний конкретных
поддерживаемых CLI/Engine versions, non-root bind permissions, network/firewall
изолированность, health proof, reboot, process/power-loss boundaries, полный диск,
занятый loopback port, daemon restart, SELinux constraints и фактические image
SBOM/license evidence. Synthetic image metadata не закрывают эти gates.

Полный контракт и ограничения: [DOCKER_ENGINE.md](DOCKER_ENGINE.md).
