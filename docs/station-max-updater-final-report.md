# YaPanel Station Max: итоговый отчёт

Дата проверки: 2026-11-13.

## Результат

На реальной станции YaPanel обновлена через Android Web UI с 0.3.11 до 0.3.12. Windows-инструмент не выполнял это обновление.

- Основная Web UI-транзакция: `20261113-034313-e1cd7c65`, `healthy`.
- Maintenance-транзакция исправления автозапуска: `20261113-105324-05278f64`, `healthy`.
- Версия: `0.3.12`.
- Strict health: `ready=true`, `critical_errors=0`.
- `/api/hook`: `possible=false`, `broken=false`; для этой прошивки отдельный boot hook не требуется.
- `/api/assistant`: `running=true`, `autostart=true`.
- После финального cold boot assistant worker снова запустился автоматически.

## Сохранённые локальные изменения

Station overlay применён к официальному 0.3.12. Сохранены:

- Android TTS, StationAudio, DSP и пользовательские аудиоданные.
- Алиса и локальный assistant worker.
- SOCKS5/SOCKS5H-конфигурация без публикации credentials.
- Bluetooth/Zigbee runtime assets и пользовательские настройки.
- Внешние updater и bootstrap в `/data/yapanel-state/updater`.

Патч автозапуска добавляет только retry-worker: он создаётся сразу при локальном старте панели и ждёт появления `/data/quasar`, вместо того чтобы пропустить worker из-за раннего boot. Дублирование не создаётся: worker проверяет PID и межпроцессный lock.

## Проверка реальной станции

После maintenance restart без вызова `/api/assistant/start` наблюдались:

- bootstrap PID `15116`;
- YaPanel PID `15122`;
- assistant PID `15223`;
- `init.svc.yapanel=rootd=adbguard=absshd=running`;
- `sys.boot_completed=1`;
- `start.sh` сохранил hash `7424f025be9b9171038bf2761833dbdc914bb687bfc4127c049187e075851067`;
- `max_profile.py`: `ff55dcc781f4496c8ce312da97326431ac78440695a919e65cdcf4b6626a489c`;
- patched `server.py`: `8b1999946bd2d90575e6cbf7cb5b21543fa78d19dc161310854c266379dd5c11`;
- `update_agent.py`: `d7bfc6f3471e9531ccaadcd2a371d11e087bce65483dfab5eb295cec56f13f89`;
- `station_overlay.py`: `3ddcf8656558f58304555dab3029af24d4118dbd91f98867c43e44cb19f35a0e`;
- bootstrap: `a8ad1d9b61a0ea2c6272f7b9e936b15c7e7d3a67f1a05b3f373a1441a2a261af`.

После разрешённого cold boot наблюдались новые PID: bootstrap `4276`, YaPanel `4849`, assistant `5476`. `sys.boot_completed=1`, `init.svc.yapanel=running`, assistant `running=true`, а запрос через сохранённый SOCKS5 после reboot вернул HTTP 200. Дублирующих процессов не обнаружено.

`/odm` остался read-only; hosts mount topology, rootd, adbguard, absshd, SSH и ADB не изменялись. Порт 8765 слушает, параметры `--device local`, `--host 0.0.0.0`, `--port 8765` и `PANEL_ALLOW_NETWORKS=192.168.31.0/24` сохранены.

Первый cold boot после 0.3.12 выявил раннее условие `/data/quasar`: assistant worker не был создан. После исправления overlay и ownership-safe updater финальный cold boot подтвердил автоматический запуск worker без ручного API start.

Атомарные записи updater теперь наследуют mode существующего файла и владельца существующего файла/родительского каталога. Новые transaction, journal и backup при root-вызове не остаются недоступными пользователю `shell`.

## Проверка fixture и regression

Проверены реальные временные каталоги, SHA-256, Python CLI, durable journal, staged activation, strict health, rollback, PowerShell-контракт и последовательные Web UI-релизы. Отдельно покрыты:

- отсутствие `/data/quasar` в момент старта и последующее появление файлов;
- длительное отсутствие зависимостей;
- уже запущенный assistant worker;
- явное отключение autostart;
- сохранение overlay на последовательных официальных релизах;
- блокировка upstream без проверяемого assistant worker;
- выбор целевого файла `server.py` для maintenance CLI;
- failure до health и legacy rollback.

Последний адресный прогон: `70 passed, 2 skipped`.

Предыдущий полный regression до финального overlay: `1004 passed, 48 skipped, 56 subtests passed`.

## Git

Релевантные коммиты последнего этапа:

- `2a46c1a` - v0.3.12 overlay и бинарно-безопасный ADB push;
- `cea9555` - безопасное исключение нечитаемых generated caches из backup;
- `17c26a8` - сохранение supervisor wrapper после rollback и durable journal priority;
- `0b38163` - выбор внешнего updater state на Android;
- `81bb622` - актуализация теста wrapper recovery;
- `13367e9` - retry автозапуска assistant после Android boot;
- `182c6bf` - корректный target file для maintenance patch CLI.
- `41d95b1` - сохранение owner/mode updater при атомарных записях.

Рабочее дерево также содержит ранее существовавшие несвязанные изменения и локальные диагностические каталоги; они не входят в эти коммиты.

## Следующие обновления

Кнопка Web UI загружает официальный release asset, проверяет SHA-256 GitHub digest, применяет version-aware overlay, создаёт backup и durable transaction, затем bootstrap подтверждает health или выполняет rollback. Для неизвестного server.py без проверяемого supervisor/assistant contract staging блокируется до activation. Для известных совместимых релизов overlay не требует ручного списка SHA-256 локальных файлов.

## Ограничения

- Аппаратные проверки TTS, StationAudio, DSP, Bluetooth, Zigbee и голосового сценария Алисы требуют ручного smoke-test.
- Полный rollback реального рабочего релиза искусственно не провоцировался; его fault-injection покрыт fixture.
- Веб-панель оставлена без пароля по временному техническому решению. После завершения работ необходимо восстановить пароль администратора и проверить доступ только из локальной сети.

## Инструкция переноса

Для другой совместимой rooted Station Max используется официальный release плюс файлы overlay/updater из репозитория, без `.panel_auth.json`, proxy credentials, API keys, SSH-ключей и индивидуальных настроек. Сначала выполняется read-only preflight и backup, затем staging проверяет модель, Android/Python, ELF assets, imports и применимость overlay. Неизвестная прошивка или отсутствие проверяемого contract блокируют установку до изменения рабочих файлов.
