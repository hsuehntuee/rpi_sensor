# 樹莓派環境測站（溫濕度 / CO₂、雙相機、冷氣遙控）

每間房一台樹莓派 5，每 5 分鐘記錄一次：

- 溫度、濕度、CO₂（SCD41 感測器）
- 一張可見光照片（樹莓派相機）＋一張熱影像（FLIR Lepton）

並透過一塊 ESP32 發紅外線遙控冷氣。

所有資料都存在樹莓派上，內網裡的電腦、手機都能用瀏覽器看儀表板，或用 **API** 讀資料、控制冷氣（見〈6. 給工程師：內網 API〉）。要集中收資料的話，可以另外架一台 Server（見 [server/README.md](server/README.md)）。

```
 ┌──────────── 樹莓派 5 ────────────┐        內網任何裝置
 │ SCD41 ─ I2C                      │      ┌─────────────────┐
 │ Lepton ─ SPI/I2C   儀表板＋API ──┼─────▶│ 瀏覽器 / 你的程式 │
 │ 相機 ─ 排線         (port 8080)   │      └─────────────────┘
 │ ESP32 ─ USB ──▶ 紅外線 ──▶ 冷氣   │──(選用)──▶ Server
 └──────────────────────────────────┘
```

---

## 1. 需要準備的東西

| 品項 | 數量 | 備註 |
|---|---|---|
| Raspberry Pi 5 | 1 | 4 GB RAM 以上 |
| 樹莓派 5 原廠電源 | 1 | 5V 5A USB-C。電源不足會造成當機或冷氣遙控失靈 |
| microSD 卡 | 1 | 64 GB 以上，建議 128 GB |
| SCD41 溫濕度 / CO₂ 感測器 | 1 | |
| FLIR Lepton 3.x 熱影像模組（含轉接板） | 1 | |
| 樹莓派相機 | 1 | 用排線接 |
| ESP32 開發板（NodeMCU-32S / ESP32-WROOM） | 1 | 用來遙控冷氣 |
| 紅外線發射模組 | 1 | HW-483 或大功率模組（有電晶體驅動的，距離較遠） |
| 紅外線接收模組 | 1 | 腳位標 `G`、`R`、`Y`（選配，見 2.4） |
| USB 傳輸線 | 1 | 要能**傳資料**，不能只充電 |
| 杜邦線（母對母） | 約 20 條 | |
| Windows 電腦 | 1 | 燒錄 ESP32 用（樹莓派上也能燒，見 4.2） |

---

## 2. 接線

> 腳位編號指的是樹莓派 40 針排針的**實體編號**（Pin 1 在靠近 SD 卡那一端、標有方框的那一腳）。

### 2.1 SCD41（溫濕度 / CO₂）

| 樹莓派腳位 | SCD41 |
|---|---|
| Pin 1（3.3V） | VIN |
| Pin 6（GND） | GND |
| Pin 32（GPIO12） | SDA |
| Pin 33（GPIO13） | SCL |

### 2.2 FLIR Lepton（熱影像）

| 樹莓派腳位 | Lepton 轉接板 |
|---|---|
| Pin 17（3.3V） | VIN |
| Pin 9（GND） | GND |
| Pin 23（SCLK） | CLK |
| Pin 21（MISO） | MISO |
| Pin 19（MOSI） | MOSI |
| Pin 24（CE0） | CS |
| Pin 3（SDA） | SDA |
| Pin 5（SCL） | SCL |
| Pin 11（GPIO17） | VSYNC |
| Pin 13（GPIO27） | RESET_L（選接；接了要在 `.env` 設 `LEPTON_RESET_GPIO=27`） |

### 2.3 樹莓派相機

斷電狀態下，用排線接到樹莓派 5 的 **CAM/DISP 0** 接口（金屬接點朝向接口的接點）。

### 2.4 ESP32（冷氣遙控）

