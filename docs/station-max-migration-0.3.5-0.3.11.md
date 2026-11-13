# Миграция Station Max: 0.3.5 -> 0.3.11

Сравнение выполнено между upstream `v0.3.5`, read-only копией станции от
11.11.2026 и upstream `v0.3.11`. Станция в ходе разработки не изменялась.

## Трёхсторонний результат

| Файл | upstream 0.3.5 | Установлено на станции | upstream 0.3.11 | Решение |
|---|---|---|---|---|
| `server.py` | `f2e8fd534bb7c8245ae7fe10509db656bef89901f4c69160868034bc24ebf205` | `dd9411f9d60c30371fee282ad3d74f0d67a2e37266ded2cd48ad9293bd0d9e74` | `6076708994bf4820d97490f7b79f659e8eec02641013ea8b4c0fc6af644c1dd9` | Автозапуск ассистента уже upstream. Сохраняется только PATH-overlay для `/data/yapanel/bin`; updater выносится во внешний bridge. |
| `max_profile.py` | `c487615a2afbf6c914e855a8363cd35b944a243b80dacf9f199f494966f97229` | `ac5e1ca84e3c9f10c59819164d7a8a6ee8c38a2f0e9df860f3cccd55fc9823e4` | `49e7ea5e6d132abb9b91134f85d7ebd5d49159dde38abf02737cbe486018b098` | Android TTS, `/api/say` и StationAudio уже включены в upstream; старую реализацию не переносим. `StationAudio.dex` совпал byte-to-byte. |
| `static/app.js` | `8572db455db95400204e0ba088c2830dde5ada5531eddc8dff0fae99fb150ed3` | `b7a797f1a53a9439689381d2c46c314abc139267e8760517b8a78410327c91de` | `479b1963e31865ef418eb0c2a094b412d6aea87f6ae18425d0a8286979b644a7` | Privacy/adbguard UI уже upstream; добавляется только статус staged health/rollback. |
| `start.sh` | `38c8b7d022956ba3f6f6bd84d92028ce05d05322ffc225b9d19ad73362ccd416` | `61d13d10a194dc31d1225fc2c094f9ecadf8b4c2655e7139ced1eaa9c6d44f36` | `38c8b7d022956ba3f6f6bd84d92028ce05d05322ffc225b9d19ad73362ccd416` | Локальный launch wrapper сохраняется целиком и не является managed release-файлом. |

В установленной `0.3.5` отсутствуют `max_privacy.py` и `assistant_socks.py`.
Они поставляются полным release asset `0.3.11`.

## Первая установка

Официальный asset `windows-arm64.zip` имеет digest:

```text
e528d1f9db31bbcb8e19b41341941f0defc605dab3d2ba429cf4ec928a176c1e
```

Он используется как проверенный источник upstream-файлов. До применения на
станции собирается `initial-release` командой из `docs/panel-updater.md`. В него
включаются `server.py`, `static/app.js` и четыре локальных updater-компонента.
`max_profile.py` берётся из upstream 0.3.11, потому что проверенные TTS/
StationAudio изменения уже реализованы там.

После read-only проверки те же четыре updater-файла дополнительно копируются в
`/data/yapanel-state/updater`; именно этот внешний каталог использует bootstrap
для recovery после повреждения рабочего каталога.

Baseline содержит upstream hash `v0.3.5` и approved hash только трёх проверенных
локальных файлов: `server.py`, `max_profile.py`, `static/app.js`. Локальный
`start.sh`, настройки, секреты, DSP и state сохраняются отдельным механизмом.
Любая неизвестная модификация managed-файла блокирует активацию.

## Последующие обновления

Официальный следующий release может не содержать `update_agent.py`,
`updater_bootstrap.py`, `panel_update_integration.py` и локальные изменения
`server.py`/`static/app.js`. Это не удаляет их: старые updater-файлы остаются
вне manifest, а `station_overlay.py` при staging следующей версии:

1. добавляет PATH-overlay к совместимому `server.py`;
2. добавляет health/status route;
3. добавляет прямой импорт внешнего `panel_update_integration.py`;
4. добавляет UI polling статуса.

Тесты моделируют два последовательных upstream-релиза без updater-файлов.
При изменении структуры hook-а staging блокируется с причиной и без backup/
частичной активации.

## Recovery и rollback

Каждая активация имеет journal и уникальный backup. При сбое питания в состоянии
`activating` Android init снова запускает сохранённый `start.sh`, внешний
`updater_bootstrap.py` видит незавершённый journal и восстанавливает предыдущий
каталог и baseline до запуска панели. При `activated_pending_health` он сначала
проверяет новую версию, затем один раз выполняет rollback и проверяет старую.

Если не удаётся восстановить старую версию, записывается `rollback_failed` и
bootstrap удерживает процесс без повторных рестартов/откатов. Если питание
прерывается во время самого rollback, следующая автоматическая попытка также
останавливается в `rollback_failed`, чтобы не создавать бесконечный цикл.
Ручное
восстановление выполняется командой `update_agent.py rollback <transaction>`
после диагностики backup.

## Ограничения

`0.3.11` - beta. Автоматический health check доказывает только HTTP readiness,
точную версию и отсутствие зарегистрированных критических startup-ошибок. Он
не доказывает работу динамика, Bluetooth, StationAudio, wakeword, локального
ассистента, Zigbee и adbguard. Эти функции проверяются вручную после первой
установки.

На время технических работ авторизация веб-панели может быть отключена. Это не
считается ошибкой health check, но доступ не должен расширяться за пределы
локальной сети. После завершения всех проверок необходимо восстановить пароль
администратора; до этого безопасность процедуры не считается полностью
восстановленной.

Digest GitHub Releases API подтверждает целостность скачанного asset, но не
заменяет независимую подпись издателя. `/odm`, hosts, rootd, adbguard, absshd и
Android init не должны изменяться в рамках миграции.

## Последовательность первой установки

1. Выполнить read-only snapshot SSH/ADB и проверить `VERSION=0.3.5`.
2. Запустить `tools/backup_station_max.ps1` и проверить tar listing и SHA-256.
3. Проверить официальный asset и собрать `initial-release`.
4. Сохранить четыре updater-файла и baseline в `/data/yapanel-state/updater`.
5. Выполнить безопасный rootd capability probe без управляющих команд.
6. Остановить только `yapanel` через rootd; только после подтверждения остановки
   добавить внешний bootstrap hook в `start.sh`.
7. Проверить `--device local`, `--host 0.0.0.0`, `--port 8765`, network allowlist,
   Python и CA.
8. Выполнить staged activation внешним updater и запустить
   сервис снова.
9. Дождаться strict health check и проверить ручные аппаратные функции.

Останавливаем процедуру при недоступности backup, baseline-конфликте, изменении
hosts или `/odm`, изменении rootd/adbguard/absshd, несовпадении параметров
запуска, ошибке health check или потере SSH/ADB.
