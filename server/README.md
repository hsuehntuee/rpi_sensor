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

## 🔍 快速檢視資料庫 (DB) 與圖片 (RGB & IR) 指令

我們提供 3 種快速檢視伺服器資料庫與照片的方法：

### 方法 1: 一鍵終端機快速檢視腳本 (推薦)

在 server 目錄下直接執行：

```bash
chmod +x inspect.sh
./inspect.sh
```

此腳本會自動輸出：
1. 資料庫各表（環境數據、HVAC、RGB照片、IR照片）筆數與最新時間
2. 最新 5 筆 SCD41 溫濕度 & CO2 數據
3. 最新 5 筆 HVAC 狀態與耗電功率
4. 最新 5 筆 RGB 與 FLIR Lepton IR 照片紀錄
5. 實體硬碟 `storage/images` 照片檔案列表與容量

---

### 方法 2: 瀏覽器即時視覺化儀表板 (Web Dashboard)

在任何電腦或手機瀏覽器直接輸入伺服器網址：

👉 **`http://<伺服器IP>:8000/dashboard`** （或直接開 `http://<伺服器IP>:8000/`）

- 📊 **即時數值卡片**：溫度、濕度、CO2 濃度、冷氣狀態與功率。
- 📷 **最新雙相機照片並排展示**：RGB 可見光與 FLIR Lepton IR 熱影像左右並列對照，點擊可放大預覽。
- 📋 **最新歷史資料表**：SCD41 數據、HVAC 數據。
- 🖼️ **歷史相片藝廊**：可查看所有歷史 RGB 與 IR 拍攝縮圖。
- 🔄 **自動每 15 秒重新整理**。

---

### 方法 3: 常用單行 SQL & Terminal 指令

#### 1. 查看環境指標 (SCD41 溫度、濕度、CO2 最新 10 筆)：
```bash
docker compose exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, temperature, humidity, co2_ppm FROM env_metrics ORDER BY timestamp DESC LIMIT 10;"
```

#### 2. 查看 HVAC 冷氣狀態與功率最新 10 筆：
```bash
docker compose exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, hvac_state, power_w FROM hvac_status ORDER BY timestamp DESC LIMIT 10;"
```

#### 3. 查看雙相機 (RGB / IR) 照片上傳紀錄：
```bash
# 查看所有最新上傳照片
docker compose exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, image_type, file_path, size_bytes FROM camera_logs ORDER BY timestamp DESC LIMIT 10;"

# 只看 IR 熱影像照片
docker compose exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, file_path FROM camera_logs WHERE image_type = 'IR' ORDER BY timestamp DESC LIMIT 5;"

# 只看 RGB 可見光照片
docker compose exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, file_path FROM camera_logs WHERE image_type = 'RGB' ORDER BY timestamp DESC LIMIT 5;"
```

#### 4. 統計各表總筆數與最新記錄時間：
```bash
docker compose exec db psql -U rpi_sensor -d rpi_sensor -c "
SELECT 'env_metrics' AS table_name, COUNT(*) AS count, MAX(timestamp) AS latest_time FROM env_metrics
UNION ALL
SELECT 'hvac_status', COUNT(*), MAX(timestamp) FROM hvac_status
UNION ALL
SELECT 'camera_logs (RGB)', COUNT(*), MAX(timestamp) FROM camera_logs WHERE image_type = 'RGB'
UNION ALL
SELECT 'camera_logs (IR)', COUNT(*), MAX(timestamp) FROM camera_logs WHERE image_type = 'IR';
"
```

#### 5. 查看伺服器實體硬碟存儲的圖片檔案：
```bash
# 列出最近上傳的圖片檔案
ls -lht storage/images/* | head -n 15

# 或透過 API 查看圖片 JSON 列表
curl -s http://127.0.0.1:8000/api/v1/images/list
```

---

## 維護

更新：

```bash
git pull
cd server
docker compose up -d --build
```

查看資料庫互動式 Shell：

```bash
docker compose exec db psql -U rpi_sensor -d rpi_sensor
```

備份：

```bash
docker compose exec -T db pg_dump -U rpi_sensor rpi_sensor > rpi_sensor.sql
```

還原與 PostgreSQL major version 升級應先在測試環境演練。不要使用
`docker compose down -v`，該指令會刪除資料庫與圖片 volumes。
