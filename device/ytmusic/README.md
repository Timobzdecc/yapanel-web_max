# yt-dlp

Самостоятельный zipapp `yt-dlp` для YouTube Music. Панель запускает его через
свой Python и не требует `pip install` на станции.

- Версия: `2026.10.26`
- Источник: <https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp>
- Лицензия: Unlicense (см. <https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE>)

Обновить после изменений на YouTube:

```bash
curl -L -o device/ytmusic/yt-dlp \
  https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp
python device/ytmusic/yt-dlp --version
```

Файл должен оставаться бинарным: в `.gitattributes` для него стоит `binary`.
