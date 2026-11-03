# Руководство разработчика адаптера продукта

Для кого: разработчик продукта семейства HomenS.AI (JobPilot, Rent и др.),
который подключает его к общему установщику. Эталон — вымышленный продукт
[examples/demo_product](../examples/demo_product/README.md) и сквозные
тесты `tests/test_runtime_native.py`.

**Рекомендуемый путь — сборщик комплекта [KIT](KIT.md):** `python -m shared.kit
init / keygen / fetch / build / verify` формирует из проекта подписанный
комплект для Linux и Windows со встроенным Python, зависимостями и
установщиком `setup.sh` / `setup.cmd`. Разделы 2–4 ниже описывают ручную
сборку пакета и прямые вызовы API, на которых основан комплект.

Модуль не выводит свойства продукта сам: адаптер объявляет корни данных,
компоненты, зависимости, миграции, совместимость и проверку здоровья.
Объявление в манифесте не доказывает их корректность — у каждого продукта
должна быть собственная интеграционная приёмка.

## 1. Требования к продукту

- Запуск без интерактивного ввода, только argv; без shell-конструкций.
- Все изменяемые данные — в каталогах `data`, `backup`, `config` вне
  заменяемого выпуска. Выпуск (`releases/app-<версия>`) только читается.
- Каждый компонент корректно завершается по SIGTERM в пределах таймаута
  systemd; иначе обновление и архив откажут (это проверяется).
- Web-компонент слушает только `127.0.0.1` и отвечает на путь `web.healthz`
  HTTP 200 с JSON ровно из трёх полей:
  `{"product": "<product_id>", "release": "<версия>", "healthy": true}` —
  [HEALTH](HEALTH.md).
- Миграции идемпотентны и транзакционны: повтор после сбоя «после commit» не
  должен применять изменения дважды. Перед каждой миграцией установщик делает
  отдельную копию; откат восстанавливает данные из копии.
- Секреты не попадают в манифест, команды, файлы выпуска и журналы; продукт
  получает их от платформы (EnvironmentFile на Linux, DPAPI на Windows —
  будущий блок).
- Если вне служб есть другие процессы-писатели, они должны использовать
  совместную блокировку [WRITER-GUARD](WRITER-GUARD.md).

## 2. Манифест v2

Полный контракт — [MANIFEST](MANIFEST.md). Минимально важное:

- `product_id` — `[a-z0-9][a-z0-9_-]*`; из него строятся имена units
  `<product_id>-<компонент>.service`.
- `paths` — семь относительных путей внутри корня установки; постоянные
  корни не пересекаются с `app`/`releases`.
- `compatibility.targets` — явно разрешённые сочетания ОС, архитектуры и
  способа запуска; без секции совместимость неизвестна и установка отказывает.
- `components[].cmd` — первый аргумент обязан начинаться с `@runtime/...`
  или `@release/...`; доступны обозначения `@release`, `@runtime`, `@data`
  и `@app` с необязательным относительным суффиксом (`@releases` запрещено). Запуск идёт через загрузчик pointer-v2, который на каждом старте
  читает указатель активного выпуска.
- `components[].after` задаёт порядок запуска; остановка — в обратном.
- `modes` и `mutual_exclusion` — допустимые наборы компонентов.
- `migrations.cmd` — argv с целыми аргументами `@DATA_DIR@` и
  `@MIGRATION_ID@`.
- `runtime` — Python 3.14, lock-файлы Windows и Linux и wheels с SHA-256;
  `offline: true`.

Проверка: `python -m shared.manifest путь/manifest.json` или API `validate`.

## 3. Payload и пакет

Каталог payload содержит:

- код продукта (например, `demo/main.py`, `demo/release.json`);
- `runtime-linux/` — переносимый CPython 3.14 с рецептом
  `runtime-v1.json` и загрузчиком `lib/homensai/portable_launcher.py`
  (байты должны совпасть с закреплённым хэшем установщика) —
  [RUNTIME-LINUX](RUNTIME-LINUX.md);
- `systemd-user/pointer-v2.json` — подписанный рецепт служб
  (`{"format":2,"generator":"homensai-systemd-user-pointer-v2"}` и перевод
  строки, без других файлов в `systemd-user/`);
- все lock-файлы и wheels, перечисленные в `runtime`.

Сборка и подпись:

```python
from shared.package import build_package
from shared.package_signing import sign_package

receipt = build_package(manifest, '1', '/abs/payload', '/abs/out/product-1.zip')
envelope = sign_package('/abs/out/product-1.zip', private_key=private_key,
                        expected_product_id=manifest['product_id'], expected_version='1')
```

Приватный ключ Ed25519 хранится у издателя вне репозитория; открытый ключ
передаётся пользователям по независимому каналу. Сборка отказывает при
несовпадении хэшей lock/wheel; wheels проходят структурный аудит, а для
зависимостей можно приложить офлайн-отчёт OSV — [SUPPLY-CHAIN-POLICY](SUPPLY-CHAIN-POLICY.md),
[CRYPTO-LOCK](CRYPTO-LOCK.md), [PACKAGE](PACKAGE.md).

## 4. Подключение через API

Продукт вызывает установщик через локальный JSON API и Python-клиент
`homensai-installer-client` — [PYTHON-CLIENT](PYTHON-CLIENT.md),
[API-DESIGN](API-DESIGN.md). Перед вызовом проверяются `capabilities` и
`min_minor`; изменяющие операции не повторяются автоматически.

| Задача | Операции |
|---|---|
| Проверка | `validate`, `check-target`, `verify-package`, `audit-signed-wheels` |
| Первая установка Linux | `install-portable-linux`, `inspect-portable-linux-install` |
| Обновление и откат | `prepare-signed-linux-update`, `run-pointer-linux-update`, `inspect-started-pointer-update`, `cycle-updated-pointer-linux` |
| Архив | `check-archive-storage`, `import-update-archive` |
| Удаление и переустановка | `plan-portable-linux-uninstall`, `uninstall-portable-linux`, `reuse_preserved_data` — [UNINSTALL](UNINSTALL.md) |
| Диагностика | `check-local-health`, `inspect-technical-log`, `check-docker-engine` |

Идентификаторы операций (32 hex) создаёт и хранит интегратор: повтор с тем
же ключом продолжает операцию, а другой ключ при начатой операции
отклоняется.

## 5. Языки

Сообщения установщика берутся из словарей `shared/locales/*.json` (ru, en,
de). Дополнительный язык подключается как полный словарь без исполняемого
кода и проверяется `validate-language-catalog` — [LANGUAGES](LANGUAGES.md).

## 6. Проверка адаптера

1. Валидация манифеста и `check-target` для каждого заявленного профиля.
2. Сборка двух подписанных версий; повторяемость пакета.
3. В изолированном systemd (`tests/run_isolated_systemd_test.py`) по образцу
   демо-продукта: установка, прерывание и повтор, обновление, отказ миграции,
   отказ health с откатом, удаление с сохранением данных, переустановка.
4. Пути с пробелами и кириллицей, недостаток места, повреждённый пакет и
   архив, параллельный запуск.
5. Проверка реального состояния служб и данных, а не только кодов возврата.

Готовность продукта заявляется только по результатам его собственной
приёмки на целевых системах; приёмка общего модуля на демо-продукте её не
заменяет.
