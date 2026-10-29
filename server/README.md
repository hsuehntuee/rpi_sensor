# 資料收集 Server（選用）

樹莓派自己就能存資料、提供 API（見根目錄 [README](../README.md)）。只有想把多台樹莓派的資料**集中到一台電腦**時才需要這個 Server。

- 內容：FastAPI（接收資料、提供網頁儀表板）＋ PostgreSQL（資料庫），用 Docker Compose 一次啟動。
- 資料表結構會在啟動時自動建立／升級，不需要手動建資料庫。
- 只供**受信任的內網**使用，不要開放到網際網路。

---

## 1. 準備一台電腦

- Ubuntu 22.04 / 24.04（或任何能裝 Docker 的 Linux）。
- 固定的內網 IP：在路由器幫這台設定「DHCP 保留（DHCP reservation）」，以下用 `192.168.1.20` 舉例。
- 硬碟空間：照片每台樹莓派每天約 150–300 MB，請預留足夠空間。

## 2. 安裝 Docker 與 git

```bash
sudo apt update
sudo apt install -y git curl openssl
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

執行完**登出再登入**，讓 docker 權限生效。確認：

```bash
docker compose version
```

## 3. 下載程式與設定

```bash
git clone https://github.com/hsuehntuee/rpi_sensor.git
cd rpi_sensor/server
cp .env.example .env
```

產生兩組隨機密碼（各執行一次，複製輸出的那串）：

```bash
openssl rand -hex 32
```

編輯 `.env`：

```bash
nano .env
```

```dotenv
POSTGRES_PASSWORD=<第一組隨機字串>
API_KEY=<第二組隨機字串>            # 每台樹莓派的 .env 要填一樣的
API_BIND_ADDRESS=192.168.1.20      # 這台 Server 的內網 IP（不要用 0.0.0.0）
API_PORT=8000
```

其他欄位維持預設。

建立照片資料夾並給容器寫入權限（容器內以 uid 10001 執行）：

```bash
mkdir -p storage/images
sudo chown -R 10001:10001 storage
```

## 4. 啟動

```bash
docker compose up -d --build
docker compose ps
```

兩個服務（`api`、`db`）都應該是 `Up`／`healthy`。確認：

```bash
curl http://192.168.1.20:8000/health
```

回覆 `{"status":"ok"}` 就成功了。之後開機會自動啟動。

**防火牆（建議）**：只允許樹莓派所在的網段連線，例如：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
```

路由器**不要**設定 port forwarding。

## 5. 讓樹莓派開始上傳

在每台樹莓派的 `~/rpi_sensor/.env` 設定：

```dotenv
SERVER_URL=http://192.168.1.20:8000
API_KEY=<與 Server .env 相同的 API_KEY>
```

套用（幾秒鐘，不需重新建置）：

```bash
cd ~/rpi_sensor
docker compose up -d
```

樹莓派之前累積的資料會自動逐步補傳（每 30 秒 10 張照片＋100 筆數值），量大時需要一段時間。

## 6. 查看資料

- 瀏覽器：`http://192.168.1.20:8000/dashboard`，可切換各台樹莓派。
- API 文件：`http://192.168.1.20:8000/docs`。
- 照片檔：`server/storage/images/<DEVICE_ID>/`。
- 資料庫：

  ```bash
  docker compose exec db psql -U rpi_sensor -d rpi_sensor
  ```

  例如 `SELECT device_id, timestamp, temperature, humidity, co2_ppm FROM env_metrics ORDER BY timestamp DESC LIMIT 10;`，輸入 `\q` 離開。

- 一鍵摘要：`./inspect.sh`。

| 資料表 | 內容 |
|---|---|
| `env_metrics` | `device_id`、`timestamp`（UTC）、`temperature`、`humidity`、`co2_ppm` |
| `camera_logs` | `device_id`、`timestamp`、`image_type`（RGB/IR）、`file_path`、`size_bytes` |
| `hvac_status` | 舊的冷氣功率表，已不再寫入 |

## 7. 維護

更新程式：

```bash
cd rpi_sensor
git pull
cd server
docker compose up -d --build
```

備份（資料庫＋照片）：

```bash
docker compose exec -T db pg_dump -U rpi_sensor rpi_sensor > backup_$(date +%F).sql
sudo tar czf images_$(date +%F).tar.gz storage/images
```

**不要執行 `docker compose down -v`**，`-v` 會把資料庫整個刪掉。

## 8. 上傳介面（樹莓派自動使用，一般不需要理會）

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/health` | 健康檢查（不需 API key） |
| POST | `/api/v1/sync/metrics` | 數值上傳，JSON，標頭 `X-API-Key` |
| POST | `/api/v1/sync/images` | 照片上傳，multipart（`device_id`、`timestamp`、`image_type`、`image`），標頭 `X-API-Key` |

同一台、同一時間的資料重複上傳會自動忽略，所以重傳是安全的。
