from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .models import CameraLog, EnvMetric, HVACStatus
from .schemas import ImageResult, MetricsSyncIn, SyncResult
from .security import require_api_key

from typing import Any
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Raspberry Pi Sensor Server", version="1.0.0")
SessionDependency = Annotated[Session, Depends(get_session)]
AuthDependency = Annotated[None, Depends(require_api_key)]

# Mount static images directory for instant browser viewing
app.mount("/static/images", StaticFiles(directory="/data/images"), name="images")


@app.get("/health")
def health(session: SessionDependency) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/v1/overview")
def get_system_overview(session: SessionDependency) -> dict[str, Any]:
    """取得資料庫各表最新統計與最新 10 筆數據"""
    # 統計總數
    env_count = session.scalar(select(text("COUNT(*) FROM env_metrics"))) or 0
    hvac_count = session.scalar(select(text("COUNT(*) FROM hvac_status"))) or 0
    rgb_count = session.scalar(select(text("COUNT(*) FROM camera_logs WHERE image_type = 'RGB'"))) or 0
    ir_count = session.scalar(select(text("COUNT(*) FROM camera_logs WHERE image_type = 'IR'"))) or 0

    # 最新環境數據
    latest_env = session.scalars(
        select(EnvMetric).order_by(EnvMetric.timestamp.desc()).limit(15)
    ).all()

    # 最新 HVAC 狀態
    latest_hvac = session.scalars(
        select(HVACStatus).order_by(HVACStatus.timestamp.desc()).limit(15)
    ).all()

    # 最新照片記錄 (RGB & IR)
    latest_rgb = session.scalars(
        select(CameraLog).where(CameraLog.image_type == "RGB").order_by(CameraLog.timestamp.desc()).limit(10)
    ).all()
    latest_ir = session.scalars(
        select(CameraLog).where(CameraLog.image_type == "IR").order_by(CameraLog.timestamp.desc()).limit(10)
    ).all()

    def _fmt_cam(cam: CameraLog) -> dict[str, Any]:
        url = cam.file_path.replace("/data/images/", "/static/images/") if cam.file_path.startswith("/data/images/") else f"/static/images/{cam.file_path}"
        return {
            "id": cam.id,
            "device_id": cam.device_id,
            "timestamp": cam.timestamp.isoformat(),
            "image_type": cam.image_type,
            "file_path": cam.file_path,
            "url": url,
            "size_bytes": cam.size_bytes,
        }

    return {
        "counts": {
            "env_metrics": env_count,
            "hvac_status": hvac_count,
            "camera_rgb": rgb_count,
            "camera_ir": ir_count,
        },
        "latest_env": [
            {
                "id": e.id,
                "device_id": e.device_id,
                "timestamp": e.timestamp.isoformat(),
                "temperature": e.temperature,
                "humidity": e.humidity,
                "co2_ppm": e.co2_ppm,
            }
            for e in latest_env
        ],
        "latest_hvac": [
            {
                "id": h.id,
                "device_id": h.device_id,
                "timestamp": h.timestamp.isoformat(),
                "hvac_state": h.hvac_state,
                "power_w": h.power_w,
            }
            for h in latest_hvac
        ],
        "latest_rgb_images": [_fmt_cam(c) for c in latest_rgb],
        "latest_ir_images": [_fmt_cam(c) for c in latest_ir],
    }


