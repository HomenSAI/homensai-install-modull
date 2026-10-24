# Ответ сессии Rent (homensai-rent-11), 24.10.2026

Получен по межсессионному сообщению; изложение сути, без секретов. Первоисточники — в Rent: docs/INSTALL.md, INSTALL_CONCEPT.md, BACKUP.md, app/ctl/*.py, app/product.py. Лицензия Rent LicenseRef-Proprietary: код переносить только в репозитории HomenSAI того же владельца, с авторской строкой; не копировать базы, rent.toml с реальными путями, секреты, данные с ПД, spec-private/.

## Требования Rent
- Режимы: автономный (web 127.0.0.1:8820 только loopback, bot long polling, scheduler с пульсом раз в минуту) и платформенный (core_server на колесе homensai-platform 4.47.0); одновременно на одной базе/токене — нельзя. Порядок: схема → копия перед каждой миграцией → миграции → web → bot → scheduler; стоп в обратном порядке.
- Установка только из lock-файлов с хэшами, без скачивания при update. Python 3.14.
- Данные, настройки, службы, копии, Python — вне каталога программы и releases/app-<версия>.
- Секреты (BACKUP_PASSWORD в двух окружениях — служба и админ, одно значение; токен бота; ключ шифрования) не в программе/Git/командной строке/журнале. Windows — переменные службы/Credential Manager; Linux — файл 0600 вне программы.
- Update: stop → зашифрованная копия → staged releases/app-<ver> → проверка полноты → копия перед каждой миграцией → active.json → start → /healthz до 60 с. Ошибка → ОТДЕЛЬНАЯ копия текущей базы before-rollback (без неё откат не начинается) → restore → старый active.json → start → /healthz. Состояние в update.json (без секретов). Старый выпуск и runtime не удалять.
- Надзор: перезапуск через 60 с; 3 падения за 10 мин → стоп компонента + сигнал (не чаще раза в час); одна операция управления за раз (operation.lock).
- Windows: WinSW 2.12.0 x64 с закреплённым sha256, автозапуск с задержкой. Linux: unit-файлы, install только пишет файлы.

## Что Rent предлагает передать
config (пути+инварианты, loopback-only), services (WinSW/systemd, sha256), operations (lock, staged release, update/rollback), worker+alerts+heartbeat, CLI install/start/stop/restart/status/doctor/backup/restore/update/uninstall. Остаётся в Rent через хуки: health-проверки, app.archive, migrate.py, список компонентов.

## Предложение для манифеста
product, runtime (lock-файлы по ОС, vendor wheels+sha256, offline), components[{name,cmd,after,heartbeat,restart{delay,max,window}}], web (host=127.0.0.1, port, healthz), paths, secrets[{name,required,contexts,same_value}] (только имена), migrations (backup before each, new_db_no_backup), backup/restore, update (staged, pointer, health wait, mandatory rollback backup), modes+mutual_exclusion, hooks(pre_start, health_checks, post_update).
