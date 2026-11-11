# Безопасное обновление панели

## Состав решения

Обновление выполняется внешним `update_agent.py`, который не импортирует
`server.py` во время staging, активации и rollback. Архив скачивается в
отдельный каталог состояния, проверяется по SHA-256, распаковывается в staging
и проходит preflight до создания backup.

На Station Max updater состоит из четырёх контролируемых файлов:

- `update_agent.py` - manifest, preflight, backup, staged activation и rollback;
- `updater_bootstrap.py` - стабильный дочерний процесс Android init, recovery
  после сбоя питания, health check и кооперативное управление дочерним
  `server.py`;
- `station_overlay.py` - повторяемое version-aware расширение официального
  `server.py` и `static/app.js`;
- `panel_update_integration.py` - внешний bridge, который возвращает updater в
  каждый чистый upstream `server.py` после первой миграции.

`server.py` и `static/app.js` содержат только интеграционный hook и UI статуса.
`start.sh` остаётся станционным launch wrapper и не входит в manifest релиза.
Настройки, секреты, DSP-пресеты, логи и каталог `state` не перезаписываются.
Каталоги `docs`, `tools` и прочие неуправляемые каталоги автоматически не
удаляются.

## Поведение последующих релизов

Официальный ZIP может не содержать четыре локальных updater-файла. Это
ожидаемый сценарий: они не считаются release-managed файлами и остаются в
рабочем каталоге. Для нового `server.py` overlay добавляет прямой импорт
`panel_update_integration.py`; для нового `static/app.js` добавляется polling
статуса. Если структура upstream изменилась и hook нельзя установить
однозначно, staging блокируется до активации.

Таким образом, следующий совместимый релиз не удаляет updater и не требует
повторного ручного переноса. Неизвестный конфликт остаётся `Block and report`.
Тест `test_sequential_upstream_releases_keep_external_updater_integration`
проверяет две последовательные чистые upstream-версии без updater-файлов.

Для принятия нового `server.py` действует отдельный строгий контракт: после
overlay должны присутствовать и импорт с вызовом
`_install_station_updater(PanelUpdater)`, и supervisor-протокол с
`PANEL_SUPERVISOR_MODE`, `RESTART_HOOK` и кодом завершения `75`. Если upstream
уже содержит `agent.stage`, но не предоставляет этот проверяемый протокол и
его нельзя однозначно дополнить по известному шаблону, staging блокируется до
активации. Один только `SERVER_MARKER` больше не считается доказательством
сохранения внешнего updater и restart supervisor.

После первого SIGTERM rollback принадлежит только `updater_bootstrap.py`.
PowerShell не восстанавливает файлы и `start.sh`, если handoff уже начат или
служба находится в состоянии `running`. При исчезновении supervisor процедура
фиксирует ошибку и оставляет transaction для восстановления через bootstrap или
отдельную SSH-процедуру. Это предотвращает запись поверх процесса, который мог
быть автоматически запущен Android init.

При rollback backup восстанавливается в два прохода: сначала обычные файлы и
symlink, затем hard links. `panel/...` и `state/data/...` linkname приводятся к
своему namespace; ссылки с traversal, между namespace или на symlink запрещены.

## Preflight и backup

До записи рабочей панели проверяются:

1. SHA-256 загруженного архива и безопасные пути ZIP;
2. обязательные модули, синтаксис Python и локальные импорты;
3. наличие Python 3.9+, права записи и свободное место;
4. baseline и локальные hash-конфликты;
5. наличие внешнего updater при наличии station overlay.

Для исторических generated artifacts используется только явная lineage-база
`tools/station-max-generated-lineage.json`. Она разрешает текущие официальные
бинарники `v0.3.4` по точным hashes и provenance. Неизвестные или изменённые
исполняемые файлы не принимаются автоматически и не исключаются из manifest.

Старая незапущенная транзакция может быть переведена из `staged` в терминальное
состояние `abandoned`. Операция сохраняет archive, staging, manifest и journal,
не выполняет rollback и не запускает процессы. Для активных состояний операция
запрещена.

