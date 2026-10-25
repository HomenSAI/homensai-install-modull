# Общая блокировка исполнения продукта и зависимостей

Автор: Serhii Khomenko. Инженерный шаг CP-20261025-017.

`ExecutionDomain` объединяет зарегистрированные product SDK и prerequisite
controllers одного исходного владельца. Захват происходит до публикации новой
операции; владение передаётся её worker и удерживается до записи результата,
финальных callbacks и освобождения ресурсов. Синхронный вызов не держит общий
Python mutex во время длительного исполнения: status и cancel остаются доступны.

Это гарантия для участников одного доверенно настроенного domain. Отдельные
произвольные bootstrap roots, старые бинарники и прямые вызовы engine вне
этого протокола в неё не входят. Windows ACL/UAC, настоящий reboot/power loss,
службы и реальные системные установщики требуют отдельной приёмки.

## Настройка доверенным bootstrap

```python
from homensai_install.api.v1 import InstallerAPI
from homensai_install.execution_lease import ExecutionDomain
from homensai_install.prerequisite_execution import PrerequisiteCoordinator

# Этот постоянный private path выбирает bootstrap исходного владельца.
# Он не зависит от каталога, release, режима native/docker или product API state.
domain = ExecutionDomain(owner_state / "execution-domain", owner_id=owner_id)
coordinator = PrerequisiteCoordinator(
    catalog, state_dir=prerequisite_state, owner_id=owner_id,
    downloads=downloads, adapters=adapters, discover=discover,
    product_consent_refs=product_consent_refs, execution_domain=domain,
)
first_api = InstallerAPI(first_api_state, engine=engine, prerequisites=coordinator)
second_api = InstallerAPI(second_api_state, engine=engine, execution_domain=domain)
```

В примере остальные объекты — уже проверенные trusted ports хоста, а не
данные браузера. `from_bootstrap(..., execution_domain=domain)` поддерживает
тот же порт. При `prerequisites=coordinator` SDK наследует его domain;
противоречащий дополнительный domain отклоняется. Путь, owner и объект domain
не принимаются через manifest, options, CLI argv или HTTP body.

По умолчанию coordinator использует `state_dir / "execution-domain"`.
Сам `state_dir` по прежнему контракту общий и постоянный для исходного
владельца prerequisite controller. Если bootstrap использует несколько таких
stores, он обязан передать им один явный domain. Незарегистрированный SDK
без `execution_domain` сохраняет прежние product journal claims; общего
исключения с произвольным другим SDK для него не заявляется.

Конструктор `ExecutionDomain` и его read-only наблюдение не создают файлы.
Конструктор `PrerequisiteCoordinator` сохраняет прежнее создание собственного
prerequisite store; это отдельное поведение, которое не называется lazy.
Публичные status/capabilities не раскрывают путь, fencing token или ID чужой
операции. Новые поля в старые reviewed plans и consent digests не добавляются.

### Переход со старых журналов

Сохранённые операции v1 существовали до общего domain. Поэтому отсутствие его
claim не означает, что прежде незавершённых операций не было. SDK и coordinator
лениво регистрируют свои известные journal directories. Trusted bootstrap может
заранее добавить другие известные ему старые stores:

```python
domain.register_participant("product", previous_product_api_state)
domain.register_participant("prerequisite", previous_prerequisite_state)
```

Регистрация в конструкторе не пишет файлы. При reserve под общим OS lock
список сохраняется в private domain store; последующие экземпляры с другим
API state directory проверяют уже сохранённых участников. Проверки старых
журналов идут до enqueue, вне domain SQLite transaction. Лимит — 256 stores.
Исчезнувший или заменённый ранее наблюдавшийся журнал вызывает отказ;
удаление каталога не является способом снять неизвестный effect.
Автоматический перенос такого журнала в другое место или принятие нового inode
после восстановления пока не реализованы; подмена не принимается за миграцию.

Учитываются и старые terminal записи с незавершённым результатом: product
`CANCELED` с `resume_required`, prerequisite `CANCELED` с ожиданием reboot/relogin.
Явное восстановление исключает из проверки только свою собственную operation
в соответствующем journal; чужие незавершённые операции остаются блокирующими.

Bootstrap должен зарегистрировать известные ему stores до разрешения мутаций.
Конструктор другого объекта сам по себе ещё не сохраняет его регистрацию.
Неизвестные незарегистрированные каталоги не ищутся по диску. Старые живые
бинарники, способные начать работу без этого протокола, требуют отдельного
порядка обновления и не получают гарантию общей блокировки автоматически.

