# YaPanel Station Max: migration readiness

Дата проверки: 2026-11-12.

## 1. Commit и рабочее дерево

Работа выполнена поверх commit `2165209` (`feat: harden station updater workflow`).
Итоговый commit этой проверки имеет сообщение `test: validate station installer end to end`. Его hash указан в итоговом отчёте. Четыре
каталога `station-install-review*` являются локальными артефактами предыдущих
проверок и в commit не включаются.

Основная среда - Windows 11 и Windows PowerShell 5.1. WSL2 нужен только для
POSIX syntax/path тестов и не является зависимостью установки.

## 2. Покрытие production installer

До этой проверки существовали отдельные тесты PowerShell helper-функций,
Python CLI, journal и bootstrap. Настоящий `install_station_max.ps1 -Apply`
против изолированной станции не запускался.

Добавлен Windows fixture harness. Он запускает настоящий production installer,
настоящий PowerShell 5.1, реальный `update_agent.py`, реальный ZIP preparation,
реальный binary backup pipeline и durable journal. Только транспортная граница
эмулирована:

- SSH и ADB wrappers принимают только пути внутри временного fixture-каталога;
- Android init и rootd lifecycle моделируются без изменения системных файлов;
- HTTP health работает через локальный `ThreadingHTTPServer`;
- rollback выполняет настоящий `UpdateAgent.rollback`;
- реальная станция и её SSH/ADB endpoints не используются.

Production defaults не изменены:

- `SshExecutable=ssh.exe`;
- `AdbExecutable=adb.exe`;
- `HealthUri=http://192.168.31.196:8765`;
- health и supervisor wait имеют штатный лимит 60 попыток.

Тестовые параметры транспорта и локального URI нужны только для изоляции.

## 3. Матрица isolated `-Apply`

Каждый сценарий проверяет итоговые файлы, SHA-256, journal, wrapper intent,
service state, handoff ownership и отсутствие обращения к реальным путям.

| Сценарий | Ожидаемый результат | Проверяемые invariants |
|---|---|---|
| Успешный `0.3.5 -> 0.3.11` | `healthy` | backup, baseline, JSON contract, activation, новый PID, health, journal |
| Baseline conflict | отказ до activation | исходный `start.sh`, версия и managed-файлы не изменены |
| Ошибка после частичной activation | supervisor rollback | legacy files, legacy wrapper и `rolled_back` |
| Ошибка установки wrapper | отказ до activation | исходный wrapper, отсутствие handoff и rollback |
| Power loss до handoff | recovery после повторного supervisor запуска | journal продолжен, rollback идемпотентен, wrapper восстановлен |
| Failed strict health | rollback | новая версия не объявляется успешной, legacy health path используется |
| Успешный rollback и legacy health | `rolled_back` | версия 0.3.5, исходный wrapper, backup сохранён |
| Supervisor недоступен | `fail-closed` | patched wrapper и managed release сохранены для независимого recovery |
| Незавершённый journal | recovery при повторном запуске | unknown state не запускает новую панель |
| Конкурентный rollback | запрещён | PowerShell не меняет файлы, если владелец recovery - supervisor |

Последовательные Web UI updates и второй официальный release дополнительно
проверяются существующим сценарием
`tests/integration/test_transactional_workflow.py::test_web_ui_runs_two_real_cli_releases`.
Он использует реальный HTTP POST `/api/panel/update`, реальный CLI, journal и
bootstrap между двумя версиями.

## 4. Regression

Целевые команды:

```powershell
python.exe -m pytest -q tests/integration/test_station_installer_apply.py
python.exe -m pytest -q
```

POSIX-проверки:

```bash
find . -type f -name '*.sh' -not -path './.git/*' -exec sh -n {} +
python3 -m py_compile update_agent.py updater_bootstrap.py
```

Фактические результаты:

- production fixture: `8 passed` за 585.38 s;
- полный Python regression: `977 passed, 48 skipped, 56 subtests passed` за
  800.93 s;