Перед активацией создаётся уникальный backup с исходниками панели, правами,
метаданными, `state/data`, а также снимками `baseline.json` и `status.json`.
Существующие backup не перезаписываются. Journal записывается атомарно после
каждого файла.

Каждая durable-фаза после усиления реализации фиксирует содержимое файла и
родительский каталог. Для backup создаётся маркер `backup.complete`; отсутствие
маркера означает неполный backup и запрещает rollback. До первого изменения
рабочего файла durable journal уже содержит состояние `activating`. После записи
всех файлов он фиксирует `activated_pending_health`, поэтому потеря питания
между активацией и записью `status.json` всё равно приводит к strict health check
после следующего запуска.

В первой миграции сначала создаётся durable `wrapper-intent`, затем заранее
проверенный wrapper атомарно заменяет исходный `start.sh`, и только после этого
запускается `update_agent apply`. Если apply не начал активацию, установщик
возвращает исходный wrapper без остановки работающей 0.3.5. Если journal уже
перешёл в recovery-фазу, wrapper сохраняется, а восстановление выполняет только
внешний bootstrap.

## Health check и rollback

### Durable state contract

| Journal | Владелец | Поведение после ошибки или power loss |
|---|---|---|
| staged | UpdateAgent | рабочие файлы не изменялись; разрешено закрытие staged |
| backup | UpdateAgent/bootstrap | восстановить backup до запуска панели |
| activating | bootstrap | продолжить rollback по полному backup |
| activated_pending_health | bootstrap | запустить новую версию и выполнить strict health |
| healthy | bootstrap | повторить strict health и восстановить status projection |
| rollback_started | bootstrap/UpdateAgent | повторить полный rollback не более одного раза |
| rolled_back | bootstrap | проверить legacy health и завершить status |
| abandoned | оператор | только закрытие незапущенной staged-транзакции |

Все записи journal выполняются атомарно с fsync файла и каталога. При
неизвестном состоянии запуск панели запрещается. status.json не может
перевести journal в более безопасную фазу и используется только для UI.

Journal является источником истины для readiness. Если journal содержит
`activated_pending_health`, bootstrap выполняет health check даже при устаревшем
`status.json`. Сначала вызывается `mark_healthy`, затем очищается wrapper intent
и фиксируется `status.json=healthy`.

Если journal уже содержит `healthy`, а status остался pending после сбоя питания,
bootstrap повторно проверяет новую версию и только после этого завершает status.
Если journal содержит `rolled_back`, bootstrap повторяет legacy health и только
после этого фиксирует завершённое восстановление. Неизвестное или множественное
активное состояние journal переводит bootstrap в fail-closed.

После восстановления старой версии status сначала получает
`rollback_pending_health`. Состояние `rolled_back` фиксируется только после
успешной проверки legacy endpoint `/api/status` и `/api/panel/version`.

Состояние `healthy` выставляется только bootstrap после ответа
`GET /api/panel/health` с одновременными условиями:

- HTTP 200;
- корректный JSON;
- `ready == true` после завершения инициализации панели и bind HTTP-сервера;
- точное совпадение `version` с активируемой версией;
- `critical_errors == 0`.

Ошибки инициализации до `ready` завершают процесс. Счётчик критических ошибок
предоставляется через `Handler.critical_errors`; автоматический check не заменяет
ручную проверку аппаратных функций, аудио, ассистента и privacy/adbguard.

При незавершённом journal в состояниях `backup` или `activating` после питания
bootstrap сначала выполняет rollback. Для `activated_pending_health` он запускает новую
версию и проверяет её health даже если `status.json` не успел записаться. После
успешной проверки journal и status получают состояние `healthy`. При
необходимости rollback bootstrap записывает `rolled_back`, запускает предыдущую
версию и проверяет её health.
При неудаче rollback состояние становится `rollback_failed`, а bootstrap
останавливается в удерживающем режиме. Если питание прервет сам rollback,
следующий запуск повторяет полный rollback поверх частично восстановленных
файлов не более одного раза. После исчерпания двух автоматических попыток
операция остаётся в `rollback_started`, status переводится в
`rollback_failed`, и требуется ручная SSH-процедура. Это позволяет продолжить
идемпотентное восстановление после power loss, но не создаёт бесконечный цикл.

