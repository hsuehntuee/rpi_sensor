# Raspberry Pi 5 環境感測與雙相機邊緣節點 & 遠端 Server 系統 (Edge Sensor & Server System)

本專案包含兩個主要部分：
1. **樹莓派 5 邊緣感測節點 (Edge Sensor Node)**：部署於 Raspberry Pi 5，負責 CO2/溫濕度感測 (SCD41)、RGB 相機與 FLIR Lepton 熱影像相機拍攝，以及透過 ESP32 紅外線遙控冷氣（`/ac` 頁面）。
2. **遠端感測資料伺服器 (Remote Server)**：採用 Docker Compose 部署 FastAPI + PostgreSQL，負責接收邊緣端的環境指標數據與 JPEG 影像。

---

## ⚡ 快速開始 (Fast Start)

### 1. 全新環境 Server 端啟動步驟

在全新的伺服器 (Server) 主機上，請依序執行以下命令：

```bash
# 1. 複製 repository 並進入 server 目錄
cd server

# 2. 複製設定檔範本
cp .env.example .env

# 3. 編輯 .env (設定 POSTGRES_PASSWORD 與 API_KEY)
# 可使用 openssl rand -hex 32 產生強金鑰
nano .env

# 4. 使用 Docker Compose 一鍵背景啟動 (自動執行資料庫 Migration 與 FastAPI 服務)
docker compose up --build -d
# 或舊版/獨立版本: docker-compose up -d --build

# 5. 健康檢查驗證
curl http://127.0.0.1:8000/health
# 預期輸出: {"status":"ok"}
```

### 2. 樹莓派 邊緣端 (Edge Sensor Node) 啟動步驟

在 Raspberry Pi 5 32-bit (或 64-bit) OS 上，我們提供了一鍵配置與啟動指令：

```bash
# 1. 給予啟動腳本執行權限
chmod +x start_edge.sh

# 2. 執行腳本 (會自動偵測硬體、啟用 SPI/I2C、建立並配置 .env 檔，最後一鍵編譯與啟動 Docker)
./start_edge.sh
```

*(提示：首次啟動後，請確保您的 `.env` 檔案內正確設定了 `DEVICE_ID`、`SERVER_URL` 和 `API_KEY`。)*

### 3. 已部署的樹莓派更新程式

不需要 `docker pull`：image 是在樹莓派上依 `Dockerfile` 建的，`start_edge.sh` 每次都會自動重建（`docker compose up --build -d`）。

```bash
cd <repo 路徑>
git status        # 先確認沒有本機修改（.env、data/ 不在版控內，不受影響）
git pull
nano .env         # 對照 .env.example 補上新增的設定（start_edge.sh 只在 .env 不存在時才建立，不會幫你補）
./start_edge.sh   # 重建並在背景重啟；最後停在 log 畫面，按 Ctrl+C 離開即可，服務會繼續執行
```

- `requirements.txt` 有變動時，Docker 會重裝所有 Python 套件，第一次重建可能要很久（和初次安裝差不多），屬正常現象。
- 只改 `.env` 時不用重建，執行 `docker compose up -d` 即可套用。



---

## 📂 專案架構與相對檔案路徑

本專案檔案標註均採用 **相對路徑**，方便在任何環境下直接參照：

- [`start_edge.sh`](start_edge.sh) — 樹莓派邊緣端一鍵硬體設定與 Docker 啟動腳本。
- [`src/main.py`](src/main.py) — 樹莓派邊緣端主進程（包含 APScheduler 定時排程與同步任務）。
- [`src/sensors/camera_ir.py`](src/sensors/camera_ir.py) — FLIR Lepton IR 熱感應相機驅動（正式環境 IR 處理模組）。
- [`src/sensors/camera_rgb.py`](src/sensors/camera_rgb.py) — 樹莓派官方 RGB 相機驅動（rpicam-still / libcamera-still）。
- [`src/sensors/scd41.py`](src/sensors/scd41.py) — SCD41 CO2 / 溫濕度感測器驅動。
- [`src/sensors/lepton_capture.c`](src/sensors/lepton_capture.c) — High-Performance Native C SPI Kernel VoSPI Driver。
- [`server/app/main.py`](server/app/main.py) — 遠端 Server FastAPI 主程式與 API Endpoints。
- [`server/compose.yaml`](server/compose.yaml) — 遠端 Server Docker Compose 配置檔（FastAPI + PostgreSQL）。
- [`tests/verify_ir.py`](tests/verify_ir.py) — FLIR Lepton 熱影像相機硬體驗證黃金腳本 (Golden Engine Standard)。
- [`docs/README.md`](docs/README.md) — 樹莓派硬體 1-to-1 接線與 Device Tree 詳細文檔。
- [`src/control/ir_ac.py`](src/control/ir_ac.py) — ESP32 紅外線冷氣遙控（USB 序列埠）。
- [`esp32_ir/firmware`](esp32_ir/firmware) — ESP32 韌體（PlatformIO，IRremoteESP8266）。
- [`esp32_ir/flash_esp32.bat`](esp32_ir/flash_esp32.bat) — 點兩下燒錄 ESP32（Windows）。