@app.get("/api/v1/images/list")
def list_uploaded_images() -> list[dict[str, Any]]:
    base = get_settings().image_storage_path
    images = []
    if base.exists():
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                rel_path = path.relative_to(base).as_posix()
                images.append({
                    "name": path.name,
                    "relative_path": rel_path,
                    "url": f"/static/images/{rel_path}",
                    "size_bytes": path.stat().st_size,
                    "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
    return sorted(images, key=lambda item: item["mtime"], reverse=True)


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/view", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def serve_dashboard() -> str:
    return """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RPi 邊緣感測與雙相機即時儀表板</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-main: #0f172a;
            --bg-card: #1e293b;
            --bg-card-hover: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-blue: #38bdf8;
            --accent-green: #34d399;
            --accent-orange: #fb923c;
            --accent-red: #f87171;
            --accent-purple: #c084fc;
            --border-color: #334155;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-main);
            color: var(--text-primary);
            padding: 20px;
            min-height: 100vh;
        }
        .container { max-width: 1380px; margin: 0 auto; }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 24px;
        }
        h1 { font-size: 1.5rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .badge-live {
            background: rgba(52, 211, 153, 0.15);
            color: var(--accent-green);
            padding: 4px 10px;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }
        .badge-live::before {
            content: "";
            width: 8px;
            height: 8px;
            background: var(--accent-green);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 8px var(--accent-green);
        }
        .controls { display: flex; gap: 12px; align-items: center; }
        .btn {
            background: #2563eb;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.875rem;
            transition: background 0.2s;
        }
        .btn:hover { background: #1d4ed8; }
        .btn-outline {
            background: transparent;
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
        }
        .btn-outline:hover { background: var(--bg-card); color: var(--text-primary); }

        /* Stats Grid */
        .grid-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .stat-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .stat-title { font-size: 0.85rem; color: var(--text-secondary); font-weight: 500; }
        .stat-val { font-size: 1.75rem; font-weight: 700; }
        .stat-sub { font-size: 0.75rem; color: var(--text-secondary); }

        /* Cameras Section */
        .section-title {
            font-size: 1.15rem;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .camera-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 28px;
        }
        @media (max-width: 768px) {
            .camera-grid { grid-template-columns: 1fr; }
        }
        .camera-card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .camera-header {
            padding: 12px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(15, 23, 42, 0.6);
            border-bottom: 1px solid var(--border-color);
        }
        .camera-title { font-weight: 600; font-size: 0.95rem; display: flex; align-items: center; gap: 8px; }
        .camera-meta { font-size: 0.8rem; color: var(--text-secondary); }
        .camera-img-box {
            width: 100%;
            height: 380px;
            background: #090d16;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }
        .camera-img-box img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            cursor: pointer;
            transition: transform 0.2s;
        }
        .camera-img-box img:hover { transform: scale(1.02); }
        .camera-footer {
            padding: 10px 16px;
            display: flex;
            justify-content: space-between;
            font-size: 0.8rem;
            color: var(--text-secondary);
            border-top: 1px solid var(--border-color);
        }
        .camera-footer a { color: var(--accent-blue); text-decoration: none; }
        .camera-footer a:hover { text-decoration: underline; }

        /* Tables & Tabs */
        .tabs { display: flex; gap: 8px; margin-bottom: 16px; border-bottom: 1px solid var(--border-color); padding-bottom: 8px; }
        .tab-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.9rem;
        }
        .tab-btn.active { background: var(--bg-card); color: var(--text-primary); }
        .table-wrap {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow-x: auto;
        }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.875rem; }
        th { background: rgba(15, 23, 42, 0.4); padding: 12px 16px; color: var(--text-secondary); font-weight: 600; border-bottom: 1px solid var(--border-color); }
        td { padding: 12px 16px; border-bottom: 1px solid rgba(51, 65, 85, 0.5); }
        tr:last-child td { border-bottom: none; }
        tr:hover { background: rgba(51, 65, 85, 0.2); }

        /* Gallery Grid */
        .gallery-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 12px;
            padding: 16px;
        }
        .gallery-item {
            background: #090d16;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .gallery-item img {
            width: 100%;
            height: 130px;
            object-fit: cover;
            cursor: pointer;
        }
        .gallery-meta { padding: 6px 10px; font-size: 0.75rem; color: var(--text-secondary); display: flex; justify-content: space-between; }

        /* Modal */
        .modal {
            display: none;
            position: fixed;
            z-index: 999;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.85);
            align-items: center;
            justify-content: center;
            cursor: pointer;
        }
        .modal.open { display: flex; }
        .modal img { max-width: 92vw; max-height: 90vh; border-radius: 8px; box-shadow: 0 0 30px rgba(0,0,0,0.8); }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>🍓 RPi 5 邊緣環境與雙相機感測系統</h1>
                <div style="margin-top: 6px; display: flex; gap: 12px; align-items: center;">
                    <span class="badge-live">系統正常運行 (Live)</span>
                    <span id="lastUpdated" style="font-size: 0.8rem; color: var(--text-secondary);">更新中...</span>
                </div>
            </div>
            <div class="controls">
                <button class="btn btn-outline" id="btnToggleAuto" onclick="toggleAutoRefresh()">自動更新: 15s</button>
                <button class="btn" onclick="fetchData()">手動重新整理</button>
            </div>
        </header>

        <!-- Stats Overview Cards -->
        <div class="grid-stats">
            <div class="stat-card">
                <div class="stat-title">🌡️ SCD41 最新溫度</div>
                <div class="stat-val" id="statTemp" style="color: var(--accent-orange);">-- °C</div>
                <div class="stat-sub" id="statTempTime">等待取樣...</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">💧 SCD41 最新濕度</div>
                <div class="stat-val" id="statHum" style="color: var(--accent-blue);">-- %</div>
                <div class="stat-sub" id="statHumTime">等待取樣...</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">🌿 CO2 二氧化碳濃度</div>
                <div class="stat-val" id="statCo2" style="color: var(--accent-green);">-- ppm</div>
                <div class="stat-sub" id="statCo2Status">正常範圍</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">❄️ HVAC 冷氣狀態 / 功率</div>
                <div class="stat-val" id="statHvac">--</div>
                <div class="stat-sub" id="statHvacPower">0.0 W</div>
            </div>
            <div class="stat-card">
                <div class="stat-title">📦 資料庫總筆數</div>
                <div class="stat-val" id="statTotal" style="color: var(--accent-purple);">0 筆</div>
                <div class="stat-sub" id="statCamCount">RGB: 0 | IR: 0</div>
            </div>
        </div>

        <!-- Latest Dual Camera Side-by-Side -->
        <div class="section-title">
            <span>📷 最新雙相機拍攝畫面 (RGB & FLIR Lepton IR 熱影像)</span>
            <span style="font-size: 0.85rem; color: var(--text-secondary);">每 5 分鐘整點準時拍攝採樣</span>
        </div>
        <div class="camera-grid">
            <!-- RGB Camera Card -->
            <div class="camera-card">
                <div class="camera-header">
                    <span class="camera-title">🟢 RGB 可見光相機 (PiCamera)</span>
                    <span class="camera-meta" id="rgbTime">--</span>
                </div>
                <div class="camera-img-box">
                    <img id="rgbImg" src="" alt="尚無 RGB 影像" onclick="openModal(this.src)">
                </div>
                <div class="camera-footer">
                    <span id="rgbSize">大小: --</span>
                    <a id="rgbLink" href="#" target="_blank">🔍 點擊查看原圖</a>
                </div>
            </div>

            <!-- IR Camera Card -->
            <div class="camera-card">
                <div class="camera-header">
                    <span class="camera-title">🔴 FLIR Lepton IR 熱感應影像</span>
                    <span class="camera-meta" id="irTime">--</span>
                </div>
                <div class="camera-img-box">
                    <img id="irImg" src="" alt="尚無 IR 影像" onclick="openModal(this.src)">
                </div>
                <div class="camera-footer">
                    <span id="irSize">大小: --</span>
                    <a id="irLink" href="#" target="_blank">🔍 點擊查看原圖</a>
                </div>
            </div>
        </div>

        <!-- Tables & History Tabs -->
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('env')">📊 環境感測記錄 (SCD41)</button>
            <button class="tab-btn" onclick="switchTab('hvac')">❄️ HVAC 冷氣記錄</button>
            <button class="tab-btn" onclick="switchTab('gallery')">🖼️ 歷史相片藝廊</button>
        </div>

        <div id="tabEnv" class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>裝置 ID</th>
                        <th>取樣時間 (UTC)</th>
                        <th>溫度 (°C)</th>
                        <th>濕度 (%)</th>
                        <th>CO2 (ppm)</th>
                    </tr>
                </thead>
                <tbody id="envTableBody">
                    <tr><td colspan="6" style="text-align: center; color: var(--text-secondary);">載入中...</td></tr>
                </tbody>
            </table>
        </div>

        <div id="tabHvac" class="table-wrap" style="display: none;">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>裝置 ID</th>
                        <th>記錄時間 (UTC)</th>
                        <th>運轉狀態</th>
                        <th>耗電功率 (W)</th>
                    </tr>
                </thead>
                <tbody id="hvacTableBody">
                    <tr><td colspan="5" style="text-align: center; color: var(--text-secondary);">載入中...</td></tr>
                </tbody>
            </table>
        </div>

        <div id="tabGallery" class="table-wrap" style="display: none;">
            <div class="gallery-grid" id="galleryContainer">
                <div style="grid-column: 1/-1; text-align: center; color: var(--text-secondary); padding: 20px;">載入相片中...</div>
            </div>
        </div>
    </div>

    <!-- Image Zoom Modal -->
    <div id="imgModal" class="modal" onclick="closeModal()">
        <img id="modalImg" src="">
    </div>

    <script>
        let autoRefreshInterval = null;
        let isAuto = true;

        async function fetchData() {
            try {
                const res = await fetch('/api/v1/overview');
                if (!res.ok) throw new Error('API request failed');
                const data = await res.json();
                renderOverview(data);

                const imgRes = await fetch('/api/v1/images/list');
                if (imgRes.ok) {
                    const imgList = await imgRes.json();
                    renderGallery(imgList);
                }

                document.getElementById('lastUpdated').textContent = '最後更新: ' + new Date().toLocaleTimeString();
            } catch (err) {
                console.error(err);
                document.getElementById('lastUpdated').textContent = '連線失敗: ' + err.message;
            }
        }

        function renderOverview(data) {
            // Stats
            const totalRecords = (data.counts.env_metrics || 0) + (data.counts.hvac_status || 0) + (data.counts.camera_rgb || 0) + (data.counts.camera_ir || 0);
            document.getElementById('statTotal').textContent = totalRecords + ' 筆';
            document.getElementById('statCamCount').textContent = `RGB: ${data.counts.camera_rgb || 0} | IR: ${data.counts.camera_ir || 0}`;

            if (data.latest_env && data.latest_env.length > 0) {
                const latest = data.latest_env[0];
                document.getElementById('statTemp').textContent = (latest.temperature != null ? latest.temperature.toFixed(1) : '--') + ' °C';
                document.getElementById('statHum').textContent = (latest.humidity != null ? latest.humidity.toFixed(1) : '--') + ' %';
                document.getElementById('statCo2').textContent = (latest.co2_ppm != null ? latest.co2_ppm : '--') + ' ppm';
                document.getElementById('statTempTime').textContent = formatTime(latest.timestamp);
                document.getElementById('statHumTime').textContent = formatTime(latest.timestamp);
                
                if (latest.co2_ppm > 1000) {
                    document.getElementById('statCo2Status').textContent = '⚠️ 濃度偏高，建議通風';
                    document.getElementById('statCo2Status').style.color = 'var(--accent-red)';
                } else {
                    document.getElementById('statCo2Status').textContent = '🌿 空氣品質良好';
                    document.getElementById('statCo2Status').style.color = 'var(--accent-green)';
                }
            }

            if (data.latest_hvac && data.latest_hvac.length > 0) {
                const latestHvac = data.latest_hvac[0];
                const stateStr = latestHvac.hvac_state === 1 ? '運轉中 (ON)' : (latestHvac.hvac_state === 0 ? '關閉 (OFF)' : '離線/未設定');
                document.getElementById('statHvac').textContent = stateStr;
                document.getElementById('statHvacPower').textContent = (latestHvac.power_w != null ? latestHvac.power_w.toFixed(1) : '0.0') + ' W';
            }

            // Latest RGB & IR
            if (data.latest_rgb_images && data.latest_rgb_images.length > 0) {
                const rgb = data.latest_rgb_images[0];
                document.getElementById('rgbImg').src = rgb.url;
                document.getElementById('rgbLink').href = rgb.url;
                document.getElementById('rgbTime').textContent = formatTime(rgb.timestamp);
                document.getElementById('rgbSize').textContent = `大小: ${(rgb.size_bytes / 1024).toFixed(1)} KB`;
            }

            if (data.latest_ir_images && data.latest_ir_images.length > 0) {
                const ir = data.latest_ir_images[0];
                document.getElementById('irImg').src = ir.url;
                document.getElementById('irLink').href = ir.url;
                document.getElementById('irTime').textContent = formatTime(ir.timestamp);
                document.getElementById('irSize').textContent = `大小: ${(ir.size_bytes / 1024).toFixed(1)} KB`;
            }

            // Tables
            const envTbody = document.getElementById('envTableBody');
            if (data.latest_env && data.latest_env.length > 0) {
                envTbody.innerHTML = data.latest_env.map(e => `
                    <tr>
                        <td>#${e.id}</td>
                        <td>${e.device_id}</td>
                        <td>${formatTime(e.timestamp)}</td>
                        <td><strong>${e.temperature != null ? e.temperature.toFixed(2) : '-'}</strong></td>
                        <td><strong>${e.humidity != null ? e.humidity.toFixed(2) : '-'}</strong></td>
                        <td><strong>${e.co2_ppm != null ? e.co2_ppm : '-'}</strong></td>
                    </tr>
                `).join('');
            } else {
                envTbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-secondary);">目前尚無環境感測數據</td></tr>';
            }

            const hvacTbody = document.getElementById('hvacTableBody');
            if (data.latest_hvac && data.latest_hvac.length > 0) {
                hvacTbody.innerHTML = data.latest_hvac.map(h => `
                    <tr>
                        <td>#${h.id}</td>
                        <td>${h.device_id}</td>
                        <td>${formatTime(h.timestamp)}</td>
                        <td>${h.hvac_state === 1 ? '🟢 運轉中' : (h.hvac_state === 0 ? '⚪ 關閉' : '⚠️ 未連線')}</td>
                        <td>${h.power_w != null ? h.power_w.toFixed(1) + ' W' : '-'}</td>
                    </tr>
                `).join('');
            } else {
                hvacTbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-secondary);">目前尚無 HVAC 記錄</td></tr>';
            }
        }

        function renderGallery(images) {
            const container = document.getElementById('galleryContainer');
            if (!images || images.length === 0) {
                container.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-secondary); padding: 20px;">尚無上傳照片</div>';
                return;
            }
            container.innerHTML = images.map(img => `
                <div class="gallery-item">
                    <img src="${img.url}" alt="${img.name}" onclick="openModal('${img.url}')">
                    <div class="gallery-meta">
                        <span>${img.name.includes('_IR_') ? '🔴 IR' : '🟢 RGB'}</span>
                        <span>${(img.size_bytes / 1024).toFixed(0)}KB</span>
                    </div>
                </div>
            `).join('');
        }

        function formatTime(isoStr) {
            if (!isoStr) return '--';
            const d = new Date(isoStr);
            return isNaN(d) ? isoStr : d.toLocaleString('zh-TW', { hour12: false });
        }

        function switchTab(tab) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('tabEnv').style.display = tab === 'env' ? 'block' : 'none';
            document.getElementById('tabHvac').style.display = tab === 'hvac' ? 'block' : 'none';
            document.getElementById('tabGallery').style.display = tab === 'gallery' ? 'block' : 'none';
            event.target.classList.add('active');
        }

        function openModal(src) {
            if (!src) return;
            document.getElementById('modalImg').src = src;
            document.getElementById('imgModal').classList.add('open');
        }

        function closeModal() {
            document.getElementById('imgModal').classList.remove('open');
        }

        function toggleAutoRefresh() {
            isAuto = !isAuto;
            const btn = document.getElementById('btnToggleAuto');
            if (isAuto) {
                btn.textContent = '自動更新: 15s';
                btn.classList.remove('btn-outline');
                autoRefreshInterval = setInterval(fetchData, 15000);
            } else {
                btn.textContent = '自動更新: 關閉';
                btn.classList.add('btn-outline');
                clearInterval(autoRefreshInterval);
            }
        }

        // Initialize
        fetchData();
        autoRefreshInterval = setInterval(fetchData, 15000);
    </script>
</body>
</html>
"""


@app.post("/api/v1/sync/metrics", response_model=SyncResult)
def sync_metrics(
    payload: MetricsSyncIn,
    session: SessionDependency,
    _: AuthDependency,
) -> SyncResult:
    env_values = [
        {"device_id": payload.device_id, **item.model_dump()}
        for item in payload.env_data
    ]
    hvac_values = [
        {"device_id": payload.device_id, **item.model_dump()}
        for item in payload.hvac_data
    ]
    env_inserted = 0
    hvac_inserted = 0
    if env_values:
        result = session.execute(
            insert(EnvMetric)
            .values(env_values)
            .on_conflict_do_nothing(
                constraint="uq_env_device_timestamp"
            )
            .returning(EnvMetric.id)
        )
        env_inserted = len(result.scalars().all())
    if hvac_values:
        result = session.execute(
            insert(HVACStatus)
            .values(hvac_values)
            .on_conflict_do_nothing(
                constraint="uq_hvac_device_timestamp"
            )
            .returning(HVACStatus.id)
        )
        hvac_inserted = len(result.scalars().all())
    session.commit()
    return SyncResult(env_inserted=env_inserted, hvac_inserted=hvac_inserted)


@app.post(
    "/api/v1/sync/images",
    response_model=ImageResult,
    status_code=status.HTTP_201_CREATED,
)
def upload_image(
    session: SessionDependency,
    _: AuthDependency,
    device_id: Annotated[
        str,
        Form(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        ),
    ],
    timestamp: Annotated[datetime, Form()],
    image_type: Annotated[str, Form(pattern="^(RGB|IR)$")],
    image: Annotated[UploadFile, File()],
) -> ImageResult:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    existing = session.scalar(
        select(CameraLog).where(
            CameraLog.device_id == device_id,
            CameraLog.timestamp == timestamp,
            CameraLog.image_type == image_type,
        )
    )
    if existing is not None:
        return ImageResult(
            id=existing.id, stored=False, file_path=existing.file_path
        )

    settings = get_settings()
    storage = settings.image_storage_path / device_id
    suffix = Path(image.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".tiff", ".tif"}:
        suffix = ".bin"
    final_path = storage / f"{timestamp:%Y%m%dT%H%M%S}_{image_type}_{uuid4().hex}{suffix}"
    temporary_path = final_path.with_suffix(final_path.suffix + ".part")
    size = 0
    try:
        storage.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("xb") as output:
            while chunk := image.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_image_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "image too large")
                output.write(chunk)
        if size == 0:
            raise HTTPException(422, "image is empty")
        os.replace(temporary_path, final_path)
        row = CameraLog(
            device_id=device_id,
            timestamp=timestamp,
            image_type=image_type,
            file_path=str(final_path),
            content_type=image.content_type or "application/octet-stream",
            size_bytes=size,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    except HTTPException:
        session.rollback()
        raise
    except PermissionError as exc:
        session.rollback()
        import logging
        logging.getLogger(__name__).error("PermissionError writing to %s: %s", storage, exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Server storage permission denied on {storage}: {exc}. Please run 'sudo chmod -R 777 storage' on server host.",
        )
    except Exception as exc:
        session.rollback()
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        import logging
        logging.getLogger(__name__).exception("Failed to process image upload: %s", exc)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Image upload failed: {exc}")
    return ImageResult(id=row.id, stored=True, file_path=row.file_path)
