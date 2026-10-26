# Reference product

Небольшой реальный web server + SQLite. Только синтетические данные, без email,
Telegram, платежей и внешней сети. Web и migrations используют runtime из поставки.

Сборщик `packaging/build_reference.py` создаёт настоящий wheel, устанавливает его
в изолированный bundled Python, закрепляет hashes и подписывает полный payload
тестовым ключом. По умолчанию ключ остаётся только в памяти сборочного процесса.
Для последовательности update/rollback допускается явно заданный приватный
DEV key file вне репозитория; файл никогда не входит в поставку.
Ключ проверки встраивается в frozen bootstrap. Такая сборка имеет development
marker и не является коммерческим production release.

Текущая reference версия поддерживает Linux x64 user-session. Windows/Docker
и автоматический старт как служба ОС требуют отдельного принятия. Пользователь
не должен устанавливать Python для запуска frozen Linux bootstrap.

Профили v1/v2/v3 и контролируемые отказы описаны в `docs/REFERENCE_SCENARIOS.md`.
Версия wheel описывает общий тестовый harness; версия установленного продукта
и схема SQLite выбираются строго проверяемым подписанным `reference-profile.json`.
Все сборки здесь — DEV. Повторяемая процедура не означает доказанную побайтовую
воспроизводимость executable или принятый production выпуск.

EULA в `legal/` — согласованные черновики ru/en/de для reference-проверок.
Выпуск реального продукта блокируется до определения издателя, product license,
юрисдикции, обязательств поддержки и юридического утверждения текстов.

`homensai_reference.adapter` — исполняемый JSON ProductAdapter протокола 1.0.
Первый принятый набор — `describe`, `readiness`, `quiesce`, `operation_status`.
Readiness читает настоящий loopback HTTP и сохраняет наблюдённую identity;
quiesce проверяет SQLite и фиксирует durable application receipt. Reference
server имеет только читающие endpoints и не запускает фоновых писателей.
Порт берётся из подписанного `adapter-config.json` текущего release.

Независимый проверочный профиль:

```text
python packaging/check_installed_product_adapter.py --work-dir /tmp/new-maintenance-check --receipt /tmp/new-maintenance-receipt.json --wheelhouse /opt/wheelhouse
```

Нужны Linux x64, подготовленный build environment Python 3.14 и wheels из
`requirements-build.lock`. Harness собирает новые SDK/reference wheels,
устанавливает их вне checkout, использует отдельные процессы клиентов и
проверяет настоящий start/stop/full encrypted backup с восстановлением в
пустую candidate-папку. Обычные клиенты не могут читать checkout, обращаться
к внешней сети или запускать executable вне собственного installed release.

Первоначальная установка этого maintenance-профиля выполняется существующим
legacy NativeEngine. Она не доказывает ProductAdapter fresh/update. Product
venv содержит подписанный скопированный ELF, но использует stdlib, libpython
и OS libraries DEV-контейнера; это не standalone payload и не приёмка чистой
Ubuntu/Windows. Production signing, runtime/license closure, GUI и все G0–G5
этой проверкой не закрываются. Каждая попытка получает новый work-dir и receipt;
ранний FAIL сохраняется через `--previous-receipt`.

Реализованный read-only `migration_plan` принимает `installation_id`,
`from_release`, `to_release`, `mode` (`fresh_empty` или `upgrade`). Начальную
схему он наблюдает в SQLite, целевую читает из подписанного reference profile.
Fresh возвращает отдельный `from_version="empty"` и не создаёт каталоги или БД.
Неизвестная/повреждённая БД, WAL/journal и downgrade требуют отдельного решения
и отвергаются до изменений. Дескриптор maintenance-проверки этот hook пока не
объявляет; installed adapter fresh/update остаётся NOT RUN.

`migration_apply` принимает только согласованный plan digest, один opaque
`backup_receipt_id`/`fresh_receipt_id` и engine-owned `migration_binding`.
Проверка реального backup/fresh proof выполняется trusted authorizer SDK до
запуска executable; продукт не считает произвольный ID доказательством архива.
Схема и application result receipt фиксируются в одном SQLite COMMIT через
существующий migration engine reference. Отдельный durable intent не даёт права
повторять эффект. Потерянный ACK восстанавливается по business receipt; отсутствие
строки, WAL/неизвестная БД или незавершённые внешние defaults дают UNKNOWN.
Этот reference hook проверен отдельно; общий installed engine fresh/update
требует собственного актуального acceptance receipt.

`packaging/check_installed_reference_effects.py` отдельно проверяет транспорт
установленного SDK из точного `--sdk-revision`: подписанный subprocess возвращает
nonzero до эффекта либо после business COMMIT. Во втором случае reconciliation
подтверждает ровно одну миграцию v1→v2, а повтор из нового controller не исполняет
её снова. Начальная v1 здесь — явный legacy product fixture; trusted authorizer
является синтетическим exact-request oracle. Этот результат нельзя использовать
как доказательство реального backup proof или общего engine fresh/update.
