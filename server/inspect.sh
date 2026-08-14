#!/usr/bin/env bash
# ==============================================================================
# 遠端 Server 資料庫與圖片 (RGB & IR) 快速檢視腳本 (Quick Server Inspector)
# ==============================================================================
set -euo pipefail

# 顏色定義
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}======================================================${NC}"
echo -e "${GREEN} 🚀 RPi Sensor Server 即時數據與影像狀態檢視器 ${NC}"
echo -e "${CYAN}======================================================${NC}"

# 1. 檢查 Docker 服務是否正常運行
if command -v docker &>/dev/null && docker compose ps &>/dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    echo -e "${YELLOW}[!] 找不到 docker compose 指令，請確認 Docker 環境${NC}"
    exit 1
fi

echo -e "\n${BLUE}📊 [1] 資料庫各資料表記錄總數與最新時間 (GMT+8 台北時間)：${NC}"
$COMPOSE_CMD exec -T db psql -U rpi_sensor -d rpi_sensor -c "
SELECT 
    'env_metrics (環境感測)' AS table_name, 
    COUNT(*) AS total_records, 
    COALESCE(TO_CHAR(MAX(timestamp AT TIME ZONE 'Asia/Taipei'), 'YYYY-MM-DD HH24:MI:SS (GMT+8)'), '無資料') AS latest_record
FROM env_metrics
UNION ALL
SELECT 
    'hvac_status (冷氣能耗)', 
    COUNT(*), 
    COALESCE(TO_CHAR(MAX(timestamp AT TIME ZONE 'Asia/Taipei'), 'YYYY-MM-DD HH24:MI:SS (GMT+8)'), '無資料')
FROM hvac_status
UNION ALL
SELECT 
    'camera_logs (RGB 可見光)', 
    COUNT(*), 
    COALESCE(TO_CHAR(MAX(timestamp AT TIME ZONE 'Asia/Taipei'), 'YYYY-MM-DD HH24:MI:SS (GMT+8)'), '無資料')
FROM camera_logs WHERE image_type = 'RGB'
UNION ALL
SELECT 
    'camera_logs (IR 熱影像)', 
    COUNT(*), 
    COALESCE(TO_CHAR(MAX(timestamp AT TIME ZONE 'Asia/Taipei'), 'YYYY-MM-DD HH24:MI:SS (GMT+8)'), '無資料')
FROM camera_logs WHERE image_type = 'IR';
" || true

echo -e "\n${BLUE}🌡️ [2] 最新 5 筆 SCD41 環境數據 (溫度 / 濕度 / CO2) (GMT+8 台北時間)：${NC}"
$COMPOSE_CMD exec -T db psql -U rpi_sensor -d rpi_sensor -c "
SELECT id, device_id, TO_CHAR(timestamp AT TIME ZONE 'Asia/Taipei', 'YYYY-MM-DD HH24:MI:SS (GMT+8)') AS time, 
       ROUND(temperature::numeric, 2) AS temp_c, 
       ROUND(humidity::numeric, 2) AS hum_pct, 
       co2_ppm 
FROM env_metrics 
ORDER BY timestamp DESC 
LIMIT 5;
" || true

echo -e "\n${BLUE}❄️ [3] 最新 5 筆 HVAC 冷氣狀態紀錄 (GMT+8 台北時間)：${NC}"
$COMPOSE_CMD exec -T db psql -U rpi_sensor -d rpi_sensor -c "
SELECT id, device_id, TO_CHAR(timestamp AT TIME ZONE 'Asia/Taipei', 'YYYY-MM-DD HH24:MI:SS (GMT+8)') AS time, 
       hvac_state, 
       power_w 
FROM hvac_status 
ORDER BY timestamp DESC 
LIMIT 5;
" || true

echo -e "\n${BLUE}📸 [4] 最新 10 筆 RGB 與 IR 相機照片上傳紀錄 (GMT+8 台北時間)：${NC}"
$COMPOSE_CMD exec -T db psql -U rpi_sensor -d rpi_sensor -c "
SELECT id, device_id, TO_CHAR(timestamp AT TIME ZONE 'Asia/Taipei', 'YYYY-MM-DD HH24:MI:SS (GMT+8)') AS time, 
       image_type, 
       ROUND((size_bytes / 1024.0)::numeric, 1) || ' KB' AS size,
       file_path 
FROM camera_logs 
ORDER BY timestamp DESC 
LIMIT 10;
" || true

echo -e "\n${BLUE}📁 [5] 實體硬碟儲存目錄 (storage/images) 最近圖片檔案：${NC}"
if [ -d "storage/images" ]; then
    find storage/images/ -type f \( -name "*.jpg" -o -name "*.png" \) 2>/dev/null | xargs -r ls -lht | head -n 8 || true
    echo -e "${CYAN}磁碟空間使用量：${NC} $(du -sh storage/images/ 2>/dev/null || echo '0B')"
else
    echo -e "${YELLOW}尚未建立 storage/images 目錄（等待邊緣端首次上傳圖片）${NC}"
fi

echo -e "\n${GREEN}======================================================${NC}"
echo -e "${GREEN}🌐 瀏覽器即時視覺化儀表板 (Web Dashboard)：${NC}"
echo -e "👉 請在瀏覽器開啟: ${CYAN}http://<伺服器IP>:8000/dashboard${NC}"
echo -e "👉 即時查看雙相機照片: ${CYAN}http://<伺服器IP>:8000/dashboard${NC} 或 ${CYAN}http://<伺服器IP>:8000/api/v1/images/list${NC}"
echo -e "${GREEN}======================================================${NC}"
