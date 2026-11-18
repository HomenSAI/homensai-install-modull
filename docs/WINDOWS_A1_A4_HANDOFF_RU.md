# Передача Windows-очереди A1–A4

Разработка остановлена по просьбе владельца 18.11.2026. Текущий scope —
Windows native до A4. Очередь B для Docker Desktop на Windows и этап №2
остаются в [плане](WINDOWS_INSTALLER_PLAN_RU.md) и ждут отдельной команды.

В новом чате достаточно открыть рабочую копию проекта и написать
**«продолжить»**. [Точка входа](../AI_START_HERE.md) укажет на этот handoff
через `active_handoff` в [work-state](work-state.json). Здесь есть команды,
ветки и порядок проверки. Локальная папка прежней сессии для восстановления
не требуется.

## Что сохранено в Git

| Контрольная точка | Commit | Tree | Состояние |
| --- | --- | --- | --- |
| A1 · CP-20261118T154554Z-35194ac755e2 | `7ee832743d33a36f3d96e65a3f0233b73329d6c3` | `838404f03e3b5c80e3d419799e732b6387858128` | Подписанный fresh и отказ при подмене на Windows 10 DEV. |
| A2 · CP-20261118T154901Z-2f4c7d5556cf | `27cb1682e42aca8acdd9f7b96a84d10be9f440cc` | `dddc60144234779ca5b8d5aa6c6c918111a1fded` | DEV start/stop/job, локальные тесты прошли. Windows CI FAIL. |
| A3 WIP · CP-20261118T160007Z-f72f4136d4da | `6875a7b84d0b83749a3497593555690869cffd51` | `a9aedb22b51dc7a135da4709992811ff78393a90` | Черновик без прогона. |

Основная рабочая ветка — `codex/universal-installer-20261025`. A1 и A2 в её
истории. Черновик A3 находится в отдельной ветке
`codex/universal-installer-windows-a3-wip-20261118`. Все три commit/tree были
сверены отдельной загрузкой из GitHub. A3 не входит в историю рабочей ветки.
На исходном компьютере его отдельная рабочая копия находится в
`.local-state/agents/windows-a3-transfer-20261118`.

[Рабочая ветка](https://github.com/HomenSAI/homensai-install-modull/tree/codex/universal-installer-20261025) · [Черновик A3](https://github.com/HomenSAI/homensai-install-modull/tree/codex/universal-installer-windows-a3-wip-20261118)

Для восстановления в новом checkout получить обе ветки и сверить сохранённый
черновик. Эти команды читают Git и загружают объекты без переключения веток.

```powershell
git status --short --branch
git fetch --no-tags origin refs/heads/codex/universal-installer-20261025 refs/heads/codex/universal-installer-windows-a3-wip-20261118
git rev-parse '6875a7b84d0b83749a3497593555690869cffd51^{tree}'
git diff 27cb1682e42aca8acdd9f7b96a84d10be9f440cc 6875a7b84d0b83749a3497593555690869cffd51 -- homensai_install/adapters.py homensai_install/windows_user.py
```

## Проверки и ближайший блокер

- **A1 на Windows 10 Home 19045 / Python 3.14.3.** Из подписанного дерева
  установлен и запущен вложенный CPython, проверен собственный loopback HTTP,
  повтор submit сохранил ID, подмена пакета отвергнута до создания корня.
  Windows 7 PASS, Linux Docker 36 PASS и 2 Windows-only skip. В GitHub run
  `44339799432` новое Windows integration-задание не прошло сравнение ACL по
  сокращённым именам SID.
- **A2 в той же среде.** Windows 65 PASS и 4 skip, Linux Docker 37 PASS.
  Проверены отказ для общего каталога, владение Windows job, повторный start
  и остановка всего job из другого экземпляра SDK. Чтение ACL переведено на
  сравнение фактических SID. В GitHub run `44341941117`, job `134934176233`,
  portable-тесты прошли, а Windows integration получило 2 FAIL и 1 PASS.
  Точный отказ — `check_private_path(self.path)` при повторном открытии
  `operations.sqlite3` в `homensai_install/api/journal.py`. SID владельца нового
  файла отличается от SID пользователя процесса, наружу возвращается
  `STATE_STORAGE_UNAVAILABLE`. Отличие политики владельца в токене раннера —
  гипотеза. Нужна безопасная диагностика SID owner и token owner в одноразовой
  fixture. Защиту журнала и проверки ACL сохранить. На момент остановки Linux
  source/integration и Linux installed в этом run ещё выполнялись.

[Квитанция A1](../packaging/acceptance-evidence/windows-native-a1-20261118.json) · [Квитанция A2](../packaging/acceptance-evidence/windows-native-a2-20261118.json) · [GitHub CI A2](https://github.com/HomenSAI/homensai-install-modull/actions/runs/44341941117)

## Что начато в A3 и что делать дальше

1. По команде владельца «продолжить» сверить рабочую
   feature-ветку и результат CI A2. Устранить Windows-отказ журнала и получить
   повторный Windows integration PASS. Локальный DEV PASS сохраняет свой scope.
2. В отдельной рабочей копии сравнить A3 WIP с A2 и перенести пригодные
   изменения через обычный Git review. В `adapters.py` начаты Windows-проверки
   исполняемого файла и сбор ограниченного ответа hook. В `windows_user.py`
   начаты дескриптор каталогов и запуск hook с перенаправлением потоков внутри
   отдельного job. Черновик сохранён без тестового запуска. Допуск Windows
   `ProductAdapter` в нативном движке пока закрыт.
3. Первые проверки A3 — реальный подписанный hook Windows, bounded stdout/stderr,
   timeout и обработка неизвестного эффекта. Затем перенести проверки файлов
   и архивов. Обратить внимание на POSIX-зависимости в `migration_snapshot.py`,
   `retained_adapter.py`, `uninstall_receipts.py`, `recovery_replacement.py`,
   `code_maintenance.py` и проверке журнала в `engine.py`. Проверить полный
   цикл v1→v2→v3, restore/rollback, repair/reinstall и сохранность данных после
   удаления. Включать capabilities только вслед за проверенным сценарием.
4. После принятого A3 перейти к A4. Исходники сборщика Windows и `Setup.exe`
   ещё не подготовлены. На локальном хосте `makensis` не найден, чистая
   Windows 11 не подключена. При добавлении инструмента сначала сверить
   точную версию, источник и лицензию. Сборка DEV и целевая Win11-приёмка
   записываются отдельно.

По окончании каждого шага сохранить отчёт, commit и обычный push в рабочую
feature-ветку, затем независимо сверить ref/tree. Ключи, локальные среды,
личные данные и бинарные сборки остаются вне Git. На хосте локальные тесты
завершены. `production_ready=false`, G0–G5 открыты.