---

## 🎯 FLIR Lepton IR 長效穩定性與自我恢復機制說明

正式環境的熱感應相機驅動 [`src/sensors/camera_ir.py`](src/sensors/camera_ir.py) 具備高可靠度無人值守容錯架構：

1. **CCI 控制與真 OEM 軟體 Reboot 自動重置**：
   - 拍照前讀取 Status Register (`0x0002`)。若檢測到晶片處於 FFC 快門自動校正狀態 (`Busy=1`)，自動優雅等待最多 2.5 秒，避免暴力中斷。
   - 若檢測到異常或逾時，自動發送真實的 Lepton OEM 軟體重置命令 **`0x4842`** (`LEP_CID_OEM_REBOOT`)，重啟內部 ASIC 並等待重新開機完成 (`BootOK=1`)。
   - 可選硬體引腳支援：支援配置 `LEPTON_RESET_GPIO`（如接至 Pin 13 / GPIO27），當 I2C 死鎖時自動觸發 50ms 主動低電位硬體冷重置。
2. **Native C Kernel SPI 單例驅動引擎**：
   - 使用由 [`src/sensors/lepton_capture.c`](src/sensors/lepton_capture.c) 編譯的 100% 零延遲 C 語言 VoSPI 接收引擎。
   - 驅動採用開機單例快取（Singleton），徹底避免反覆 gcc 編譯與 dlopen 造成的記憶體與動態連結洩漏。
   - 移除失敗次數鎖定限制，即使遇到偶發干擾也始終維持 C 核心運作，並具備 250ms CS High 脫困時序。
   - 加入硬體互斥鎖 (`threading.Lock`)，確保定時排程、網頁 Dashboard 即時拍照與外部信號快照不產生資源競爭。
3. **高動態熱影像後處理 Pipeline**：
   - **14-bit Pixels 遮罩與絕對溫度換算**：使用 `raw & 0x3FFF` 處理，並支援 $T_{\text{Celsius}} = \frac{\text{raw}}{100} - 273.15$。
   - **5% 至 95% Percentile 動態範圍裁切**：自動去除極端噪訊，提升人臉與環境對比度。
   - **鏡像水平翻轉**：採用 `np.fliplr` 修正相機鏡像。
   - **3x3 中值去斑過濾器 (De-striping Filter)**：有效消除 Lepton 條紋噪訊 (Zebra stripes)。
   - **Thermal Ironbow / Rainbow 偽彩色 Lut 映射** 與 BICUBIC 高品質雙三次內插放大輸出 JPEG。

---

## 🛠️ 單元測試與硬體驗證

在邊緣端 Docker 容器內執行硬體與模組測試：

```bash
# 執行所有 Pytest 單元測試
docker compose run --entrypoint "python -m pytest" edge-sensor

# 執行 FLIR Lepton IR 獨立硬體驗證黃金腳本
docker compose run --entrypoint "python tests/verify_ir.py" edge-sensor
```

---

## 🍓 樹莓派邊緣端即時視覺化儀表板 (Edge Local Web Dashboard)

現在樹莓派 5 邊緣節點已內建專屬的 **本地即時 Web 儀表板**！在與樹莓派處於同一個區網 (WiFi / 乙太網路) 的任何電腦、手機或平板瀏覽器直接打開：

👉 **`http://<樹莓派IP>:8080/dashboard`** （或樹莓派本機 `http://localhost:8080/dashboard`）

