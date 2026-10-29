# HTTP-подключение prerequisites

Автор: Serhii Khomenko. Область: transport CP-012, requirements CP-014 и GUI contract CP-015 поверх существующего coordinator.

Это source/transport implementation. Production registry, настоящий OS installer,
reboot/relogin и browser auto/manual flow этим документом не объявляются готовыми.
Все текущие ограничения приёмки остаются в каноническом ToDo и реестре проверок.

## Доверенная конфигурация

Host заранее создаёт `SignedPrerequisiteCatalog`, фиксированные adapters,
downloader/discover, private original-owner store и `PrerequisiteCoordinator`.
Публичная фабрика принимает этот объект через `prerequisites=coordinator`.
`WizardSession` получает тот же product manifest и signed payload bindings,
а также настоящий `SignedPayloadVerifier` своего bootstrap.

При создании сессии сравниваются product/manifest/target и проверенные
`payload_binding={manifest_digest,target,release,runtime_executable,files_digest}`.
Равенство путей не требуется: одинаковые перенесённые байты допустимы.
Owner, catalog digest и payload binding сохраняются как неизменяемая привязка.
HTTP не изменяет эти параметры и не регистрирует platform adapter.

`prerequisites_identity()` читает закреплённую identity без полного повторного
hash/probe. Поэтому наблюдение и отмена уже связанной операции возможны после
потери source bundle. Каталог проверяется заново coordinator перед эффектами;
отмена не является подтверждением целостности или readiness.

## Маршруты

Префикс всех маршрутов — `/api/v1/prerequisites`. Host выбирает loopback port.
Каждый запрос требует `X-HInstall-Token`; Host/Origin/cross-site и размер JSON
проверяются существующим HTTP handler. Browser token не хранится в access log.

| Метод и путь | Вход и результат |
|---|---|
| `GET /capabilities?mode=native` | Только зарегистрированный exact target; signed `requirements` для просмотра. Без coordinator — configured=false. |
| `GET /licenses/{component_id}` | Проверенный текст из signed catalog. |
| `POST /plans` | Mode, locale, choices auto/manual/use/skip, optional bool offline. Возвращает server plan ID, operation ID и plan для просмотра. |
| `POST /operations` | Plan ID/digest, explicit consent refs, accepted_plan, accepted_terms, terms_digest. Durable background start. |
| `GET /operations` | Unfinished по умолчанию, limit 1–100, after_id cursor. |
| `GET /operations/{id}` | Сохранённый status и execution snapshot. |
| `GET /operations/{id}/events` | after_sequence >=0, limit 1–1000; events и snapshot. |
| `GET /operations/{id}/attempts/{component_id}` | after >=0, limit 1–1000; attempt history. |
| `POST /operations/{id}/cancel` | Пустой JSON; durable cancel без ожидания installer. |
| `POST /operations/{id}/resume` | Пустой JSON для ранее сохранённых согласий либо explicit consent_refs; background resume. |
| `POST /operations/{id}/recheck` | Пустой JSON; синхронные read-only probes, без install/download. |
| `POST /operations/{id}/boundary` | when=now/later; только решение, performed=false. |
| `POST /operations/{id}/replace-plan` | Новый server plan ID и новые согласия; не исполняет installer. |

Неизвестные поля и повторные query values отвергаются. Полный план, произвольные
argv, paths, URL, publisher/trust/config/owner/target dictionaries не являются
входами HTTP. Local artifact selection через браузер пока не реализована;
SDK/CLI имеют отдельный явный контракт локальных файлов.

Успех HTTP-запроса возвращается с `200` и `{api_version:"1",result:...}`.
Внутреннее `result.api_version`, когда оно присутствует в SDK capabilities,
имеет отдельную публичную версию SDK `"1.0"`; это не версия HTTP envelope.
Семантические отказы — `400`; Host/Origin/token — `403`; внутренний безопасный
отказ — `500`. HTTP `200` не означает READY или завершённую product installation.

### Требуемые и найденные версии

Ответ configured capabilities содержит `requirements`: 1–128 объектов с ровно
шестью полями `component_id`, `kind`, `required`, `minimum_version`,
`maximum_version`, `scope`. Границы имеют тип string/null и всегда присутствуют.
Scope — `host`, `controller` или `product`. Coordinator получает эту проекцию
из заново проверенного signed catalog, без discovery и записи операции.
Каталог, local paths, artifact URLs, команды и evidence в неё не включаются.

Обе границы включительные; `null` снимает только соответствующее ограничение.
Две `null` означают отсутствие ограничения версии. Формат
`v?MAJOR.MINOR[.PATCH][+build]` сравнивается численно по трём частям; отсутствующий
PATCH равен нулю, `v` и build suffix игнорируются. Prerelease и PEP 440 не
поддерживаются. Например, `v1.2` и `1.2.0+build` равны для этой проверки.

