# 遠端感測資料伺服器 (Server)

單機 Docker Compose 部署，包含 FastAPI API 與 PostgreSQL。PostgreSQL 不映射
主機 port，只有 API 容器可以連線。Alembic migration 會在 API 啟動前自動套用。

---

## ⚡ 快速開始 (Fast Start) - 全新環境 Server 啟動

在全新的伺服器環境中，執行以下 command 即可完成 100% 啟動：

```bash
# 1. 進入 server 目錄並複製環境變數設定檔
cd server
cp .env.example .env

# 2. 編輯 .env 設定資料庫密碼與 API 密鑰 (請使用 openssl rand -hex 32 產生強密碼)
# vim .env 或 nano .env

# 3. 背景編譯並啟動容器 (PostgreSQL + FastAPI)
# 若使用獨立 docker-compose (V1/Standalone)，請使用: docker-compose up -d --build
docker compose up --build -d
# 或
docker-compose up -d --build

# 4. 驗證服務啟動狀態與健康檢查
docker compose ps
curl http://127.0.0.1:8000/health
# 預期回覆: {"status":"ok"}
```

---

## 首次部署詳細說明

```bash
git pull
cd server
cp .env.example .env
```

編輯 `.env`，務必更換 `POSTGRES_PASSWORD` 與 `API_KEY`。可用以下方式各產生一組：

```bash
openssl rand -hex 32
```

將 `API_BIND_ADDRESS` 改成這台伺服器的固定內網 IP，例如：

```dotenv
API_BIND_ADDRESS=192.168.1.20
API_PORT=8000
```

不要填 `0.0.0.0`；綁定實際 LAN IP 可避免 API 同時監聽其他網路介面。如果伺服器
使用 DHCP，應先在路由器設定 DHCP reservation，避免重開機後 IP 改變。

接著啟動：

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f api
```

健康檢查：

```bash
curl http://127.0.0.1:8000/health
```

預期回覆：

```json
{"status":"ok"}
```

## Pi 端設定

Pi 的 `.env`：

```dotenv
SERVER_URL=http://192.168.1.20:8000
API_KEY=與伺服器相同的API_KEY
```

本設計只供受信任內網使用，不包含 Nginx。FastAPI 已處理 API key 驗證，額外加
Nginx 不會自動增加內網隔離能力。若未來需要網域、HTTPS 或對外服務，再加入
Caddy/Nginx。

確認沒有在路由器設定 port forwarding。若主機使用 UFW，可只允許感測器所在
網段（請依實際網段修改）：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
sudo ufw status
```

從 Pi 測試：

```bash
curl http://192.168.1.20:8000/health
```

## API

- `GET /health`：不需 API key。
- `POST /api/v1/sync/metrics`：JSON，需 `X-API-Key`。
- `POST /api/v1/sync/images`：multipart/form-data，需 `X-API-Key`。
- `GET /docs`：FastAPI 自動產生的互動文件。

圖片欄位：

- `device_id`
- `timestamp`：含 timezone 的 ISO 8601
- `image_type`：`RGB` 或 `IR`
- `image`：檔案

## 維護

更新：

```bash
git pull
cd server
docker compose up -d --build
```

查看資料庫：

```bash
docker compose exec db psql -U rpi_sensor -d rpi_sensor
```

備份：

```bash
docker compose exec -T db pg_dump -U rpi_sensor rpi_sensor > rpi_sensor.sql
```

還原與 PostgreSQL major version 升級應先在測試環境演練。不要使用
`docker compose down -v`，該指令會刪除資料庫與圖片 volumes。
