FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app


RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       i2c-tools \
       libgpiod2 \
       gcc \
       python3-dev \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY tests ./tests

CMD ["python", "-m", "src.main"]

