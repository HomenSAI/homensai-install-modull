# Reference product: настоящие версии данных и сценарии восстановления

Reference — синтетический web product с настоящей SQLite-базой. Он нужен для проверки общего установщика на install/update/backup/restore/rollback/uninstall. В продукте нет внешних API-запросов, реальных пользовательских аккаунтов, платежей или исходящих интеграций.

Один набор исходников поддерживает три состояния продукта, выбранные явным `reference-profile.json` в корне release. Профиль вместе с runtime, wheel и исходниками должен входить в полный подписанный inventory. Число в `product_version` не подменяет миграцию: readiness дополнительно проверяет фактическую схему и целостность SQLite. Build release ID остаётся отдельной идентичностью общего engine.

## Формат профиля

```json
{
  "schema_version": 1,
  "product_version": "1.0.0",
  "database_schema": 1,
  "health_failure": false,
  "migration_failure": false,
  "require_api_key": false
}
```

`schema_version=1` — версия формата профиля. `database_schema` принимает только целые `1`, `2`, `3`. `product_version` имеет форму версии, например `1.0.0`, `2.0.0`, `3.0.0`. Первые три поля обязательны. Три boolean-флага могут отсутствовать и тогда равны `false`. Неизвестные поля, строки вместо bool, duplicate JSON keys, NaN, oversized JSON и ссылки на другой файл отвергаются.

Файл читается из текущего каталога release. Путь к нему не берётся из окружения. При отсутствии файла применяется совместимый v1-профиль; повреждённый существующий файл не заменяется значениями по умолчанию. `HOMENSAI_RELEASE` определяет только идентичность выпуска, а не версию БД. Environment flags не включают искусственные отказы.

| Сценарий | product_version | database_schema | Явный флаг |
| --- | --- | --- | --- |
| v1 | `1.0.0` | 1 | Нет |
| v2 | `2.0.0` | 2 | Нет |
| v3 | `3.0.0` | 3 | Нет |
| v2 с отказом migration | `2.0.0` | 2 | `migration_failure=true` |
| v3 с отказом health | `3.0.0` | 3 | `health_failure=true` |
| Версия с обязательным ключом | Выбранная версия | 1/2/3 | `require_api_key=true` |

Флаги отказа принадлежат только этому synthetic reference profile. Они не запускают произвольные команды и не предоставляют скрытую production-настройку общего engine. Чтобы изменить их в готовом payload, требуется новый подписанный inventory; правка файла после подписи должна отклоняться verifier.

## Реальные миграции и пользовательские данные

Файл базы: `DATA_HOME/reference.sqlite`.

| Схема | Изменение |
| --- | --- |
| v1 | Таблица `notes(id, text)`, одна синтетическая стартовая запись. |
| v2 | Добавляются `normalized_text`, `text_length`, `category`. Для имеющихся записей нормализуются пробелы и регистр Unicode; исходный `text` сохраняется. |
| v3 | Добавляются `content_sha256`, `is_archived`, таблица `search_terms(note_id, term)`. SHA-256 вычисляется из точного исходного текста, поисковые термины — из его нормализованного представления. |

Миграция v3 также рассчитывает производные поля для записей, добавленных или изменённых после v2. Пользовательские `category`, исходный текст и остальные записи сохраняются. У таблицы `schema_migrations` есть маркеры выполненных/обнаруженных схем, без выдуманной исторической даты установки legacy v1.

Переходы выполняются в `BEGIN IMMEDIATE` transaction с `synchronous=FULL`, включая DDL и `PRAGMA user_version`. Повтор v2 или v3 на уже соответствующей схеме не повторяет преобразования и не сбрасывает пользовательские поля. Сразу установить v2/v3 на пустую БД можно: реально выполняются все предшествующие шаги.

Запуск migration старой версии на новой БД возвращает `REFERENCE_DOWNGRADE_REQUIRES_SNAPSHOT_RESTORE`. Он не переписывает `user_version` и не удаляет новые столбцы. Возврат к предыдущей версии выполняет общий engine посредством проверенного snapshot restore; сам reference product не копирует логику отката установщика.

После успешного commit создаются только отсутствующие начальные файлы:

- `DATA_HOME/user-files/welcome.txt` — собственный синтетический пользовательский файл;
- `CONFIG_HOME/settings.json` — явные настройки `schema_version`, `site_title`, `language`, `theme`, `show_release`.

Если `CONFIG_HOME` не передан старым окружением, используется `DATA_HOME/config`. Уже существующие файлы не заменяются и не переформатируются. Неизвестные дополнительные настройки остаются в файле; web UI использует только известные поля. Изменённые title/language/theme/show_release действительно влияют на HTML. Файловая и SQLite транзакции не объявляются одной атомарной транзакцией: при ошибке создания defaults повторный вызов не повторит уже завершённые DB migrations. Общий update snapshot покрывает и БД, и конфигурацию, и файлы данных.

## Намеренные отказы для проверки engine

`migration_failure=true` вызывает `REFERENCE_MIGRATION_FAILURE_REQUESTED` внутри SQLite transaction после выполнения запланированных DDL/data changes и до commit. Transaction откатывает новые таблицы/столбцы, преобразованные данные и `user_version`. Проверяется настоящий failure path, а не искусственная замена return status без работы с БД. Для первой установки может остаться пустой SQLite-файл со схемой 0; программа не объявляется установленной.