```
 樹莓派 USB 孔 ════ USB 傳輸線 ════ ESP32
                  （同一條線供電＋傳指令）

 紅外線發射模組                     紅外線接收模組（選配）
   訊號 (S / IN / DAT) ── GPIO32        G ──── ESP32 GND
   電源 (VCC / + / 中間) ── 3V3         R ──── ESP32 GPIO12
   地   (GND / −)       ── GND          Y ──── ESP32 GPIO14
```

- HW-483：`S` 是訊號、中間是電源、`−` 是地。
- 大功率模組：訊號一樣接 GPIO32；模組標示 **5V** 時電源改接 ESP32 的 **VIN（5V）**，標示 3.3V 或 3.3–5V 接 3V3。

- **發射模組一定要接 GPIO32**。ESP32 的 GPIO34–39 只能輸入，接上去不會發光。
- 檢查方法：用手機**前鏡頭**看發射 LED。平常不亮、按遙控指令時閃一下才是對的；一直亮代表 `S` 和 `中間` 接反了。
- 發射 LED 要**正對冷氣室內機的接收窗**。HW-483 約 1–2 m 內，大功率模組可以更遠。
- 接收模組是選配：接了之後，有人用原廠遙控器時，網頁上的冷氣狀態也會跟著更新。

---

## 3. 安裝樹莓派（從零開始）

### 3.1 燒錄系統到 SD 卡