### 儀表板內容
1. **系統狀態（最上方）**：一切正常顯示綠色「系統正常」。有狀況時每一項都寫出原因與處理方式，並標示：
   - **可自行處理**：客戶照畫面上的說明處理即可（拔插 USB、重新開機、檢查網路），不寫 log。
   - **需聯繫**：畫面提示「請截圖此區塊聯繫我們」，並在容器 log 寫一行 `[需聯繫] ...`（同一則只寫一次）。

   | 項目 | 可自行處理 | 需聯繫 |
   |---|---|---|
   | 儲存空間 | 剩不到 5 GB | 剩不到 2 GB |
   | 空間不足自動刪照片（見下方說明） | 刪的都已上傳 | 有未上傳的照片被刪（資料遺失） |
   | 溫濕度感測器 / RGB 相機 / 熱影像相機 | | 沒偵測到，或超過 3 個取樣週期沒有新資料 |
   | 冷氣遙控器 ESP32 | 未連線（拔插 USB、重開機） | |
   | 任何一項檢查本身讀取失敗 | | ✔ |

   Server 連不上不示警也不寫 log：資料會暫存在本機、連上後自動補傳（待上傳筆數顯示在數值卡），真的快滿時由「儲存空間」示警把關。資料來源：`GET /api/v1/system/health`。
2. **即時數值**：溫度、濕度、CO₂（含通風提醒）、冷氣目前設定（點進 `/ac`）、儲存空間、待上傳筆數。
3. **雙相機最新畫面**：RGB 與 FLIR Lepton 熱影像，點圖放大。
4. **分頁**：環境紀錄表、本機相片藝廊（可篩選 RGB/熱影像）、系統資訊（硬體狀態、儲存空間、最近一次自動刪照片）。
5. **操作**：立即拍照、立即同步、自動更新（每 15 秒）開關。

**Log 原則（省 SD 卡）**：預設 `LOG_LEVEL=WARNING`，平常不寫任何例行紀錄；只寫需要聯繫的狀況（相機拍照失敗、感測器讀取失敗、硬體開機偵測失敗、儲存空間快滿、緊急刪照片）。Server 連不上不寫。同一個硬體持續故障只在第一次寫，恢復後再壞才會再寫。除錯時把 `.env` 改成 `LOG_LEVEL=INFO` 即可看到每次取樣/上傳。

**照片什麼時候會被刪？**
- 每天 03:00：刪除「已上傳到 Server」且超過 `IMAGE_RETENTION_DAYS`（預設 14 天）的照片，Server 上仍有一份。想在樹莓派多留可調大，例如 `IMAGE_RETENTION_DAYS=365`。
- 緊急保護：可用空間低於 `MIN_DISK_FREE_MB`（預設 1.5 GB）時，從最舊的照片開始刪到剩 2 GB（先刪已上傳的）。發生時儀表板會示警 30 天，並記錄在 `/data/disk_guard_last.json`。

---

## ❄️ 冷氣紅外線遙控 (ESP32 + IR)

每台樹莓派用一條 USB 線接一塊 ESP32，由 ESP32 發紅外線控制同一間房的冷氣。
打開 **`http://<樹莓派IP>:8080/ac`** 即可控制：開關、模式（冷氣/除濕/送風/暖氣/自動）、16–31°C、風速、擺風。
每次操作都會送出「完整狀態」（跟原廠遙控器一樣），所以不用記住冷氣目前是什麼狀態。

### 接線（給安裝人員）

```
 樹莓派 USB-A ════ USB 傳輸線（要能傳資料，不能只充電）════ ESP32 (NodeMCU-32S)
                    ↑ 同一條線供電 + 傳指令

 紅外線發射模組 (HW-483)            紅外線接收模組 (G / R / Y)  ← 選配
   S   ──── GPIO32                    G ──── GND
  中間 ──── 3V3                       R ──── GPIO12（韌體輸出 HIGH 供電）
   −   ──── GND                       Y ──── GPIO14
```

- **發射模組一定要接 GPIO32**：ESP32 的 GPIO34–39 只能輸入，接上去不會發光。
- 用手機前鏡頭看發射 LED：**平常不亮、送指令時閃一下**才是對的；一直亮代表 3V3 接到 S 腳了。
- 發射 LED 要正對冷氣室內機的接收窗。HW-483 由 GPIO 直接推動，距離約 1–2 m；要更遠就換「有電晶體驅動」的大功率發射模組，訊號腳一樣接 GPIO32，電源接 3V3（模組標示 5V 時改接 VIN）。
- 接收模組是選配：接了之後，有人用原廠遙控器時，網頁上的狀態會跟著同步。

### 設定（每台樹莓派的 `.env`）

```bash
AC_SERIAL_PORT=auto            # 自動找 CP210x/CH340；或填 /dev/serial/by-id/...；off = 停用
AC_PROTOCOL=HITACHI_AC344      # 冷氣協定，見下表
AC_NAME=304D 日立              # 顯示在 /ac 頁面
```