Bootstrap не использует принудительное завершение дочернего процесса. Если
`server.py` не завершился после graceful stop, rollback блокируется до ручного
восстановления, поскольку запись файлов при еще работающем процессе может
повредить данные или оставить порт занятым. Быстрые аварийные завершения
считаются и после трех последовательных сбоев переводят bootstrap в
`rollback_failed`, не создавая бесконечный цикл перезапусков Android init.

## Источник ZIP и доверие

Для Android используется `windows-arm64.zip`, потому что именно release asset
содержит полный набор Python-модулей, `api`, `static`, `device` и собранные
payload, тогда как исходный ZIP Git tag содержит только исходное дерево и не
является готовым release package. При применении manifest исключает `runtime`
и Windows-исполняемые файлы `.bat`, `.cmd`, `.ps1`, `.exe`, `.dll`, `.pyd`; они
не попадают в рабочую Android-установку.

Ожидаемый digest берётся из поля `assets[].digest` GitHub Releases API и
сверяется с байтами скачанного asset до распаковки. HTTPS и digest защищают
канал и целостность загрузки, но digest API не является независимой подписью
издателя. Поэтому для beta-релизов нужно отдельно сверять release tag,
название asset и digest на странице GitHub; подписанный release artifact в
upstream пока не публикуется.

## Первый bootstrap

Первоначальная миграция выполняется только после отдельного разрешения. До неё
создаётся baseline по upstream `v0.3.5` с тремя approved hash локальных файлов:

```text
python tools/make_updater_baseline.py v0.3.5 updater-baseline.json tools/station-max-v0.3.5-overrides.json

Для Station Max к этой команде добавляется строгая lineage-база generated
artifacts:

```powershell
python tools/make_updater_baseline.py v0.3.5 updater-baseline.json tools/station-max-v0.3.5-overrides.json tools/station-max-generated-lineage.json
```
```

Затем из проверенного официального `windows-arm64.zip` собирается локальный
`initial-release`, куда добавляются только локальные updater-файлы и проверенный
overlay:

```text
python tools/prepare_station_release.py --asset yapanel-web-0.3.11-windows-arm64.zip --output yapanel-station-0.3.11.zip --sha256 e528d1f9db31bbcb8e19b41341941f0defc605dab3d2ba429cf4ec928a176c1e
```

Четыре файла `update_agent.py`, `station_overlay.py`, `updater_bootstrap.py` и
`panel_update_integration.py` дополнительно сохраняются в
`/data/yapanel-state/updater`. Bootstrap импортирует `update_agent.py` именно
оттуда, поэтому recovery не зависит от повреждённого рабочего каталога.

В рабочий каталог станции не копируется upstream `start.sh`. Согласованный
внешний hook должен сохранить текущие `--device local`, `--host 0.0.0.0`,
`--port 8765`, `PANEL_ALLOW_NETWORKS`, путь Python и CA и запускать
`updater_bootstrap.py` вместо прямого запуска `server.py`.

После bootstrap выполняется read-only health check и только затем фиксируется
активация. `/odm`, `/system/etc/hosts`, `adbguard`, `rootd`, `absshd` и Android
init этим кодом не изменяются.

## Единый установочный сценарий

Для автоматической процедуры используется
`tools/install_station_max.ps1`. Без `-Apply` сценарий выполняет только
preflight: SSH, ADB, snapshot, baseline, release digest, backup, скачивание
`start.sh` через бинарный канал SSH и подготовку patch во временном каталоге.

