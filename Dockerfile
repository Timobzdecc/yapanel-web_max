# Веб-панель для Яндекс Станций: Python (только стандартная библиотека) + adb + ffmpeg + openssl.

# Бинарники для колонки (device/agent, device/viz) — статическая сборка под linux/arm64; Go в итоговом образе не нужен.
FROM golang:1.26-alpine AS builder
COPY VERSION /VERSION
# агент панели (device/agent)
WORKDIR /agent
COPY device/agent/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w -X main.version=$(cat /VERSION)" -o /out-agent/yapanel-agent . \
  && cp /VERSION /out-agent/VERSION
# живой спектр (device/viz) — так же
WORKDIR /viz
COPY device/viz/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w -X main.version=$(cat /VERSION)" -o /out-viz/vizd . \
  && cp /VERSION /out-viz/VERSION

FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends adb ffmpeg openssl ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py access.py agent.py glagol.py profiles.py max_profile.py max_privacy.py assistant_max.py assistant_socks.py config.py multi.py pyadb.py dsp_max.py party.py party_store.py party_audio.py speakers.example.json VERSION ./
COPY api ./api
COPY extmusic ./extmusic
COPY static ./static
COPY device ./device
COPY --from=builder /out-agent/yapanel-agent /out-agent/VERSION ./device/agent/build/
COPY --from=builder /out-viz/vizd /out-viz/VERSION ./device/viz/build/
COPY LICENSE ./

# /data — токен Яндекса, пароль панели, сертификаты, история графиков и ключ adb (HOME).
# Свой порт adb-сервера, чтобы при network_mode: host не мешать adb на самой машине.
ENV PANEL_HOST=0.0.0.0 \
    PANEL_INSTALL=docker \
    PANEL_DATA_DIR=/data \
    HOME=/data \
    ANDROID_ADB_SERVER_PORT=5038 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765 8766

CMD ["python", "server.py"]