- installer helper и transactional/Web UI targeted regression: `24 passed`;
- WSL2 POSIX syntax и Python compile: `POSIX_OK`;
- skipped-тесты полного regression не относятся к production installer flow;
  они оставлены существующим проектным набором.

## 5. Live read-only preflight

Проверенный read-only snapshot станции:

- Android 9, build `PPR1.180610.011.5000000921`;
- model `yandexstation_2`;
- `VERSION=0.3.5`;
- исходный `start.sh` SHA-256:
  `61d13d10a194dc31d1225fc2c094f9ecadf8b4c2655e7139ced1eaa9c6d44f36`;
- единственный корректный YaPanel PID `10011`, executable Python и cwd
  `/data/yapanel`, порт 8765;
- `init.svc.yapanel=rootd=adbguard=absshd=running`;
- ADB `192.168.31.196:5555` отвечает, обычная shell-сессия имеет
  `uid=2000(shell)`, как и ожидалось;
- SELinux `Permissive`, SSH root transport работает через `station-max`;
- локальные approved hashes:
  - `server.py`: `dd9411f9d60c30371fee282ad3d74f0d67a2e37266ded2cd48ad9293bd0d9e74`;
  - `max_profile.py`: `ac5e1ca84e3c9f10c59819164d7a8a6ee8c38a2f0e9df860f3cccd55fc9823e4`;
  - `static/app.js`: `b7a797f1a53a9439689381d2c46c314abc139267e8760517b8a78410327c91de`;
- предыдущая транзакция `20261111-162240-a47c353e` имеет `abandoned` и
  `close_reason=baseline_conflict`;
- `/odm` read-only, hosts-ota/hosts-full mounts сохранены;
- read-only mounts snapshot SHA-256:
  `1aa0cc71009503556f2c92ebe6c244002f0dcdfe001526562e94841b6d59a636`;
- свободное место `/data` около 7.6 GB.

Перед реальной установкой эти значения нужно получить повторно. Любое
отклонение означает остановку и дополнительную диагностику.

## 6. Backup

Проверенный локальный backup:

```text
station-install-review4-20261111/backups/yapanel-station-0.3.5-20261111-232500.tar.gz
SHA-256: 0922e162a70b1f90710f3c26c73a2db5925da51b6487cd2ce4d2d2c4ff8f716a
```

Проверены версия `0.3.5`, исходный SHA-256 `start.sh`, наличие `VERSION`,
`start.sh`, `server.py`, `yapanel-state`, binary tar.gz extraction и sidecar
manifest. Перед установкой archive должен быть сопоставлен с повторным live
snapshot. Backup не является доказательством корректности уже изменённых
пользовательских форматов данных.

Дополнительная побайтная проверка архива подтвердила hashes трёх локально
изменённых файлов, `VERSION` и всех шести lineage artifacts. В архиве 532
entries и один link entry; target остаётся внутри `yapanel` scope.

## 7. Независимое SSH-восстановление

Процедура не зависит от Web UI и bootstrap:

1. На Windows проверить SHA-256 архива и безопасный список tar entries.
2. Подключиться `ssh -o BatchMode=yes station-max` и только read-only проверить
   `VERSION`, journal и текущую фазу.
3. Передать архив бинарным SSH-транспортом, без текстового pipeline.
4. На станции распаковать только `yapanel/` и разрешённую часть
   `yapanel-state/` во временный каталог под `/data`.
5. Проверить SHA-256, symlink/hard-link targets, владельцев и права.
6. Переключить только файлы панели атомарными `mv` внутри `/data`; не менять
   `/odm`, hosts, rootd, adbguard, absshd, SSH или ADB.
7. Проверить `/system/bin/sh -n start.sh`, `VERSION=0.3.5`, HTTP API и journal.
8. При несоответствии остановиться и сохранить диагностику.

Процедура испытана только на isolated fixture. На реальной станции до отдельного
разрешения не выполняется.

## 8. Команды первой установки

Рабочий каталог:

```powershell
Set-Location C:\Projects\yapanel-web_max
git rev-parse --short HEAD
```

Официальный asset:

```text
https://github.com/Timobzdecc/yapanel-web_max/releases/download/v0.3.11/windows-arm64.zip
SHA-256: e528d1f9db31bbcb8e19b41341941f0defc605dab3d2ba429cf4ec928a176c1e
```

Финальный preflight должен быть выполнен dry-run командой:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\install_station_max.ps1 `
  -SshAlias station-max `
  -AdbSerial 192.168.31.196:5555 `
  -AssetPath .\station-install-0.3.11\yapanel-web-0.3.11-windows-arm64.zip `
  -WorkDirectory .\station-install-0.3.11-live
```

Только после отдельного разрешения на установку:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\install_station_max.ps1 `
  -Apply `
  -SshAlias station-max `
  -AdbSerial 192.168.31.196:5555 `
  -AssetPath .\station-install-0.3.11\yapanel-web-0.3.11-windows-arm64.zip `
  -WorkDirectory .\station-install-0.3.11-live
```

При любом preflight, backup, JSON, journal, handoff или health failure команда
останавливается. После handoff rollback принадлежит supervisor; direct rollback
PowerShell не выполняет.

## 9. Post-install smoke test

| Функция | Объективный критерий |
|---|---|
| Web UI и настройки | UI открывается, пароль администратора восстановлен, настройки сохранены |
| HTTP API | `/api/panel/health` ready=true, version=0.3.11, critical_errors=0 |
| Алиса и голосовое управление | голосовой запрос получает ожидаемый ответ без повторного запуска процесса |
| Android TTS | `/api/say` воспроизводит фразу через Android TTS |
| StationAudio | локальный audio path воспроизводит тестовый звук |
| DSP | preset сохраняется и применяется после повторного чтения |
| Bluetooth | известное устройство подключается и звук идёт в выбранный output |
| Zigbee | существующее Zigbee-устройство отвечает без изменения pairing data |
| SSH и ADB | `ssh -o BatchMode=yes station-max`, `adb shell id` успешны |
| rootd/adbguard/absshd | init services остаются `running` |
| OTA protection | hosts mounts и `/odm` совпадают с preflight snapshot |

Аппаратные пункты требуют ручной проверки на станции; HTTP health их не
подтверждает.

## 10. Остаточные риски

### Blockers

- любой mismatch live baseline или backup;
- отсутствие полного isolated production-harness regression;
- неизвестная модификация executable-кода;
- неизвестное durable journal state;
- failure strict health или supervisor recovery;
- отклонение rootd, adbguard, absshd, hosts или `/odm` invariants.
- невосстановленный пароль администратора является blocker для завершения
  работ с точки зрения безопасности, хотя не блокирует сам контролируемый
  локальный handoff при сохранении доступа только из LAN;

### Допустимые ограничения

- 0.3.11 имеет beta-статус;
- Android init и rootd lifecycle в production-harness эмулируются на Windows;
- аппаратные функции требуют ручного smoke test;
- rollback не отменяет необратимую миграцию пользовательского формата данных;
- отключённый пароль Web UI допустим только временно и должен быть восстановлен
  после технических работ.

## 11. Решение

Техническое решение: `GO` к контролируемой первой миграции 0.3.5 -> 0.3.11.

Основания:

- production `-Apply` fixture: 8/8 сценариев passed;
- полный Python regression: 977 passed, 48 skipped, 56 subtests passed;
- POSIX syntax и Python compile прошли;
- live read-only preflight и backup соответствуют подтверждённому snapshot;
- blockers из раздела 10 не обнаружены.

Это не означает завершение security hardening. Пароль администратора Web UI
обязательно восстановить после технических работ; до этого процедуру нельзя
считать полностью завершённой с точки зрения безопасности.

Реальная установка всё равно выполняется только после отдельного разрешения
пользователя, без reboot станции и с ручным smoke test аппаратных функций.