1. 在電腦上安裝並打開 [Raspberry Pi Imager](https://www.raspberrypi.com/software/)。
2. 裝置選 **Raspberry Pi 5**，系統選 **Raspberry Pi OS (64-bit)**，儲存裝置選你的 SD 卡。
3. 按「下一步」→「編輯設定」：
   - 設定**主機名稱**（例如 `rpi-614`）、**使用者名稱與密碼**
   - 設定 **Wi-Fi**（或之後插網路線）
   - 「服務」分頁勾選 **開啟 SSH**（使用密碼登入）
4. 寫入完成後，把 SD 卡插進樹莓派，接上電源開機（第一次開機約 2–3 分鐘）。

### 3.2 連線到樹莓派

在同一個內網的電腦打開終端機（Windows 用 PowerShell）：

```bash
ssh 使用者名稱@主機名稱.local
```

例如 `ssh pi@rpi-614.local`，輸入密碼後就進到樹莓派了。以下指令都在樹莓派上執行。

### 3.3 開啟感測器需要的介面

```bash
sudo nano /boot/firmware/config.txt
```

在檔案**最後面**加上這三行，按 `Ctrl+O`、`Enter` 存檔，`Ctrl+X` 離開：

```text
dtparam=i2c_arm=on
dtparam=spi=on
dtoverlay=i2c2-pi5,pins_12_13
```

重新開機，等一分鐘後再用 3.2 的方式連回來：

```bash
sudo reboot
```

確認三個介面都有出現（三個都要列出來，沒有「No such file」）：

```bash
ls -l /dev/i2c-1 /dev/i2c-2 /dev/spidev0.0
```

### 3.4 下載程式

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/hsuehntuee/rpi_sensor.git
cd rpi_sensor
```

### 3.5 設定這台的名稱

```bash
cp .env.example .env
nano .env
```

只需要改最上面兩行，其他維持預設：

```dotenv
DEVICE_ID=rpi_agri_614_rpi0     # 這台的名稱，每台不同
AC_NAME=614 東元                 # 冷氣頁面顯示的名稱
```

`SERVER_URL` 預設是 `off`（資料只存在樹莓派）。之後有 Server 再改，見〈7. 上傳到 Server〉。

### 3.6 安裝並啟動

```bash
chmod +x start_edge.sh
./start_edge.sh
```

- 第一次執行會問是否安裝 Docker，輸入 `y`。裝完後**登出再登入**（`exit`，再用 3.2 連回來），然後再執行一次 `./start_edge.sh`。
- 第一次建置約 15–30 分鐘（視網路速度）。
- 完成後畫面會顯示儀表板網址並持續顯示紀錄，按 `Ctrl+C` 離開即可，**服務會繼續在背景執行，之後開機也會自動啟動**。

### 3.7 確認

查這台樹莓派的 IP：

```bash
hostname -I
```

在內網任何一台電腦或手機的瀏覽器打開 `http://<IP>:8080`（例如 `http://192.168.50.167:8080`）：

- 最上方「系統狀態」應該是綠色「系統正常」（還沒插 ESP32 時會顯示「冷氣遙控器未連線」，屬正常）。
- 5 分鐘內會出現第一筆溫濕度與照片。

---

## 4. 燒錄 ESP32（每塊只要做一次）

兩間房的冷氣不同，ESP32 的韌體各自寫死自己那台冷氣，**燒錄時選對房間**即可：

| 房間 | 冷氣 | Windows 燒錄檔 | 韌體設定 |
|---|---|---|---|
| 304D | 日立 RAS-36UK | `esp32_ir\flash_304D.bat` | `esp32_304D` |
| 614 | 東元 MS80IH | `esp32_ir\flash_614.bat` | `esp32_614` |

### 4.1 在 Windows 電腦燒錄（建議）

1. 電腦需安裝 [Python 3](https://www.python.org/downloads/)（安裝時勾選 **Add python.exe to PATH**）。
2. 下載這個專案（GitHub 頁面 → Code → Download ZIP，解壓縮）。
3. 用 USB 傳輸線把 ESP32 接到電腦（一次只接一塊）。
4. 點兩下對應房間的 `.bat`，等畫面顯示「完成」。第一次會自動安裝工具並編譯，約 10 分鐘；之後每塊約 30 秒。

燒錄失敗時畫面會列出常見原因：USB 線只能充電、沒裝 CH340/CP210x 驅動、需要按住板子上的 BOOT 鍵。

### 4.2 在樹莓派上燒錄（沒有 Windows 電腦時）

把 ESP32 用 USB 接到樹莓派，然後：

```bash
cd ~/rpi_sensor
docker compose stop edge-sensor                  # 先讓出 USB 序列埠
sudo apt install -y python3-venv                 # 只有第一次需要
python3 -m venv ~/pio && ~/pio/bin/pip install platformio   # 只有第一次需要
cd esp32_ir/firmware
~/pio/bin/pio run -e esp32_614 -t upload         # 304D 那台改成 esp32_304D
cd ~/rpi_sensor
docker compose start edge-sensor
```

### 4.3 裝到現場

燒好的 ESP32 照〈2.4〉接好發射模組，USB 插到該房間的樹莓派即可（隨插隨用，不需要重開機）。幾秒內，儀表板的冷氣卡片就會顯示目前設定。

---

## 5. 日常使用

| 網址 | 內容 |
|---|---|
| `http://<IP>:8080` | 儀表板：系統狀態、溫濕度/CO₂、冷氣、剩餘空間、最新照片、歷史紀錄 |
| `http://<IP>:8080/ac` | 冷氣遙控：開關、模式、溫度、風速、擺風 |
| `http://<IP>:8080/docs` | API 互動文件（給工程師） |

**系統狀態**（儀表板最上方）：

- **可自行處理**（黃色）：照畫面上的說明處理即可，例如拔插 ESP32 的 USB 線、重新開機。
- **需聯繫**（紅色）：相機或感測器故障、儲存空間快滿等，請**截圖這個區塊**聯繫維護人員。

**冷氣遙控注意事項**：紅外線是單向的，網頁顯示的是「最後送出的設定」。有人用原廠遙控器時，接了接收模組才會同步。304D 的日立冷氣電源鍵是「切換式」，如果網頁顯示的開關跟實際相反，再按一次網頁上的電源鍵即可。

**照片保留**：

- 有設定 Server 時，已上傳的照片在樹莓派保留 14 天（`IMAGE_RETENTION_DAYS`）。
- 剩餘空間低於 1.5 GB 時，會自動從最舊的照片開始刪，並在系統狀態示警。
- 128 GB 的卡不上傳約可存 250 天。

---

## 6. 給工程師：內網 API

每台樹莓派都是一個測站。內網中任何裝置都可以用 HTTP 讀資料、控制冷氣，用來寫自己的邏輯（例如溫度過高自動開冷氣、把資料存進自己的資料庫）。

- 網址：`http://<樹莓派IP>:8080`
- 不需要帳密。**只限內網使用，不要把 8080 port 開到網際網路上。**
- 已開啟 CORS，內網其他網頁可以直接用 JavaScript 呼叫。
- 互動文件：`http://<樹莓派IP>:8080/docs`，可以直接在網頁上試打每個 API。
- 時間一律是 UTC、ISO 8601 格式（例如 `2026-10-29T03:35:00Z`，台北時間要 +8 小時）。

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/health` | 服務是否正常、裝置名稱 |
| GET | `/api/v1/env/latest` | 最新一筆溫度 (°C)、濕度 (%)、CO₂ (ppm) |
| GET | `/api/v1/env?limit=100&since=2026-10-29T00:00:00+08:00` | 歷史紀錄（新到舊，每 5 分鐘一筆）。`since` 可省略 |
| GET | `/api/v1/images/latest/rgb` | 最新一張可見光照片（JPEG） |
| GET | `/api/v1/images/latest/ir` | 最新一張熱影像（JPEG） |
| GET | `/api/v1/images/list?limit=60&type=IR` | 照片清單（含網址，`type` 可省略或填 `RGB`/`IR`） |
| GET | `/api/v1/system/health` | 系統狀態示警、剩餘空間、冷氣遙控連線狀態 |
| GET | `/api/v1/ac` | 冷氣目前設定、ESP32 連線狀態、室溫 |
| POST | `/api/v1/ac` | 控制冷氣（見下方範例） |
| GET | `/api/v1/overview` | 儀表板用的總覽（各類筆數、最新資料） |

**用 curl 試**：

```bash
curl http://192.168.50.167:8080/api/v1/env/latest
curl -o ir.jpg http://192.168.50.167:8080/api/v1/images/latest/ir
curl -X POST http://192.168.50.167:8080/api/v1/ac \
     -H "Content-Type: application/json" \
     -d '{"power": true, "mode": "cool", "temp": 26, "fan": "auto", "swing": false}'
```

**Python 範例**（需 `pip install requests`）：

```python
import requests

PI = "http://192.168.50.167:8080"

# 讀最新溫濕度
env = requests.get(f"{PI}/api/v1/env/latest", timeout=5).json()
print(env["temperature"], env["humidity"], env["co2_ppm"])

# 讀今天以來的歷史紀錄
history = requests.get(f"{PI}/api/v1/env",
                       params={"since": "2026-10-29T00:00:00+08:00", "limit": 1000}, timeout=10).json()
for row in history["data"]:
    print(row["timestamp"], row["temperature"])

# 下載最新熱影像
with open("ir.jpg", "wb") as f:
    f.write(requests.get(f"{PI}/api/v1/images/latest/ir", timeout=10).content)

# 太熱就開冷氣 26 度
if env["temperature"] > 28:
    requests.post(f"{PI}/api/v1/ac",
                  json={"power": True, "mode": "cool", "temp": 26, "fan": "auto"}, timeout=10)
```

**冷氣 API 的欄位**（每次都送完整狀態，跟原廠遙控器一樣）：

| 欄位 | 值 |
|---|---|
| `power` | `true` / `false` |
| `mode` | `cool` 冷氣 / `dry` 除濕 / `fan` 送風 / `heat` 暖氣 / `auto` 自動 |
| `temp` | 16–31 |
| `fan` | `auto` / `low` / `medium` / `high` |
| `swing` | `true` / `false` |

各冷氣的支援程度：304D 日立只有冷氣/除濕/送風，風速只有弱/中/強，沒有擺風；614 東元全部支援。

成功時回傳 200 與送出後的狀態。ESP32 沒接或沒回應時回傳 502，`detail` 欄位有原因。

---

## 7. 上傳到 Server（選用）

1. 照 [server/README.md](server/README.md) 架好 Server。
2. 在每台樹莓派的 `.env` 設定：

   ```dotenv
   SERVER_URL=http://<Server的IP>:8000
   API_KEY=<與 Server 的 .env 相同的 API_KEY>
   ```

3. 套用設定（不用重新建置）：

   ```bash
   cd ~/rpi_sensor
   docker compose up -d
   ```

之前存在樹莓派的資料會自動逐步補傳。資料量大時需要一段時間，例如 3 萬張照片約一天。

---

## 8. 更新程式

```bash
cd ~/rpi_sensor
git pull
./start_edge.sh
```

只改了 `.env` 的話，執行 `docker compose up -d` 即可（幾秒鐘）。

---

## 9. 疑難排解

| 狀況 | 處理 |
|---|---|
| 瀏覽器打不開儀表板 | 確認電腦和樹莓派在同一個內網；在樹莓派上執行 `docker compose ps`，狀態應為 `Up` |
| 「冷氣遙控器 (ESP32) 未連線」 | 拔插 ESP32 的 USB 線；換一條能傳資料的線；仍不行就重新開機 |
| 網頁顯示已送出，但冷氣沒反應 | 發射頭沒對準或太遠；用手機前鏡頭看發射頭，送指令時應閃一下；確認 ESP32 燒的是這間房的韌體 |
| 發射頭一直亮 | 訊號和電源接反了，照 2.4 重接 |
| `ls /dev/serial/by-id/` 沒有 ESP32 | `dmesg \| tail -20` 若看到 `brltty`，執行 `sudo apt remove brltty` 後重插 USB |
| 溫濕度/相機「沒有新資料」 | 檢查排線與杜邦線，重新開機；確認 3.3 的三個介面都存在。完整硬體檢查：`./test.sh`（結果存在 `test_report.log`） |
| 想看詳細紀錄 | `.env` 改 `LOG_LEVEL=INFO` → `docker compose up -d` → `docker compose logs -f edge-sensor`；看完改回 `WARNING` |

重新開機：`sudo reboot`。服務會自動啟動。

---

## 10. 專案結構（開發用）

| 路徑 | 內容 |
|---|---|
| `src/main.py` | 主程式：排程取樣、拍照、上傳 |
| `src/web/app.py` | 儀表板、冷氣頁面與 API |
| `src/web/health.py` | 系統狀態檢查（示警規則） |
| `src/control/ir_ac.py` | 透過 USB 序列埠控制 ESP32 |
| `src/sensors/` | SCD41、相機、Lepton 驅動 |
| `src/db/` | 本機 SQLite 與上傳 |
| `esp32_ir/firmware/` | ESP32 韌體（PlatformIO，IRremoteESP8266） |
| `server/` | 選用的資料收集 Server |
| `tests/` | 單元測試：`docker compose run --rm --entrypoint "python -m pytest" edge-sensor` |
| `test.sh`、`tests/hw_camera_check.py`、`tests/verify_ir.py` | 樹莓派上的硬體檢查工具（I2C/SPI、相機、Lepton） |

**冷氣協定備忘**（現場抓碼結果，2026-10-29）：

- 304D 日立：`HITACHI_AC1`，遙控器型號 B。電源是「切換」位元，韌體照原廠遙控器的格式組訊號。
- 614 東元：`ELECTRA_AC`。訊號帶「按了哪顆鍵」的鍵碼，韌體依改變的項目自動填入。

要支援其他冷氣，需先用紅外線接收頭抓原廠遙控器的訊號分析，再在 `esp32_ir/firmware/platformio.ini` 新增一個設定。
