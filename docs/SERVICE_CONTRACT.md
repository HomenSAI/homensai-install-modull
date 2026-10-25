# Подписанный contract системных служб

`homensai_install.service_contracts` добавляет строгий подготовительный слой
для systemd user units и WinSW. Он проверяет подписанный contract, создаёт
конкретные тексты конфигураций и receipts владения. Он **не устанавливает службы**,
не вызывает SCM/systemctl и возвращает `execution_supported=false`.

Этим сохраняется различие между проверенным source renderer и работающим
Windows/Linux service adapter. NativeEngine остаётся Linux user-session backend
до выполнения live-host gates.

## Файл внутри signed payload

`service-contract.json` включается в полный signed inventory. Loader сначала
вызывает доверенный verifier для всего payload, manifest, target, release и
runtime, затем проверяет точный hash contract. Изменённый wrapper, неизвестное
поле или неподписанный executable блокирует обработку.

Systemd user пример:

```json
{
  "schema_version": 1,
  "backend": "systemd-user",
  "components": [
    {
      "name": "web",
      "stop_cmd": ["python", "stop.py", "--wait"],
      "stop_timeout_seconds": 30
    }
  ]
}
```

Для systemd разрешён пустой `stop_cmd`, если продукт действительно завершает
всех своих писателей по SIGTERM. Unit использует `KillMode=control-group` и
`SendSIGKILL=no`. Если graceful stop не завершён, adapter обязан остановить
backup/update и подтвердить отсутствие оставшихся writers; статус одного
главного PID не заменяет эту проверку.

Windows пример:

```json
{
  "schema_version": 1,
  "backend": "windows-winsw",
  "components": [
    {
      "name": "web",
      "stop_cmd": ["python", "stop.py", "--wait"],
      "stop_timeout_seconds": 30
    }
  ],
  "wrapper": {
    "file": "tools/winsw.exe",
    "version": "2.12.0",
    "license": "MIT",
    "license_file": "legal/third-party/winsw.txt"
  }
}
```

Для этого backend target должен иметь `os=windows`, `mode=native`, `scope=machine`.
Версия и SHA-256 wrapper также должны совпасть с `manifest.winsw`; файл лицензии
входит в signed payload. Версия 2.12.0 следует существующему v2 schema pin проекта
и не означает заявление о том, что это последний выпуск WinSW. Изменение версии
требует отдельного compatibility/license/security review и VM gate.

Windows stop-команда обязана содержать executable и непустые arguments. Она
должна синхронно ожидать завершения продукта и подтверждать закрытие БД.
`TerminateProcess` после тайм-аута не считается успешной остановкой для snapshot.
Проверка quiescence находится в обязанности live adapter и пока не реализована
этим renderer.

## SDK

```python
from homensai_install.service_contracts import (
    load_verified_service_contract,
    render_service_plan,
    verify_service_ownership,
)

contract = load_verified_service_contract(manifest, target, options,
                                         verifier=pinned_verifier)
rendered = render_service_plan(contract,
                              install_root=installation_root,
                              installation_id=installation_id,
                              modes=["local"])
```

`pinned_verifier`, `manifest`, `target`, `options`, `installation_root` и
`installation_id` предоставляет доверенный bootstrap/SDK. Модуль не принимает
произвольный verifier из HTTP-запроса. Полученный render plan содержит `files`,
`services`, `start_order`, `stop_order`, `required_gates` и флаги readiness.

Каждая служба имеет стабильный ID, зависящий от product, component и случайного
installation ID. Это предотвращает совпадение имён двух независимых установок.
Все start/stop executable разрешаются в конкретный release, включая bundled
runtime вместо системного `python` из PATH.

`verify_service_ownership(expected, observed)` сравнивает ID, backend, hash
конфигурации, executable, start/stop argv, working directory, account,
environment digest и WinSW wrapper pins/SCM image argv. Для systemd дополнительные
drop-ins и Exec hooks запрещены; adapter читает эффективную конфигурацию manager,
а не только основной unit file.
`observed` должен быть прочитан live adapter из SCM/systemd и файлов ОС.
Клиентский JSON не является доказательством ownership. Несовпадение любого
критического поля блокирует mutation; автоматическое присвоение чужой службы
не поддерживается.

## Защита конфигураций

Systemd argv экранирует quotes, backslashes, dollar expansion и percent
specifiers; отдельный `;` сохраняется как аргумент. WorkingDirectory и env
обрабатываются по их собственным правилам. Shell не добавляется. User unit
использует `Type=exec`, `WantedBy=default.target`, `NoNewPrivileges=true`,
`UMask=0077` и ограниченную политику рестартов из манифеста.

WinSW XML создаётся XML serializer. Аргументы экранируются по Windows CRT rules.
Percent expansion в пользовательских путях/argv запрещена: `%NAME%` у WinSW
может подменить путь значением глобальной переменной. Привилегированная
LocalSystem identity по умолчанию не используется; contract задаёт LocalService.
Adapter обязан установить соответствующие ACL и проверить их до старта.

Service configs не содержат значений секретов. Пока credential loader не
реализован, manifest с product secrets отклоняется с
`CAPABILITY_UNSUPPORTED`. Raw stdout/stderr продукта по умолчанию не сохраняется
в service logs; продуктовый redacting log adapter подключается отдельно.

Нельзя считать безопасность глобального окружения доказанной только этими
шаблонами. Live adapter должен ограничить environment и повторно верифицировать
runtime непосредственно перед исполнением.

## Обязательные live-host gates

1. Реальный manager adapter с read-only inventory, точным reviewable plan,
   необходимым scope/consent и durable apply/rollback receipts.
2. Проверка существующих units/SCM image path, config hash, account, ACL и wrapper
   bytes; запрет изменения чужой службы при совпадении только имени.
3. На Linux: работающий user manager, поддержка применённых директив и явная
   политика login/logout/linger. `enable-linger` не включается скрыто.
4. На Windows: signed bootstrap/helper, SCM registration, LocalService ACL,
   WinSW prerequisite/runtime и точное поведение graceful-stop helper.
5. Проверенное завершение всех writers до snapshot. Ошибка stop или оставшийся
   процесс блокирует замену данных, даже если manager вернул успешный exit code.
6. Frozen installer на чистой VM, fresh/update/repair/restore/rollback/uninstall,
   reboot, logout, interruption и отказ от чужих service IDs.

До этих проверок XML/unit не публикуются как доказательство Windows/native
system-service поддержки.

## Primary sources

Проверены 25 октября 2026 года. Документация используется как reference для
контракта; wrapper и его notices в этом модуле не распространяются.

- [WinSW MIT license](https://github.com/winsw/winsw/blob/v3/LICENSE.txt): разрешения и обязательное сохранение copyright/permission notice.
- [WinSW v2 XML configuration](https://winsw.github.io/v2/doc/xmlConfigFile/): stoparguments/stopexecutable, timeout, account, percent expansion и зависимости.
- [WinSW v2 logging](https://winsw.github.io/v2/doc/loggingAndErrorReporting/): `log mode="none"` отключает raw stdout/stderr files.
- [Microsoft ServiceMain](https://learn.microsoft.com/en-us/windows/win32/services/service-servicemain-function): интерфейс SCM и корректные service state transitions.
- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html): ExecStart/ExecStop, quoting, Type=exec и правила restart.
- [systemd.kill](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html): control-group, SIGTERM, SendSIGKILL и оставшиеся процессы.
