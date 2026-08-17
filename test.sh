#!/usr/bin/env bash
# ==============================================================================
# 🍓 304D 邊緣節點硬體與相機全面診斷測試腳本 (test.sh)
# ==============================================================================
# 用途：檢測樹莓派 5 核心設定、I2C/SPI 硬體、IMX500 可見光相機、FLIR Lepton 3.5 紅外線相機
# 執行方式：./test.sh 或 bash test.sh
# ==============================================================================
set -u

REPORT_FILE="test_report.log"
exec > >(tee "$REPORT_FILE") 2>&1

echo "======================================================================"
echo "🍓 樹莓派邊緣節點 (Edge Sensor Node) 全面診斷測試"
echo "測試時間: $(date '+%Y-%m-%d %H:%M:%S (%Z)')"
echo "======================================================================"

# ── 1. 系統與核心環境檢查 ──────────────────────────────────────────────
echo -e "\n🔍 [1/6] 系統與 Linux 核心檢查："
echo "   - 主機名稱: $(hostname)"
echo "   - 核心版本: $(uname -r) ($(uname -m))"
echo "   - 系統時間: $(date)"

CMDLINE="/boot/firmware/cmdline.txt"
[ ! -f "$CMDLINE" ] && CMDLINE="/boot/cmdline.txt"

if [ -f "$CMDLINE" ]; then
    if grep -q "spidev.bufsiz=65536" "$CMDLINE"; then
        echo "   - SPI 核心緩衝區: ✅ 已設定 (spidev.bufsiz=65536)"
    else
        echo "   - SPI 核心緩衝區: ❌ 未設定 (缺少 spidev.bufsiz=65536，Lepton 3.5 易逾時)"
        echo "     👉 修正方式：sudo sed -i '\$s/\$/ spidev.bufsiz=65536/' $CMDLINE && sudo reboot"
    fi
fi

# ── 2. I2C 與 SPI 硬體匯流排檢測 ──────────────────────────────────────
echo -e "\n🔍 [2/6] I2C 與 SPI 硬體匯流排檢測："

if [ -e "/dev/spidev0.0" ]; then
    echo "   - SPI 裝置 (/dev/spidev0.0): ✅ 存在 ($(ls -l /dev/spidev0.0 | awk '{print $1, $3, $4}'))"
else
    echo "   - SPI 裝置 (/dev/spidev0.0): ❌ 找不到裝置 (請確認 raspi-config 已啟用 SPI)"
fi

if [ -e "/dev/i2c-1" ]; then
    echo "   - I2C 匯流排 (/dev/i2c-1): ✅ 存在"
    echo "   - I2C 掃描 (2a=Lepton 3.5, 62=SCD41):"
    i2cdetect -y 1 2>/dev/null | grep -E "20:|60:" || echo "     (無法完成掃描)"
else
    echo "   - I2C 匯流排 (/dev/i2c-1): ❌ 找不到裝置"
fi

# ── 3. Host 可見光相機 (IMX500 / PiCamera) 檢測 ────────────────────────
echo -e "\n🔍 [3/6] 可見光相機 (Host 端硬體測試)："
if command -v rpicam-still &>/dev/null; then
    echo "   - 偵測相機列表："
    rpicam-still --list-cameras 2>/dev/null || true
    
    echo "   - 執行 Host 實體拍照測試 (/tmp/test_host_rgb.jpg)..."
    if rpicam-still --nopreview -t 1000 -o /tmp/test_host_rgb.jpg 2>/dev/null; then
        RGB_SIZE=$(ls -lh /tmp/test_host_rgb.jpg | awk '{print $5}')
        echo "   - Host 可見光相機拍照: ✅ 成功 (檔案大小: $RGB_SIZE)"
    else
        echo "   - Host 可見光相機拍照: ❌ 失敗 (請檢查排線與 CSI 連接埠)"
    fi
else
    echo "   - rpicam-still 指令: ⚠️ 未安裝於 Host"
fi

# ── 4. Docker 容器狀態檢查 ──────────────────────────────────────────
echo -e "\n🔍 [4/6] Docker 容器狀態檢測："
if command -v docker &>/dev/null; then
    if docker compose ps 2>/dev/null | grep -q "edge-sensor"; then
        CONTAINER_STATUS=$(docker compose ps --format "{{.Status}}" 2>/dev/null || docker compose ps | grep edge-sensor)
        echo "   - edge-sensor 容器狀態: ✅ 執行中 ($CONTAINER_STATUS)"
    else
        echo "   - edge-sensor 容器狀態: ⚠️ 未在執行中，嘗試啟動..."
        docker compose up -d
        sleep 5
    fi
else
    echo "   - Docker: ❌ 未安裝"
fi

# ── 5. Docker 容器內部功能診斷測試 ─────────────────────────────────────
echo -e "\n🔍 [5/6] 容器內部相機與感測器功能測試："

if docker compose ps 2>/dev/null | grep -q "edge-sensor"; then
    echo "   - 正在觸發容器內手動拍照與取樣..."
    docker compose exec edge-sensor python -c "
import sys
from pathlib import Path
from src.config import load_settings
from src.db.local_sqlite import LocalDatabase

settings = load_settings()
db = LocalDatabase(settings.database_path)

print('[Container Test] 1. 測試 SCD41...')
try:
    from src.sensors.scd41 import SCD41Sensor
    # 快速檢查
except Exception as e:
    print('  SCD41 check:', e)

print('[Container Test] 2. 測試 RGB 相機...')
try:
    from src.sensors.camera_rgb import PiCamera
    rgb = PiCamera(Path('/tmp/test_images'), camera_index=0)
    p = rgb.capture()
    print('  ✅ RGB 相機拍攝成功! 檔案:', p, '大小:', p.stat().st_size, 'bytes')
except Exception as e:
    print('  ❌ RGB 相機拍攝失敗:', e)

print('[Container Test] 3. 測試 FLIR Lepton 3.5 紅外線相機...')
try:
    from src.sensors.camera_ir import PiIRCamera
    ir = PiIRCamera(Path('/tmp/test_images'), spi_bus=0, spi_device=0, width=160, height=120)
    p = ir.capture()
    print('  ✅ FLIR Lepton 3.5 拍攝成功! 檔案:', p, '大小:', p.stat().st_size, 'bytes')
except Exception as e:
    print('  ❌ FLIR Lepton 3.5 拍攝失敗:', e)

print('[Container Test] 4. 最新資料庫記錄 (確認時間格式)...')
try:
    records = db.get_latest_env(limit=2)
    for r in records:
        print('  ENV Record ID #', r['id'], 'UTC Timestamp in DB:', r['timestamp'])
except Exception as e:
    print('  DB check:', e)
" 2>&1 || true
else
    echo "   - 容器未啟動，略過內部功能測試"
fi

# ── 6. 容器最近 Log 摘要 ──────────────────────────────────────────────
echo -e "\n🔍 [6/6] Docker 最近 Log 摘要 (最新 25 行)："
echo "----------------------------------------------------------------------"
docker compose logs --tail=25 edge-sensor 2>/dev/null || true
echo "----------------------------------------------------------------------"

echo -e "\n======================================================================"
echo "📊 測試診斷完畢！"
echo "詳細日誌已儲存至: $(pwd)/$REPORT_FILE"
echo "若有任何問題，請將 $REPORT_FILE 檔案內容回傳以利快速排查。"
echo "======================================================================"
