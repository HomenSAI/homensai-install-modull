# Локальный prerequisite-файл в браузерном мастере

Автор: Serhii Khomenko. Контрольная точка: `CP-20261025-020`.

Мастер принимает файл только после явного выбора пользователем и отдельной
команды загрузки. Staging не запускает installer и не меняет readiness. После
проверки файла требуется заново построить план, показать его и получить отдельные
согласия. Исполнение остаётся в прежнем SDK, coordinator и execution lease.

## HTTP-контракт

Для exact-bound coordinator `GET /api/v1/prerequisites/capabilities` возвращает
`local_artifact_upload=true` и `local_upload_max_bytes=4294967296` (4 GiB).
Без coordinator или для другого mode capability отсутствует.

1. Построить обычный prerequisite plan с `choice=auto` для нужного компонента.
2. `POST /api/v1/prerequisites/uploads` с JSON `plan_id`, `plan_digest`,
   `component_id`, `artifact_id`. Сервер берёт размер и SHA-256 из ранее
   проверенного signed plan, а не из полей browser request.
3. Ответ allocation содержит только `upload_id`, `component_id`, `artifact_id`,
   `size`, `sha256`, `state=pending`, `verified=false`.
4. `POST /api/v1/prerequisites/uploads/{upload_id}/content` передаёт raw bytes
   с точным `Content-Type: application/octet-stream` и одним `Content-Length`,
   равным подписанному размеру. В браузере `fetch` с body File выставляет длину
   самостоятельно. Multipart, chunked и произвольные имена/пути не принимаются.
5. Только после точной длины, SHA-256, `flush/fsync` и повторного чтения
   staged-файла ответ получает `state=verified`, `verified=true`.
6. Новый `POST /api/v1/prerequisites/plans` передаёт `local_uploads`:
   `{artifact_id: upload_id}`. SDK повторно проверяет каталог, inventory и путь;
   новый план требует новых consent refs. Наличие verified upload не запускает
   установку и не является dependency READY.

`GET /prerequisites/uploads/{upload_id}` читает состояние без эффектов.
`POST /prerequisites/uploads/{upload_id}/cancel` с `{}` отменяет pending/active
upload и удаляет только его собственный незакреплённый файл. Запрос отмены
доступен отдельным HTTP-потоком. Если поток ожидает чтения из сокета, очистка
может ждать bounded socket timeout (15 секунд). Обрыв передачи, неверный hash,
ошибка IO и отмена не дают verified результата; partial удаляется при обычном
завершении обработчика. При неизвестной замене inode файл не удаляется.

## Проекция плана и сохранность

HTTP возвращает в `plan.local_artifacts` opaque upload IDs. Внутренний
неизменяемый SDK plan хранит приватные абсолютные staging paths. Поэтому
`plan_digest` относится к внутреннему SDK plan и проверяется сервером через
`plan_id`; browser не вычисляет digest из HTTP-проекции. SDK/CLI контракт не
меняется, пути из HTTP по-прежнему запрещены. Новые upload ответы не содержат
filename, local path, URL/query, owner, command или trust configuration.

До 16 upload records и суммарно до 4 GiB незавершённых/verified reservations
разрешены на одну серверную сессию. Failed/canceled records сохраняются до конца
сессии для безопасного ответа на поздние запросы и входят в лимит количества.
Число bytes signed SDK artifact по-прежнему ограничено его прежним контрактом;
4 GiB — более узкая граница браузерного транспорта.

Путь staging выбирает только доверенный host: private `api.state_dir`, подкаталог
`wizard-uploads` и случайный каталог сессии. Файлы имеют случайное серверное имя,
создаются исключительно через `O_EXCL`, POSIX mode 0600; каталоги — 0700.
Проверяются owner, file type, hardlink, symlink/reparse и identity каталога/файла.
На Windows используется унаследованная ACL доверенного state directory; этот
код не является отдельной приёмкой Windows ACL/UAC.

Перед передачей пути в SDK файл снова полностью проверяется и удерживается
для плана. Cancel upload после этого отказывает `OPERATION_CONFLICT`: browser
не может удалить файл, удерживаемый планом или durable operation. Исполнитель
повторяет прежние hash/staging проверки непосредственно перед installer.

Перезагрузка страницы не начинает и не продолжает операцию. Незавершённый выбор
файлов браузер повторяет явно. Новая серверная сессия не принимает старые opaque
handles. Уже сохранённая SDK operation имеет собственный plan и staging path;
её status/cancel/resume работают через прежний recovery-контракт, а не через
upload handles. Потеря ACK upload допускает чтение status; повтор content не
перезаписывает verified файл.

Staging bytes, удерживаемые планами, не удаляются автоматически. После kill или
потери питания partial также может остаться. Автоматическая межсессионная
очистка/retention с проверкой всех journal references пока не реализована;
слепое удаление старых каталогов запрещено. Перенос state/control journals на
новую файловую identity не становится разрешённым этим механизмом.

## Проверки и пределы

`tests/test_local_artifacts.py`, `tests/test_wizard_local_artifacts.py` проверяют
staging, bounded reads, hash, file identity, cancellation, exact signed binding,
новый review и путь прежнего SDK без сетевой загрузки выбранных файлов.
`integration_tests/test_local_artifact_upload_http.py` проверяет настоящий
loopback HTTP, Host/Origin/session, framing, дубли security headers, обрыв,
неверные bytes и явный start через прежний coordinator. Они используют
синтетические installers и не заменяют реальную платформенную приёмку.

Node и actual browser проверки мастера, installed wheel и полная Windows/Linux
matrix фиксируются отдельными receipts текущих bytes. Полная задача HIM-033 и
ACC-01/05/14/16/23 остаются открыты до их обязательной приёмки;
`production_ready=false`.
