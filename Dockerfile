FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Taipei


RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       gnupg \
       curl \
       ca-certificates \
       i2c-tools \
       libgpiod2 \
       gcc \
       g++ \
       python3-dev \
       libjpeg-dev \
       zlib1g-dev \
       ffmpeg \
       v4l-utils \
    && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] http://archive.raspberrypi.com/debian/ bookworm main" > /etc/apt/sources.list.d/raspi.list \
    && apt-get update \
    && (apt-get install -y --no-install-recommends rpicam-apps || apt-get install -y --no-install-recommends libcamera-apps || true) \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 冷氣遙控用（ESP32 USB 序列埠）。獨立一層安裝，樹莓派重建時不必重裝上面所有套件
RUN pip install --no-cache-dir pyserial==3.5
COPY src ./src
COPY tests ./tests

CMD ["python", "-m", "src.main"]