## Два вида владения

Постоянный `execution.lock` содержит OS lock живого контроллера. Handle
не передаётся через exec и закрывается в forked child без снятия родительского
lock. Публичный status не открывает окно для нового владельца. Дочерний
установщик может пережить контроллер: одного исчезновения OS lock недостаточно
для разрешения другой операции.

`execution.sqlite3` хранит durable claim с fencing token и фазой. Записи
проверяют исходного владельца, private regular files и отсутствие ссылок.
Ресурсные locks существующих engines сохраняются.

| Фаза | Смысл и восстановление |
|---|---|
| `RESERVED` | Domain занят до публикации operation. Точный явный повтор восстанавливает первоначальный operation ID; effects ещё запрещены. |
| `QUEUED` | Operation сохранена, исполнение ещё не подтверждено. Потерянный ACK читается по тому же ID; start не превращается в resume. |
| `ACTIVE` | Контроллер вошёл в исполнение. После его исчезновения неизвестный результат сохраняет claim; нужен явный resume/reconciliation. |

Domain, product journal и prerequisite journal — разные хранилища, а не
одна SQLite transaction. Протокол сохраняет безопасный отказ на промежуточных
границах. Сбой обязательной записи не разрешает переход к следующему effect.
При явном восстановлении новая попытка начинается с `RESERVED`, но сохраняет
`uncertain=true`, если прежняя достигла `ACTIVE`. Одна текущая фаза `RESERVED`
не доказывает отсутствие исторического внешнего эффекта.
Освобождение сверяет текущий token: устаревший worker не может удалить claim
новой попытки. PID, timeout и отсутствие окна установщика не очищают claim.

Product submit повторно проверяет fingerprint reviewed inventory **после**
захвата domain и до enqueue. Изменение окружения между первоначальной
проверкой и захватом даёт `PLAN_STALE` до исполнения.

## Завершение, отмена и восстановление

Для trusted product engine `SUCCEEDED` и `FAILED_ROLLED_BACK` без
`resume_required` являются подтверждённым завершением. Engine не должен
возвращать их, оставляя неизвестный конфликтующий внешний effect.
`FAILED`/`CANCELED` сами по себе недостаточны: для освобождения нужен явный
trusted `execution_quiescent=true` и отсутствие `resume_required`.
Произвольное исключение сохраняет domain для восстановления.

Cancel записывает cooperative request и не ждёт длительного domain lock.
Отмена неизвестного эффекта не разрешает новый product/prerequisite start.
Если terminal journal уже подтверждает безопасное завершение, но controller
умер до release, явный resume завершает освобождение без повторного engine.
Конструктор, status и duplicate start этого не делают автоматически.

`domain.busy()` наблюдает занятость всего domain. Публичный
`prerequisites_busy()` отвечает именно за prerequisites. Собственный product
claim не блокирует read-only duplicate ACK или явный product resume в мастере;
взаимоисключение самого resume проверяет SDK при захвате общей lease.
Оба busy probe дают предварительное наблюдение, а не разрешение на исполнение.
До первого reserve новые участники зарегистрированы только в памяти; чтение
общего domain может ещё не видеть их старые journals. Обязательная проверка
известных journals выполняется под lock при reserve и может отказать после
предшествующего `busy() == False`.

Для prerequisites `READY`/`CANCELED` освобождают domain только при отсутствии
`INSTALLING`, `UNKNOWN`, `WAITING_REBOOT` и `WAITING_RELOGIN` в шагах. Отмена
на reboot/relogin boundary сохраняет claim. Явный recheck/resume проверяет
фактическую границу read-only, сохраняет запрос отмены и не запускает reboot
самостоятельно. Ручная подготовка остаётся незавершённой до recheck или cancel.

Повтор синхронного CLI start теперь читает актуальную проекцию без захвата
собственного controller lock. Для прерванной операции он может вернуть
`RECOVERY_REQUIRED` и exit code `1`, как status. Прежний искусственный
`unknown`/exit code `3` больше не создаётся самим чтением. Operation ID,
согласия и отсутствие повторного исполнения сохраняются.

## Доказательства и открытая приёмка

Точные исходные hashes, команды, результаты, сохранённые первоначальные FAIL
и ограничения находятся в checkpoint receipt и `docs/verification-status.json`.
Проверяются отдельные процессы, обе очередности product/prerequisite, окна
enqueue/final observer, аварийные выходы по фазам, отмена, fencing, ошибки
хранилища и повторная проверка inventory. Source/installed synthetic проверки
не заменяют реальный install/update/restore/rollback на целевых платформах.
