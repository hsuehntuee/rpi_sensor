# Raspberry Pi 5 環境感測與雙相機邊緣節點 & 遠端 Server 系統 (Edge Sensor & Server System)

本專案包含兩個主要部分：
1. **樹莓派 5 邊緣感測節點 (Edge Sensor Node)**：部署於 Raspberry Pi 5，負責 CO2/溫濕度感測 (SCD41)、RGB 相機與 FLIR Lepton 熱影像相機拍攝，以及透過 Modbus RTU 讀取 HVAC 冷氣狀態與能耗。
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

---

## 🔌 FLIR Lepton 3.X 核心支援與外部專案清理說明

本專案已將外部專案 [`Raspberry-Pi-FLIR-Lepton-Thermal-Imaging-Camera`](./Raspberry-Pi-FLIR-Lepton-Thermal-Imaging-Camera) 的 Lepton 3.X (160x120) 4 分段 VoSPI 影像解析核心技術與拼接演算法，完全移植並重構於本專案的底層驅動中：
- Native C 驅動：[`src/sensors/lepton_capture.c`](src/sensors/lepton_capture.c)
- Python 相機介面：[`src/sensors/camera_ir.py`](src/sensors/camera_ir.py)

### 🧹 您可以安全刪除外部子目錄
因為我們已經將 160x120 影像獲取功能直接整合進 Python 容器，**您不需要在樹莓派上編譯運作圖形化的 Qt 串流程式，也不需要 TV 輸出相關的硬體配置**。
因此，您可以放心地執行以下命令**刪除外部子專案目錄**以保持專案乾淨：
```bash
rm -rf Raspberry-Pi-FLIR-Lepton-Thermal-Imaging-Camera
```

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

---

## 🎯 FLIR Lepton IR 正式環境對齊驗證說明

正式環境的熱感應相機驅動 [`src/sensors/camera_ir.py`](src/sensors/camera_ir.py) 已 **100% 完全遵循黃金標準 [`tests/verify_ir.py`](tests/verify_ir.py)** 進行實作：

1. **CCI 控制與自動軟體 Reboot 重置**：
   - 拍照前自動透過 I2C 讀取 Status Register (`0x0002`)。
   - 若檢測到 Lepton 核心處於 `BootOK=False` 或 `Busy` 狀態，會自動發送 `0x0242` 軟體重置命令，確保感測器脫離錯誤鎖死狀態。
2. **Native C Kernel SPI 驅動引擎**：
   - 優先使用由 [`src/sensors/lepton_capture.c`](src/sensors/lepton_capture.c) 編譯的 100% 零延遲 C 語言 VoSPI 接收引擎。
   - 當 Native C 引擎不可用時，自動降級為 3-Chunk (3936B, 3936B, 1968B) 的 Python 讀取器與 CS High 200ms 硬體重置機制。
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
