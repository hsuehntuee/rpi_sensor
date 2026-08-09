# Raspberry Pi 5 環境感測與雙相機邊緣節點 (Edge Sensor & HVAC Control System)

本專案部署於樹莓派 5 (Raspberry Pi 5) 邊緣端，主要任務為定時擷取環境溫濕度/CO2、控制常規與熱影像相機拍攝，以及透過 Modbus RTU 控制與記錄冷氣能耗，最後將資料庫暫存數據同步上傳至雲端伺服器。

## 1. 系統硬體接線設計 (1-to-1 獨立配線)

為了避免「一對多」共用電源、地線或 I2C 匯流排所導致的實體接線混亂，本系統採用 **完全 1-to-1 的直連配線**（每個感測器引腳均直連樹莓派的獨立引腳，不需麵包板或銲接分流）。

### 樹莓派 5 實體引腳 FLIR Lepton 1-to-1 接線表

| 樹莓派 5 實體腳位 | BCM 功能定義 | FLIR Lepton 模組腳位 | 功能與佔線狀態說明 |
| :--- | :--- | :--- | :--- |
| **Pin 1 (或 Pin 17)** | 3.3V Power | **VIN** | 3.3V 電源輸入 |
| **Pin 6 (或 Pin 9)** | Ground | **GND** | 系統接地 (Ground) |
| **Pin 23** | GPIO11 (SPI0 SCLK) | **CLK** | Lepton VoSPI 影像時鐘線 |
| **Pin 21** | GPIO9 (SPI0 MISO) | **MISO** | Lepton VoSPI 影像數據輸出線 |
| **Pin 19** | GPIO10 (SPI0 MOSI) | **MOSI** | SPI0 數據輸入線 *(實體已接，傳輸時忽略，無佔線衝突)* |
| **Pin 24** | GPIO8 (SPI0 CE0) | **CS** | Lepton VoSPI 影像片選線 (Chip Select) |
| **Pin 3** | GPIO2 (I2C1 SDA) | **SDA** | Lepton CCI I2C1 數據線 (與其他 I2C 設備位址 `0x2A` 隔離) |
| **Pin 5** | GPIO3 (I2C1 SCL) | **SCL** | Lepton CCI I2C1 時鐘線 |
| **Pin 11** | GPIO17 | **VSYNC** | Lepton 硬體影格同步腳位 *(獨立引腳，無佔線衝突)* |

---

## 2. 樹莓派 OS 前置系統設定

由於 SCD41 感測器改用獨立的 `I2C2` 匯流排，必須在樹莓派 OS (Bookworm 64-bit) 中載入 Device Tree 覆蓋層：

1. 編輯設定檔：
   ```bash
   sudo nano /boot/firmware/config.txt
   ```
2. 在檔案末尾加入以下設定：
   ```text
   # 啟用 I2C1 (給 Lepton CCI 控制)
   dtparam=i2c_arm=on
   
   # 啟用 SPI0 (給 Lepton VoSPI 影像讀取)
   dtparam=spi=on
   
   # 啟用額外的 I2C2 並指定實體腳位 32/33 (給 SCD41)
   dtoverlay=i2c2-pi5,pins_12_13
   ```
3. 保存並重啟樹莓派：
   ```bash
   sudo reboot
   ```
4. 重啟後驗證設備節點是否正確建立：
   ```bash
   ls -l /dev/i2c-1 /dev/i2c-2 /dev/spidev0.0
   ```

---

## 3. 本地資料庫 Schema 設計 (SQLite)

本機資料儲存於 SQLite 中（斷網時暫存），包含三張資料表：

### 3.1 `env_metrics` (環境數值表)
```sql
CREATE TABLE env_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,         -- 樹莓派裝置編號
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    temperature REAL,                -- 溫度 (攝氏)
    humidity REAL,                   -- 相對濕度 (%)
    co2_ppm INTEGER,                 -- CO2 濃度 (PPM)
    is_synced INTEGER DEFAULT 0      -- 同步標記 (0: 未同步, 1: 已同步)
);
```

