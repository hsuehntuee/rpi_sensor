FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app


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
COPY src ./src
COPY tests ./tests

CMD ["python", "-m", "src.main"]