Браузер соединяет requirements с `plan.components` по component ID лишь при
совпадении `product_id`, `owner_id`, полного `target` и `catalog_digest`.
Найденная версия уже находится в `plan.components[].detected_version`.
Версии выводятся безопасным обычным текстом; `null`, нестроковое, скрытое или
некорректное значение показывается как неопределённое. UI не сравнивает версии:
совместимость и readiness берутся только из server `initial_status`/
`initial_ready`, а затем operation status/checks. При старом сервере без
`requirements` сведения о диапазоне недоступны; это не равнозначно двум `null`.

Проекция additive: raw plan v1, его digest, consent refs, сохранённые операции
и повторная первоначальная проверка queued plan остаются прежними. HTTP не
принимает `requirements` или version constraints в запросах браузера.

## Review и восстановление

До 16 полных серверных планов хранятся в памяти сессии. При достижении лимита
старый неисполненный review удаляется. Он строится заново, а не восстанавливается
из присланного браузером JSON. Каждый план получает operation ID до start.
Start подтверждается только после durable регистрации coordinator. Если ответ
потерян, повтор того же plan ID читает ту же operation без implicit resume.

После перезапуска HTTP host новый token и новый SDK получают операции через
durable list. Plan/consent payload намеренно не выдаётся в summary/status.
Продолжение ранее принятого плана требует явного resume; изменение выбора —
нового `/plans` и `/replace-plan` с новыми согласиями. Смена catalog/owner/target
не разрешает управление прежней операцией или освобождение её claim.

`execution.state=queued/running` требует polling даже при `status=AWAITING_USER`.
При `execution.active=None` состояние controller неизвестно: нельзя делать
автоматический resume. READY подтверждает только выбранные prerequisites.

Подготовка и наблюдение нового READY аннулируют product review на сервере.
Перед filtered list guard вызывает публичный `prerequisites_busy()`: он читает
claim и controller lock зарегистрированного private store. Старый catalog/owner
claim, скрытый из list, тоже блокирует product operation; возвращается только
bool, без чужих operation ID/планов. Неизвестный lock трактуется как busy.
План продукта, построенный одновременно с подготовкой, получает PLAN_STALE.
Новый product submit/resume блокируется, пока prerequisite operation не завершена
или её controller ещё активен/неизвестен. После готовности надо заново получить
product inventory/plan и подтвердить его. Никакой callback не вызывает submit.
Повторный GET со старой revision не возвращает session observation назад.

Короткий session lock защищает review и регистрацию product operations/секретов.
Discovery, signature reads и recheck не держат его на всём вызове. Cancel не
берёт этот lock; coordinator сохраняет запрос и завершает текущий installer на
разрешённой границе, без kill и удаления shared prerequisites. Сессионная защита
не заменяет глобальную координацию с посторонними SDK/CLI controllers.
В частности, read-only busy probe не является атомарным межпроцессным lease
между product и prerequisite execution; этот отдельный механизм ещё не принят.

## Прогресс и границы приёмки

`prerequisites_events` возвращает сохранённые state/phase events. Существующий
download callback ещё не является durable byte-progress stream. Показывать
состояния или неопределённый progress допустимо; вычислять выдуманные проценты нет.
Текущий JavaScript wizard не подключён к этим routes: `gui_auto_flow=false`.

Целевые tests используют подписанный синтетический каталог, настоящие
SQLite/private files и threads, а installer заменён фиксированным test port.
`tests/test_wizard_prerequisites.py` проверяет binding, refusal, cancel barriers,
review invalidation и re-created SDK. `integration_tests/test_prerequisites_http.py`
использует настоящий loopback HTTP: auth/Host/Origin, lost-ACK idempotency,
пересоздание HTTP server/SDK и cancel во время blocked synthetic installer.
Это не process power-loss, не браузер и не установленный OS prerequisite.

Actual browser acceptance NOT RUN/BLOCKED в текущей среде. Загрузка bundled
Playwright browser build 1234 вернула 195-байтовый HTML вместо архива. Альтернативный
официальный Google Chrome for Testing 156.0.8078.47 загружен полностью
(125799226 bytes, ZIP CRC проверен), но запуск и даже `--version` завершаются
SIGSEGV (-11), в том числе из точной копии в `/tmp`. Причина не установлена.
Свидетельства среды: `packaging/acceptance-evidence/environment-20261025-cp012.json`.
Новые Windows/Docker/live packages, reboot/relogin, clean-host и production tests
NOT RUN. Следующий исполнитель должен выполнить их по
`docs/VERIFICATION_BACKLOG.md` на точных новых artifacts.
