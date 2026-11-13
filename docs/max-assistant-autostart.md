# Автозапуск ассистента Station Max без watchdog и BusyBox

## Проблема

На некоторых сборках Android 9 Station Max отсутствуют
/odm/bin/adbd_watchdog.sh и start-stop-daemon. Наличие boot_hook.sh
само по себе не означает, что его кто-то выполнит после перезагрузки.
При раннем запуске панели /data/quasar/webpanel/assistant может
появиться позже самой панели.

## Решение

Когда Yapanel работает непосредственно на устройстве Android и
выбран профиль max, фоновый поток проверяет наличие конфигурации
и исполняемого assistantd каждые пять секунд (до 36 попыток).
На устройстве запускается отдельный процесс через subprocess.Popen
с новой сессией. PID-файл в /tmp публикует сам Python-ассистент
после получения эксклюзивного POSIX flock на отдельном .lock-файле.
Boot hook запускает помощника без параметра `-m` у `start-stop-daemon`:
PID-файл не создаётся и не удаляется снаружи демона. Если `/odm/bin/start-stop-daemon`
недоступен, для запуска используется фоновый `/system/bin/sh`. Обе ветви запуска
подчиняются одному `flock` внутри `assistant_max.py`.
Второй процесс, запущенный параллельно boot_hook.sh или Yapanel,
не запускает цикл обработки команд и не перезаписывает PID.
При падении процесса lock освобождается ядром, оставшийся .lock-файл
не блокирует следующий запуск.

В /data/yapanel/assistant-autostart.log пишутся только служебные
события: PANEL_STARTED, WORKER_STARTED, WAIT_FILES,
START_ATTEMPT, START_OK, START_ERROR, RETRIES_EXHAUSTED.
Лог не содержит конфигурацию, API-ключи, вопросы или URL прокси.

При запуске панели на Windows/Linux/macOS вне станции
механизм Android-автозапуска не включается, прежнее управление
через ADB сохраняется.

Профиль Station Max проверяет наличие реального Android caller для
`/data/quasar/webpanel/boot_hook.sh`. Если заявленный watchdog отсутствует
или не содержит активного вызова хука, `/api/hook` сообщает
`possible=false`, а repair блокируется до записи файла. Одного наличия
`boot_hook.sh` недостаточно: это предотвращает ложное подтверждение
автозапуска после перезагрузки.

Если все активные hook-users покрыты штатным worker-ом панели, предупреждение
не показывается. Для новых функций без такого fallback предупреждение остаётся
и требует отдельного подтверждённого boot caller.

Запуск ассистента через кнопку в локальной Android-панели тоже
использует прямой процесс, без зависимости от start-stop-daemon.

## Тумблер «Запускать после перезагрузки»

В карточке «Помощник» есть отдельный переключатель. Он управляет только
автозапуском процесса и не связан с «Отвечать на фразу».

- Флаг: `/data/quasar/webpanel/assistant_no_autostart`.
- Флага нет — автозапуск включён (так остаётся у существующих установок).
- Флаг есть — boot hook и autostart-worker не запускают демона.
- Выключение не останавливает уже запущенный процесс; кнопки
  «Запустить»/«Остановить демон» продолжают работать.
- Включение не запускает процесс немедленно — только разрешает
  автозапуск при следующей загрузке.

## Проверка

python -m unittest tests.test_max_assistant_autostart -v

Тесты изолированы от реальной станции. Отдельно необходимо
проверить штатную установку обновления, фактический автозапуск
после полного reboot Android и работу кнопок перезапуска/остановки.

На отдельной тестовой YNDX-0008 с прошивкой 5000000921 ранее проверялись установка,
`start`/`daemon stop`, reboot и автозапуск через worker. Это не является доказательством
cold boot для текущей станции. Для текущей станции read-only проверка подтверждает
только запущенный worker, его prerequisites и записи `START_OK`. Тумблер автозапуска проверен отдельно:
OFF → reboot → демон не запущен (`AUTOSTART_DISABLED`); ON → reboot → запущен (`START_OK`).
Отдельно найден и исправлен CRLF-баг: `device/assistant/assistantd` должен иметь LF, иначе shebang
`#!/system/bin/sh\r` даёт `ENOENT` и ассистент не стартует.

Для точечного обновления уже установленной 0.3.11 используется
`tools/apply_station_file_patch.ps1`. Он проверяет `healthy` journal, создаёт полный
локальный backup, передаёт только проверенный Python-файл через base64 SSH-транспорт
и запускает `update_agent.py apply-patch`. Такой patch создаёт обычную durable
transaction, сохраняет полный baseline и выполняет атомарную замену managed-файла.
Перезапуск выполняется только отдельным `-Restart`: supervisor остаётся владельцем
health check и rollback, а helper может отправить лишь graceful SIGTERM подтверждённому
PID процесса панели. При неизвестном или активном процессе прямой rollback блокируется.

Применение выполняется двумя отдельными шагами после отдельного разрешения:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\apply_station_file_patch.ps1 `
  -Apply

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\apply_station_file_patch.ps1 `
  -Restart -TransactionId <transaction-id-из-вывода-Apply>
```

При failed health сначала ожидается rollback supervisor. Ручной откат файлов
допустим только при отсутствии активной службы и процесса панели:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\apply_station_file_patch.ps1 `
  -Rollback -TransactionId <transaction-id>
```

Rollback восстанавливает данные из durable backup transaction, сохраняет metadata
и не выполняет запуск панели или команды Android init.

## Ограничения

- Это исправление не устанавливает ассистента автоматически.
- Бинарник assistantd и файлы приложения должны быть установлены отдельно.
- Права доступа к /data/quasar должны позволять запуск от пользователя Yapanel.
- Для HTTPS по-прежнему требуется рабочее доверенное CA-хранилище Python.
