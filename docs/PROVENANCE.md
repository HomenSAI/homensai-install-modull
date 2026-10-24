# Происхождение реализации шага 1

© 2026 Serhii Khomenko · HomenS.AI

24.10.2026. Манифест, валидатор и Docker-рендер написаны в этом репозитории
как обобщение JobPilot Pro. Исходники JobPilot прочитаны **только для анализа**,
не изменялись и не исполнялись. Каталог источника:
`C:/homensai/homensai-jobpilot-pro/homensai-jobpilot-pro`.
Все шесть запрошенных файлов доступны; fallback только на аудит не потребовался.

| Исходный файл JobPilot | Заимствованный контракт и результат |
|---|---|
| `install.sh:132–164`, `:248` | Проверка ID/порта, отказ от fresh поверх данных, адрес 127.0.0.1 и `/health/ready`; обобщены в `shared/manifest.schema.json`, `shared/manifest.py`, `docker/compose.template.yml`. Скрипт установки не переносился. |
| `install.ps1:321–330`, `:394–417` | Требование полного архива перед заменой, DataHome вне программы, readiness URL; отражены в `docs/MANIFEST.md`, полях манифеста и тестах. PowerShell/ACL/подготовка DataHome не переносились. |
| `Dockerfile:9`, `:34–58` | Закреплённый Python-образ, UID 10001, отдельный `/data`, запуск `backend.main`, healthcheck и пауза 60 с; производные файлы — `docker/Dockerfile.template`, `docker/compose.template.yml`, `shared/render_docker.py`, пример манифеста. Общий HTTP probe написан заново вместо продуктового CLI. |
| `scripts/install_archive_policy.py:15`, `:50` | Чтение политики отдельно от исполнения и отказ при неверном контракте; обобщены в строгое логическое `archive_before_replace: true`. SQL, пользовательские политики и чтение базы не переносились. |
| `scripts/install-archive-policy.ps1:7`, `:17`, `:29` | Проверка политики/идентичности перед доступом к архиву; учтена граница шага 1: манифест не читает и не расшифровывает архивные настройки. DPAPI-код не переносился. |
| `backend/platform/data_home.py:1–66` | Программа отдельно от постоянного корня; состав `data/documents/configuration/secrets/history/installation`; описаны в `docs/MANIFEST.md`, контейнерный корень параметризован через `data_home_env`. Перенос хостовых путей и runtime.env не реализован. |

`shared/__init__.py`, `shared/manifest.py`, `shared/manifest.schema.json`,
`shared/render_docker.py`, `examples/jobpilot.manifest.json`, `tests/`, `pytest.ini`
и документация — новая реализация производных контрактов; установщики JobPilot,
бинарники и сторонние зависимости в модуль не скопированы. Ограничение read-only
корня, единый том и текстовые тесты добавлены по заданию этого шага.

SHA-256 прочитанных файлов (снимок содержимого, без обращения к Git metadata):

| Файл | SHA-256 |
|---|---|
| `install.sh` | `962fc2300ddff0f86a7910434c663d9f376328a8a6e91ccae936442649ef6c7e` |
| `install.ps1` | `6aec408b25f61f210f963963722092820d12a9dc0c999e4f39143e76213e630a` |
| `Dockerfile` | `faefa5be0a02b8aae3958bb5405e0ca134f4d334154c42d4e9bc6e5d6e26b829` |
| `scripts/install_archive_policy.py` | `8efb80ed1ff1071604a7960bcd0429c4a32ee0fd192e4f52e53ff0e1c09dc637` |
| `scripts/install-archive-policy.ps1` | `f1d04e21d18062f8dba088354b72a815ea43e743a03afa289eb66f8356394807` |
| `backend/platform/data_home.py` | `fc72218ccd586687b7e157d0c44253651acc5434be055606f4ab1af2d7291040` |

Основание анализа и внутреннего обобщения — прямое задание владельца обоих
репозиториев. Авторские уведомления JobPilot: © 2026 Serhii Khomenko, HomenS.AI.
`install.sh:18` обозначает закрытую лицензию. Полный LICENSE JobPilot не входил
в разрешённый список и не читался; разрешение на публичное распространение
и лицензии будущего payload этим шагом не устанавливаются. Новая лицензия
для исходников не назначалась.

Дополнительные основания: `README.md`, `docs/REQUIREMENTS.md`,
`docs/AUDIT-RENT-2026-10-24.md`. Из Rent код не переносился; сведения его аудита
не являются доказательством функциональной готовности JobPilot или этого модуля.
