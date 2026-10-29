# Веб-страница текущей работы

Владелец запросил страницу 26.10.2026 во время продолжающейся разработки.

Адрес: https://homensai-installer-work-20261026.hoseskhom.chatgpt.site

Sites project: `appgprj_6adf6a1e6b4c819195800ccc83cd3731`.
Доступ: owner-private, вход через ChatGPT. Публикация первой версии подтверждена
Sites в 2026-10-26T15:01:24Z. Это страница статуса разработки, не установщик.

На странице: текущий инженерный этап, точные области проверок, последний
подтверждённый checkpoint, blockers, исполнители и журнал. Данные — явно
датированный срез; кнопка GitHub проверяет новый commit и не подменяет результаты
приёмки. Страница сама не читает личные данные, локальные процессы или секреты.

Исходники сохранены в отдельном source repository Sites. Текущая локальная копия:
`.local-state/sites/homensai-work-20261026-01/`. В ней `.openai/hosting.json`
содержит только project ID и static directory. Credential не сохранять в файлах.
Первая source revision: `fcfafaf1e8ca269bab9eb6223c4acd1297f902c8`.

Для обновления использовать Sites skill и тот же project ID, сохраняя audience.
После финального checkpoint обновить страницу фактическим результатом и
опубликовать следующую версию. Source helper требует JSON с credential через
stdin. На этом Windows-хосте запуск exec с TTY недоступен; работает Node REPL
`child_process.spawn` с `windowsHide:true` и передачей credential только в stdin.
Архив пакуется тем же штатным site-workflow.mjs. Не выводить credential в журнал.

Проверки первой версии: HTMLParser и JavaScript parser PASS, публикация native
Sites — succeeded. Browser QA — NOT RUN: встроенный браузер и browser providers
недоступны в сессии. Это не препятствовало подтверждённой публикации.
