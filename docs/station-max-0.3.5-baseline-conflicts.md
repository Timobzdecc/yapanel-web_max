# Конфликты baseline установленной 0.3.5

Анализ выполнен по read-only snapshot станции и локальным тегам репозитория. Конфликты не добавляются в approved hashes автоматически, а исполняемые файлы не исключаются из manifest без проверки зависимостей.

## Сводка

| Файл | Источник | Причина расхождения | Управление updater | Сохранение |
| --- | --- | --- | --- | --- |
| `CONTRIBUTION.md` | Есть в `v0.3.5` и `v0.3.11` | Файл отсутствует в `/data/yapanel`, но есть в baseline, поэтому текущий hash отсутствует | Не считать runtime-файлом станции; требуется явная политика optional release metadata | Локального файла нет, удалять нечего |
| `LICENSE` | Есть в `v0.3.5` и `v0.3.11` без изменения | Файл отсутствует в `/data/yapanel`, но есть в baseline | Не считать runtime-файлом станции; требуется явная политика optional release metadata | Локального файла нет, удалять нечего |
| `README.md` | Есть в обеих версиях, содержимое изменилось между тегами | Файл отсутствует в `/data/yapanel`, но есть в baseline | Не считать runtime-файлом станции; требуется явная политика optional release metadata | Локального файла нет, удалять нечего |
| `device/agent/build/VERSION` | Сгенерирован в release asset `0.3.11`; в Git-тегах отсутствует | Станция содержит старый generated artifact, которого нет в baseline v0.3.5 | Управлять только после проверки совместимости agent и формата запуска | Сохранить до подтверждения замены |
| `device/agent/build/yapanel-agent` | Сгенерированный бинарник release asset | Размер совпадает с новым asset, hash станции другой; в тегах отсутствует | Управлять после проверки вызывающих путей и протокола agent | Сохранить до подтверждения замены |
| `device/assistant/build/VERSION` | Сгенерирован в release asset `0.3.11`; в Git-тегах отсутствует | Станция содержит старый generated artifact, которого нет в baseline v0.3.5 | Управлять только вместе с совместимым `assistantd` | Сохранить до подтверждения замены |
| `device/assistant/build/assistantd` | Сгенерированный бинарник release asset | Размер совпадает с новым asset, hash станции другой; в тегах отсутствует | Управлять после проверки boot hook и локального ассистента | Сохранить до подтверждения замены |
| `device/viz/build/VERSION` | Сгенерирован в release asset `0.3.11`; в Git-тегах отсутствует | Станция содержит старый generated artifact, которого нет в baseline v0.3.5 | Управлять только вместе с проверкой `vizd` и DSP/LED путей | Сохранить до подтверждения замены |
| `device/viz/build/vizd` | Сгенерированный бинарник release asset | Размер совпадает с новым asset, hash станции другой; в тегах отсутствует | Управлять после проверки DSP и визуальных функций | Сохранить до подтверждения замены |

## Доказательства hashes

Отсутствующие документы имеют upstream SHA-256 в baseline `v0.3.5`:

- `CONTRIBUTION.md`: `ae0c27b66d6d600a676a929b04c13a8cf90bfa7fc482ddcd6243ebde57e40a96`;
- `LICENSE`: `8bd6f88648efb826282bb743c31072bd838c9e6e8091dca2ad0d8556561f0dc1`;
- `README.md`: `a363760bfb5ea7773f06b36b592e8ff7b9500fc4cf8db5889e9fb4410e668915`.

На станции эти три файла отсутствуют. Поэтому расхождение является отсутствием необязательной документации, а не локальной модификацией.

Сгенерированные файлы отсутствуют в обоих Git-тегах, но присутствуют в `windows-arm64.zip` `0.3.11`. Сравнение release asset со станцией:

| Файл | Станция | `0.3.11` asset | Размер |
| --- | --- | --- | --- |
| `device/agent/build/VERSION` | `027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2` | `19754aefd27e92de8c47211fbc5bb30d95e85a61128529c448e75c47e7c0db8d` | 6 / 7 |
| `device/agent/build/yapanel-agent` | `ff096256a027cbcff547c591d9c2622914d9078258978d9e64651a50f71b4761` | `ce7900c8b7d92d265b81ff38d2e9c1d26790ec91b35fcda02cc1b89087e82064` | 5308568 |
| `device/assistant/build/VERSION` | `027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2` | `19754aefd27e92de8c47211fbc5bb30d95e85a61128529c448e75c47e7c0db8d` | 6 / 7 |
| `device/assistant/build/assistantd` | `cd0e90d7045c194b88e81f0e9873c7ebd5d7b1586071b35834832b1f1b492df9` | `005763278b9197f1bdd1c3dd2dcf37b73df5311c03a2f5bfef8d51abfe79c78a` | 5308568 |
| `device/viz/build/VERSION` | `027a61167f091a427fa7844dcac0c5141bafbcbdcadc7e04a93578147ab956f2` | `19754aefd27e92de8c47211fbc5bb30d95e85a61128529c448e75c47e7c0db8d` | 6 / 7 |
| `device/viz/build/vizd` | `b61f4a152f614667ba538d652cb6695b5df020f4ceb4a77d2afa69112558755b` | `c2f7912d96b87103e1bcadcc22dd11337a6ac40932f7a216fa9f24c894f02478` | 2162840 |

## Подтвержденная lineage-политика

Три пары generated binaries и VERSION на станции не являются неизвестными
сборками. Их содержимое совпадает с официальным release `v0.3.4`:

- `yapanel-agent-linux-arm64`: `ff096256...71b4761`;
- `assistantd-linux-arm64`: `cd0e90d7...b492df9`;
- `vizd-linux-arm64`: `b61f4a15...5587b`;
- три `VERSION`: `0.3.4\n`, SHA-256 `027a6116...ab956f2`.

Для этих файлов добавлен `tools/station-max-generated-lineage.json`.
Каждая запись содержит репозиторий, release tag, имя asset, digest asset,
путь внутри архива, SHA-256 файла, размер и ELF/Go metadata.

Стратегия остаётся `Block and report`:

1. Документы не объявляются approved hashes и не должны удаляться с Android-установки.
2. Generated binaries не исключаются из manifest.
3. Разрешаются только шесть точных hashes официальной lineage v0.3.4.
4. Любой другой hash, неверная версия, ABI, ELF metadata или отсутствие provenance блокирует preflight.
5. При миграции binaries заменяются проверенными artifacts целевого официального релиза.