| 冷氣 | `AC_PROTOCOL` | 狀態 |
|---|---|---|
| 304D 日立 RAS-36UK | `HITACHI_AC344` | 函式庫支援，待實機確認 |
| 614 東元 MS80IH-ZRS2T | 現場抓碼後決定（`TECO` 或其他） | 待抓碼 |

其他品牌可用的協定名稱：`HITACHI_AC424`、`DAIKIN`、`MITSUBISHI_AC`、`PANASONIC_AC`、`TOSHIBA_AC`、`SAMSUNG_AC`、`LG`、`MIDEA`、`COOLIX`、`GREE`… （ESP32 上送 `{"cmd":"protocols"}` 可列出全部）。

容器已有 `privileged` 與 `/dev` 掛載，看得到 USB 序列埠，不需改 `docker-compose.yml`。

### 燒錄 ESP32（出貨前，在 Windows 電腦上做一次）

1. 用 USB 線把 ESP32 接到電腦。
2. **點兩下 `esp32_ir/flash_esp32.bat`**，等它顯示「完成」。（第一次會自動安裝工具並編譯，約 10 分鐘；之後約 30 秒。）
3. 拔下 ESP32，照上面的接線圖接到樹莓派即可。

失敗時畫面會列出常見原因：USB 線只能充電、沒裝 CH340/CP210x 驅動、需要按住板子上的 BOOT 鍵。

### 安裝到現場的順序

1. 燒好 ESP32（上一節）。
2. 樹莓派更新程式並設定 `.env`（見「快速開始 → 3. 已部署的樹莓派更新程式」，設定值見上方「設定」）。
3. ESP32 的 USB 插到樹莓派，照接線圖接好發射模組，**發射頭正對冷氣室內機的接收窗**。
4. 執行 `./start_edge.sh`（若容器已在跑，ESP32 晚點才插也可以，程式第一次用到時才會去找 port）。
5. 驗證：
   ```bash
   ls /dev/serial/by-id/          # 應看到含 1a86（CH340）或 10c4（CP210x）的裝置
   ```
   打開 `http://<樹莓派IP>:8080/ac` 按「開機」，冷氣應會嗶一聲；主儀表板的冷氣卡片應顯示目前設定，「系統資訊」分頁顯示「已連線」。

### 疑難排解

| 狀況 | 處理 |
|---|---|
| `ls /dev/serial/by-id/` 看不到裝置 | 換一條能傳資料的 USB 線；執行 `dmesg \| tail -20`，若看到 `brltty` 佔用 CH340，執行 `sudo apt remove brltty` 後重插 USB |
| 面板顯示「冷氣遙控器 (ESP32) 未連線」 | 拔插 ESP32 的 USB 線；仍不行就重新開機 |
| 面板顯示已送出，但冷氣沒反應 | 發射頭沒對準或太遠（HW-483 約 1–2 m）；用手機前鏡頭看發射頭，送指令時應閃一下；`AC_PROTOCOL` 是否正確 |
| 發射頭一直亮 | 3V3 接到了 S 腳，照接線圖重接 |
| 想看詳細 log 除錯 | `.env` 改 `LOG_LEVEL=INFO` 後 `docker compose up -d`，再 `docker compose logs -f edge-sensor`；除錯完改回 `WARNING` |

---

## 🔍 Server 端即時檢視資料庫 (DB) 與照片 (RGB & IR) 指令

在遠端 Server 主機上，您可以使用以下 3 種方式快速檢視已上傳的數據與照片：

### 1. 瀏覽器即時視覺化儀表板 (Server Web Dashboard)
打開瀏覽器訪問：
👉 **`http://<SERVER_IP>:8000/dashboard`**
- 彙整由邊緣節點同步至 PostgreSQL 的完整歷史數據與照片。
- 最新雙相機照片對照與歷史藝廊。

### 2. 一鍵快速檢視腳本 (終端機輸出)
```bash
cd server
./inspect.sh
```

### 3. 單行資料庫查詢指令
```bash
# 查看最新 10 筆環境感測數據 (SCD41)
docker compose -f server/compose.yaml exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, temperature, humidity, co2_ppm FROM env_metrics ORDER BY timestamp DESC LIMIT 10;"

# 查看最新 10 筆相機照片上傳紀錄 (RGB & IR)
docker compose -f server/compose.yaml exec db psql -U rpi_sensor -d rpi_sensor -c "SELECT id, device_id, timestamp, image_type, file_path, size_bytes FROM camera_logs ORDER BY timestamp DESC LIMIT 10;"

# 查看磁碟實體照片檔案
ls -lht server/storage/images/* | head -n 10
```