### 3.2 `hvac_status` (空調能耗與狀態表)
```sql
CREATE TABLE hvac_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    hvac_state INTEGER,              -- 冷氣狀態 (0: 關閉, 1: 開啟, -1: 離線/錯誤)
    power_w REAL,                    -- 耗電瓦數 (W)
    is_synced INTEGER DEFAULT 0
);
```

### 3.3 `camera_logs` (相機拍攝索引表)
儲存拍攝影像之本機檔案路徑與後設資料：
```sql
CREATE TABLE camera_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    image_type TEXT NOT NULL,        -- 'RGB' 或 'IR'
    file_path TEXT NOT NULL,         -- 影像儲存之路徑 (例如 '/data/images/xxx.jpg')
    is_synced INTEGER DEFAULT 0
);
```

---

## 4. 模組說明與運作機制

### 4.1 感測器與相機驅動
*   **SCD41 CO2 感測器 ([scd41.py](src/sensors/scd41.py))**: 
    透過 Adafruit CircuitPython SCD4X 驅動讀取，當感測器 `data_ready` 時寫入溫濕度與 CO2。
*   **RGB Camera ([camera_rgb.py](src/sensors/camera_rgb.py))**:
    使用新版樹莓派官方支援的 `rpicam-still` 擷取影像。
*   **FLIR Lepton IR Camera ([camera_ir.py](src/sensors/camera_ir.py))**:
    - **CCI 控制**：經由 I2C 讀取 Lepton 核心料號並自動判定解析度（如 Lepton 2.5 為 80x60，Lepton 3.5 為 160x120）。
    - **VoSPI 接收**：自實體 SPI0 連接埠以 20MHz 速度讀取 164-byte 封包。支援 Lepton 3.x 分段拼接（檢查 Packet 20 中的 Segment ID 進行影像對齊與防掉幀重置）。
    - **影像生成**：讀取出的 16-bit 熱原始數據經由 `numpy` 進行 Min-Max 歸一化，並由 `Pillow` 導出為標準 JPG 影像。

### 4.2 備援與容錯設計 (Fault Tolerance)
*   **多執行緒寫入防鎖定**：資料庫實作連線 `timeout=20.0`，有效預防 APScheduler 多執行緒背景併發讀寫 SQLite 時產生的 `database is locked` 錯誤。
*   **硬體安全降級 (Dummy Fallback)**：當特定感測器未接線或在非樹莓派測試環境執行時，系統會自動切換為 Dummy 模擬器並記錄 Warning，**絕不導致主進程與其他正常感測器的排程當機**。

---

## 5. 快速開始與部署驗證

### 5.1 建立設定檔
複製設定範本並修改：
```bash
cp .env.example .env
```
設定環境變數：
```ini
DEVICE_ID=rpi_agri_304D
SERVER_URL=http://your-server-url:8000
API_KEY=your-secure-api-key-at-least-16-chars
DATABASE_PATH=/data/sensor.db
IMAGE_DIR=/data/images
SCD41_I2C_BUS=2
LEPTON_I2C_BUS=1
LEPTON_SPI_BUS=0
LEPTON_SPI_DEVICE=0
```

### 5.2 執行單元測試 (Docker 容器內)
您可以在 Docker 內運行單元測試，包括 Lepton VoSPI 的 Mock 串流解碼測試：
```bash
docker compose run --entrypoint "python -m pytest" edge-sensor
```

### 5.3 正式背景啟動
使用 Docker 一鍵在背景啟動採樣：
```bash
docker compose up --build -d
# 或舊版: docker-compose up -d --build
```

### 5.4 觀測運作 Log
```bash
docker compose logs -f edge-sensor
```
正常啟動時會輸出以下資訊：
```text
Successfully initialized SCD41 hardware sensor on I2C bus 2
Successfully initialized RGB PiCamera on index 0
Detected FLIR Lepton: Lepton 3.5 (160x120)
Successfully initialized FLIR Lepton IR Camera on SPI bus 0, device 0
```