Изменяющий режим запускается только отдельной командой:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\install_station_max.ps1 -Apply
```

Сценарий сам создаёт baseline, скачивает и проверяет release asset, собирает
initial-release, создаёт backup и подготавливает внешний updater. Операция
`-Apply` дополнительно требует подтвержденного lifecycle handoff. Пока текущий
процесс 0.3.5 не переведен под стабильный bootstrap, сценарий не должен
пытаться вызывать `setprop ctl.stop/start`: root UID в shell-контексте этого не
гарантирует, а уже зафиксирован отказ `ctl.stop`.

`tools/patch_station_start.py` отказывается работать при неожиданном SHA-256,
проверяет сохранение всех текущих параметров и является idempotent.

На Station Max SSH может иметь UID 0, но оставаться в SELinux-контексте
`u:r:shell:s0`. Очередь `rootd` подтверждена для диагностических команд, но
сама по себе не доказала право управления Android init. Поэтому capability
probe не выполняет `ctl.*`, а `-Apply` блокируется до отдельного минимального
lifecycle-теста. Рабочий `start.sh` и release-файлы станции до этого теста не
изменяются.

## Backup на Windows

До первой миграции backup создаётся без Web UI:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\backup_station_max.ps1
```

Скрипт читает `/data/yapanel` и `/data/yapanel-state` через SSH, проверяет
`VERSION=0.3.5`, наличие `start.sh`, создаёт tar.gz, проверяет список архива и
сохраняет SHA-256 и manifest. Архив может содержать секреты, поэтому он
остаётся локальным и получает ограниченный ACL.

## Аварийное восстановление через SSH

При проблеме с Web UI сначала читаем journal, не останавливая системные
сервисы:

```powershell
ssh -o BatchMode=yes station-max "cat /data/yapanel-state/updater/status.json"
ssh -o BatchMode=yes station-max "find /data/yapanel-state/updater/transactions -name transaction.json -type f"
```

Остановку и запуск через `setprop ctl.*` из SSH выполнять нельзя: точная
причина прежнего отказа еще не подтверждена, а повтор управляющей команды не
разрешен. До завершения lifecycle-теста используется только recovery внешнего
updater и ручная процедура без управления Android init.

Перед любой записью проверяем, что Android init не перезапустит панель
параллельно с восстановлением. Читаем состояние init и список процессов:

    ssh -o BatchMode=yes station-max "getprop init.svc.yapanel; ps -A | grep server.py || true"

Если служба находится в состоянии running или найден процесс панели, запись
запрещена: сначала требуется отдельное согласование безопасного graceful
handoff. Не используются ctl.stop, ctl.start, SIGKILL, remount или reboot.

Только после подтверждения отсутствия активного процесса восстанавливаем
незавершённую активацию внешним updater:

```powershell
ssh -o BatchMode=yes station-max "/data/python/bin/python3 /data/yapanel-state/updater/update_agent.py recover --root /data/yapanel --state /data/yapanel-state"
```

Для конкретной транзакции используем её идентификатор:

```powershell
ssh -o BatchMode=yes station-max "/data/python/bin/python3 /data/yapanel-state/updater/update_agent.py rollback <TRANSACTION_ID> --root /data/yapanel --state /data/yapanel-state"
```

После запуска проверяем `/api/panel/health`, версию, порт 8765, SSH, ADB,
`rootd`, `adbguard`, `absshd` и mountinfo hosts. Если внешний updater также
повреждён, используется локальный tar.gz из Windows backup и отдельная SSH
процедура восстановления файлов.

## Локальный сквозной harness

`tests/integration/test_transactional_workflow.py` проверяет реальный workflow
временной станции: Python CLI, durable journal, backup, дочерний HTTP-сервер,
bootstrap, legacy health и повторный релиз. PowerShell 5.1 запускает
`tools/run_local_update_harness.ps1`, который вызывает настоящий CLI и проверяет
ответ через `station_apply_contract.ps1`. Harness не использует SSH/ADB к
реальной станции и не изменяет её.