`health_failure=true` позволяет успешно выполнить migration, но заставляет `/healthz` вернуть `ready=false` и HTTP 503. Так проверяется более сложный случай: после изменения данных общий engine должен восстановить предыдущую версию и совместимый snapshot БД. Одного возврата active pointer недостаточно — старый сервер отклонит новую схему.

## Readiness, версия и API key

`GET /healthz` сохраняет совместимые поля `product_id`, `installation_id`, `release`, `ready` и добавляет:

```text
product_version
database_schema
expected_database_schema
api_key_configured
api_key_required
checks.database / checks.settings / checks.api_key / checks.profile
```

Для `ready=true` нужны ожидаемая реальная схема SQLite, структурные таблицы/столбцы, успешный `quick_check`, допустимые настройки, выполнение key policy и отсутствие `health_failure`. Чтение отсутствующей БД не создаёт её. Проверка архива поддерживает все три схемы и проверяет целостность через read-only SQLite connection.

Продуктовый секрет называется **`REFERENCE_API_KEY`**. Он передаётся только через выделенный environment процесса сервиса. Для профиля с `require_api_key=true` builder объявляет manifest secret с `required=true`, `contexts=["service"]`, `same_value=true`. Reference проверяет только наличие непустого значения; подлинность ключа во внешнем сервисе не проверяется и внешний сервис не вызывается.

В HTTP, HTML и диагностическом выводе присутствует только boolean configured/ready. Значение ключа не записывается в БД, defaults, profile или stdout/stderr. Нельзя использовать `HOMENSAI_*` для продуктового ключа: эти имена зарезервированы общим engine.

Web server слушает только `127.0.0.1`, проверяет Host, не пишет request logs и выставляет CSP/no-store/nosniff. Публичная страница показывает версию, схему, синтетические записи и реальные производные поля. Текст записей и пользовательский title экранируются; ключ туда не подставляется. Неготовая страница тоже имеет HTTP 503.

## Сценарии полной приёмки установщика

Следующая последовательность предназначена для отдельных signed bundles и общего GUI/SDK. Она не считается автоматически выполненной от одного source-теста:

1. **Install v1.** Через общий мастер принять реальные условия/план, установить v1. Проверить `/healthz`, фактическую schema 1, стартовую запись и defaults.
2. **Пользовательские изменения.** Изменить `welcome.txt`, title/language/theme в settings и добавить синтетическую запись в БД. Зафиксировать ожидаемые bytes/rows.
3. **Update v2.** Создать новый план для подписанного v2. Проверить schema 2, настоящие normalized fields, неизменные исходные тексты/user-file/settings и сохранённый pre-update archive.
4. **Update v3.** Проверить schema 3, content hashes/search terms, сохранение пользовательских данных. Успех означает readiness целевого выпуска, а не один успешный exit migration.
5. **Backup/restore.** Сделать зашифрованный архив через общий engine, изменить синтетические данные, восстановить выбранный snapshot с явным подтверждением. Проверить bytes, schema и release identity.
6. **Failure during migration.** Из v1 попытаться перейти на v2 с `migration_failure=true`. Проверить итоговое состояние, прежнюю schema/данные и отсутствие ложного successful update.
7. **Failure after migration.** Из принятой v2 перейти на v3 с `health_failure=true`. Общий engine должен вернуть предыдущие release/runtime и совместимый snapshot schema 2. Обновление остаётся неудачным даже при успешном откате.
8. **Downgrade guard.** Вызов v1/v2 migration непосредственно на schema 3 обязан отказать без изменения данных. Для настоящего rollback использовать общий snapshot workflow.
9. **Required key.** Пройти выбранный профиль с обязательным key через protected GUI secret input. Подтвердить configured boolean и отсутствие значения в событиях/выгрузке/HTML. После перезапуска мастера секрет вводится заново.
10. **Uninstall preserving data.** Удалить программу через общий API и проверить сохранность пользовательских файлов, конфигурации и архивов. После restore подтвердить работу соответствующей версии.

Сборка передаёт профиль в `packaging/build_reference.py --profile PROFILE_JSON`, а builder копирует его в payload **до** расчёта inventory и подписи. Для каждого сценария используются собственный release ID и соответствующий профиль; неподписанная подмена готового bundle не является допустимым способом включить отказ.

## Выполненная source-проверка

```text
python -m pytest -q tests/test_reference_product.py
```

[Тесты reference product](../tests/test_reference_product.py) работают с настоящей SQLite и временными данными, не открывают сокеты и не выполняют исходящих запросов. Проверены v1/v2/v3, fresh v2/v3, повторные migrations, rollback DDL/data/user_version при намеренном сбое, downgrade guard, legacy v1, сохранение files/config, malformed profiles, actual schema readiness, key redaction и HTML escaping.

Helpers для отдельной headless-приёмки: `load_profile(release_dir=None)`, `migrate(data_home, config_home=None, profile=...)`, `server.readiness(...)`, `server.read_notes(...)`, `server.render_page(...)`, `archive.verify_database(...)`. Полная GUI/frozen-bundle приёмка, Docker/Windows и восстановление общего engine подтверждаются отдельными сценариями и квитанциями.
