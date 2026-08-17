from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import Settings
from src.db.local_sqlite import LocalDatabase

LOGGER = logging.getLogger(__name__)


def create_edge_app(
    settings: Settings,
    database: LocalDatabase,
    capture_callback: Callable[[], None] | None = None,
    sync_callback: Callable[[], int] | None = None,
    hardware_status: dict[str, Any] | None = None,
) -> FastAPI:
    """建立樹莓派邊緣節點專屬的 FastAPI 儀表板應用程式"""
    app = FastAPI(
        title="Raspberry Pi Edge Local Dashboard",
        description="樹莓派 5 邊緣節點本地即時監控與控制儀表板",
        version="1.0.0",
    )

    image_dir = Path(settings.image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static/images", StaticFiles(directory=str(image_dir), check_dir=False), name="images")

    action_lock = threading.Lock()

    def _resolve_image_url(file_path: str) -> str:
        p = Path(file_path)
        try:
            rel = p.relative_to(image_dir).as_posix()
            return f"/static/images/{rel}"
        except Exception:
            return f"/static/images/{p.name}"

    def _get_file_size(file_path: str) -> int:
        try:
            p = Path(file_path)
            if p.exists() and p.is_file():
                return p.stat().st_size
        except Exception:
            pass
        return 0

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "node_type": "edge_sensor",
            "device_id": settings.device_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/overview")
    def get_overview() -> dict[str, Any]:
        counts = database.get_counts()
        latest_env = database.get_latest_env(limit=15)
        latest_hvac = database.get_latest_hvac(limit=15)
        latest_rgb = database.get_latest_cameras(limit=10, image_type="RGB")
        latest_ir = database.get_latest_cameras(limit=10, image_type="IR")

        def _fmt_cam(item: dict[str, Any]) -> dict[str, Any]:
            fpath = item.get("file_path", "")
            return {
                "id": item.get("id"),
                "device_id": item.get("device_id"),
                "timestamp": str(item.get("timestamp", "")),
                "image_type": item.get("image_type"),
                "file_path": fpath,
                "url": _resolve_image_url(fpath),
                "size_bytes": _get_file_size(fpath),
                "is_synced": bool(item.get("is_synced", 0)),
            }

        return {
            "device_id": settings.device_id,
            "server_url": settings.server_url,
            "sample_interval_minutes": settings.sample_interval_minutes,
            "counts": counts,
            "hardware": hardware_status or {
                "scd41": "Active",
                "camera_rgb": "Active",
                "camera_ir": "Active",
                "hvac_modbus": "Configured" if settings.modbus_slave_id is not None else "Disabled",
            },
            "latest_env": latest_env,
            "latest_hvac": latest_hvac,
            "latest_rgb_images": [_fmt_cam(c) for c in latest_rgb],
            "latest_ir_images": [_fmt_cam(c) for c in latest_ir],
        }

    @app.get("/api/v1/images/list")
    def list_local_images() -> list[dict[str, Any]]:
        images = []
        if image_dir.exists():
            for path in image_dir.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
                    try:
                        rel_path = path.relative_to(image_dir).as_posix()
                        stat = path.stat()
                        is_ir = "ir" in path.name.lower()
                        images.append({
                            "name": path.name,
                            "relative_path": rel_path,
                            "url": f"/static/images/{rel_path}",
                            "size_bytes": stat.st_size,
                            "type": "IR" if is_ir else "RGB",
                            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                        })
                    except Exception:
                        continue
        return sorted(images, key=lambda x: x["mtime"], reverse=True)

    @app.post("/api/v1/actions/capture")
    def action_instant_capture() -> dict[str, Any]:
        """手動觸發立即拍攝 (RGB & IR) 並上傳同步"""
        if capture_callback is None:
            raise HTTPException(status_code=501, detail="Instant capture is not supported in current mode")
        
        with action_lock:
            try:
                capture_callback()
                synced_count = 0
                if sync_callback is not None:
                    synced_count = sync_callback()
                return {
                    "status": "success",
                    "message": "雙相機立即拍攝與伺服器同步完成！",
                    "synced_count": synced_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                LOGGER.exception("Manual instant capture failed: %s", exc)
                raise HTTPException(status_code=500, detail=f"拍照或同步失敗: {exc}")

    @app.post("/api/v1/actions/sync")
    def action_instant_sync() -> dict[str, Any]:
        """手動觸發立即上傳待同步資料至遠端 Server"""
        if sync_callback is None:
            raise HTTPException(status_code=501, detail="Sync callback is not configured")
        
        with action_lock:
            try:
                synced_count = sync_callback()
                return {
                    "status": "success",
                    "message": f"成功同步 {synced_count} 筆資料/照片至 Server ({settings.server_url})",
                    "synced_count": synced_count,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                LOGGER.exception("Manual sync failed: %s", exc)
                raise HTTPException(status_code=500, detail=f"同步至伺服器失敗: {exc}")

    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/view", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    def serve_edge_dashboard() -> str:
        return EDGE_DASHBOARD_HTML

    return app


EDGE_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🍓 RPi 5 邊緣節點即時儀表板 (Edge Local Dashboard)</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-body: #0b0f19;
            --bg-card: #151d30;
            --bg-card-alt: #1c2742;
            --bg-card-hover: #223052;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --text-dim: #64748b;
            --accent-rpi: #ff3366;
            --accent-cyan: #38bdf8;
            --accent-emerald: #10b981;
            --accent-amber: #f59e0b;
            --accent-rose: #f43f5e;
            --accent-violet: #a855f7;
            --border-subtle: #1e293b;
            --border-bright: #334155;
            --glass-bg: rgba(21, 29, 48, 0.75);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-body);
            color: var(--text-main);
            padding: 20px 16px 40px;
            min-height: 100vh;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        
        /* Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            padding: 16px 20px;
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 16px;
            margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        .header-title-area { display: flex; align-items: center; gap: 14px; }
        .node-icon {
            font-size: 2rem;
            width: 48px;
            height: 48px;
            display: flex;
            align-items: center;
            justify-content: center;
            background: rgba(255, 51, 102, 0.12);
            border-radius: 12px;
            border: 1px solid rgba(255, 51, 102, 0.3);
        }
        .header-text h1 {
            font-size: 1.35rem;
            font-weight: 800;
            letter-spacing: -0.02em;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .badges-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 6px;
            flex-wrap: wrap;
        }
        .badge {
            font-size: 0.75rem;
            padding: 3px 9px;
            border-radius: 9999px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }
        .badge-live {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-emerald);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }
        .badge-live::before {
            content: "";
            width: 6px;
            height: 6px;
            background: var(--accent-emerald);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 6px var(--accent-emerald);
        }
        .badge-dev {
            background: rgba(56, 189, 248, 0.12);
            color: var(--accent-cyan);
            border: 1px solid rgba(56, 189, 248, 0.25);
            font-family: 'JetBrains Mono', monospace;
        }
        .badge-server {
            background: rgba(168, 85, 247, 0.12);
            color: var(--accent-violet);
            border: 1px solid rgba(168, 85, 247, 0.25);
            font-family: 'JetBrains Mono', monospace;
        }

        /* Action Buttons */
        .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        .btn {
            background: #2563eb;
            color: white;
            border: 1px solid rgba(255,255,255,0.1);
            padding: 9px 16px;
            border-radius: 10px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.85rem;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 2px 8px rgba(0,0,0,0.2);
        }
        .btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35); }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .btn-rpi {
            background: linear-gradient(135deg, #e11d48, #be123c);
            border-color: rgba(225, 29, 72, 0.4);
        }
        .btn-rpi:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(225, 29, 72, 0.45); }
        .btn-sync {
            background: linear-gradient(135deg, #0284c7, #0369a1);
            border-color: rgba(2, 132, 199, 0.4);
        }
        .btn-sync:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(2, 132, 199, 0.45); }
        .btn-outline {
            background: rgba(30, 41, 59, 0.6);
            border: 1px solid var(--border-bright);
            color: var(--text-muted);
        }
        .btn-outline:hover:not(:disabled) { background: var(--bg-card-alt); color: var(--text-main); }
        .btn-outline.active {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-emerald);
            border-color: rgba(16, 185, 129, 0.4);
        }

        /* Banner / Notification Toast */
        #toastNotice {
            display: none;
            padding: 12px 18px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-size: 0.9rem;
            font-weight: 500;
            display: flex;
            align-items: center;
            justify-content: space-between;
            animation: fadeIn 0.3s;
        }
        .toast-success { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); color: #34d399; }
        .toast-error { background: rgba(244, 63, 94, 0.15); border: 1px solid rgba(244, 63, 94, 0.4); color: #fb7185; }

        /* Stats Grid */
        .grid-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .stat-card {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            position: relative;
            overflow: hidden;
            transition: transform 0.2s, border-color 0.2s;
        }
        .stat-card:hover { transform: translateY(-2px); border-color: var(--border-bright); }
        .stat-card::after {
            content: "";
            position: absolute;
            top: 0; left: 0; right: 0; height: 3px;
            background: transparent;
        }
        .stat-card.temp::after { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
        .stat-card.hum::after { background: linear-gradient(90deg, #38bdf8, #0ea5e9); }
        .stat-card.co2::after { background: linear-gradient(90deg, #10b981, #34d399); }
        .stat-card.hvac::after { background: linear-gradient(90deg, #a855f7, #c084fc); }
        .stat-card.db::after { background: linear-gradient(90deg, #ff3366, #f43f5e); }
        .stat-card.queue::after { background: linear-gradient(90deg, #06b6d4, #3b82f6); }

        .stat-title { font-size: 0.8rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
        .stat-val { font-size: 1.85rem; font-weight: 800; line-height: 1.1; }
        .stat-sub { font-size: 0.75rem; color: var(--text-dim); }

        /* Camera Section */
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }
        .section-title {
            font-size: 1.15rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .camera-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 28px;
        }
        @media (max-width: 840px) {
            .camera-grid { grid-template-columns: 1fr; }
        }
        .camera-card {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            box-shadow: 0 4px 16px rgba(0,0,0,0.25);
        }
        .camera-header {
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(11, 15, 25, 0.6);
            border-bottom: 1px solid var(--border-subtle);
        }
        .camera-title { font-weight: 700; font-size: 0.95rem; display: flex; align-items: center; gap: 8px; }
        .camera-meta { font-size: 0.8rem; color: var(--text-muted); }
        .camera-img-box {
            width: 100%;
            height: 380px;
            background: #060911;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            overflow: hidden;
        }
        .camera-img-box img {
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
            cursor: pointer;
            transition: transform 0.25s ease;
        }
        .camera-img-box img:hover { transform: scale(1.03); }
        .camera-empty-placeholder {
            color: var(--text-dim);
            font-size: 0.9rem;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
        }
        .camera-footer {
            padding: 12px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.8rem;
            color: var(--text-muted);
            border-top: 1px solid var(--border-subtle);
            background: rgba(11, 15, 25, 0.4);
        }
        .camera-footer a { color: var(--accent-cyan); text-decoration: none; font-weight: 600; }
        .camera-footer a:hover { text-decoration: underline; }
        .sync-tag {
            font-size: 0.7rem;
            padding: 2px 7px;
            border-radius: 4px;
            font-weight: 600;
        }
        .sync-tag.synced { background: rgba(16, 185, 129, 0.15); color: #34d399; }
        .sync-tag.pending { background: rgba(245, 158, 11, 0.15); color: #fbbf24; }

        /* Tabs and Data Tables */
        .tabs { display: flex; gap: 8px; margin-bottom: 16px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 8px; overflow-x: auto; }
        .tab-btn {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 9px 18px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.88rem;
            transition: all 0.2s;
            white-space: nowrap;
        }
        .tab-btn:hover { background: var(--bg-card); color: var(--text-main); }
        .tab-btn.active { background: var(--bg-card-alt); color: var(--text-main); border-bottom: 2px solid var(--accent-cyan); }
        
        .table-wrap {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            overflow-x: auto;
        }
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.875rem; }
        th { background: rgba(11, 15, 25, 0.6); padding: 13px 18px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border-subtle); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.03em; }
        td { padding: 13px 18px; border-bottom: 1px solid rgba(30, 41, 59, 0.7); }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: rgba(30, 41, 59, 0.35); }

        /* Gallery */
        .gallery-filter {
            display: flex;
            gap: 8px;
            padding: 16px 18px 0;
            align-items: center;
        }
        .gallery-filter button {
            background: transparent;
            border: 1px solid var(--border-bright);
            color: var(--text-muted);
            padding: 4px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.8rem;
        }
        .gallery-filter button.active { background: var(--accent-cyan); color: #0b0f19; border-color: var(--accent-cyan); font-weight: 700; }
        .gallery-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 14px;
            padding: 18px;
        }
        .gallery-item {
            background: #060911;
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.2s, border-color 0.2s;
        }
        .gallery-item:hover { transform: translateY(-3px); border-color: var(--accent-cyan); }
        .gallery-item img {
            width: 100%;
            height: 135px;
            object-fit: cover;
            cursor: pointer;
        }
        .gallery-meta { padding: 8px 10px; font-size: 0.75rem; color: var(--text-muted); display: flex; justify-content: space-between; align-items: center; background: rgba(11, 15, 25, 0.8); }

        /* Hardware Info Panel */
        .hw-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 16px;
            padding: 20px;
        }
        .hw-item {
            background: var(--bg-body);
            border: 1px solid var(--border-subtle);
            border-radius: 10px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .hw-label { font-size: 0.75rem; color: var(--text-dim); text-transform: uppercase; font-weight: 600; }
        .hw-val { font-size: 0.95rem; font-weight: 700; font-family: 'JetBrains Mono', monospace; }

        /* Modal Lightbox */
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(4, 7, 13, 0.9);
            backdrop-filter: blur(8px);
            align-items: center;
            justify-content: center;
            cursor: pointer;
        }
        .modal.open { display: flex; }
        .modal-content {
            max-width: 94vw;
            max-height: 92vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 12px;
            cursor: default;
        }
        .modal img { max-width: 92vw; max-height: 84vh; border-radius: 10px; box-shadow: 0 0 40px rgba(0,0,0,0.9); border: 1px solid rgba(255,255,255,0.1); }
        .modal-close {
            position: absolute;
            top: 24px;
            right: 28px;
            color: var(--text-main);
            font-size: 2rem;
            cursor: pointer;
            line-height: 1;
        }

        /* Spinner */
        .spinner {
            display: inline-block;
            width: 14px;
            height: 14px;
            border: 2px solid rgba(255,255,255,0.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-6px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header>
            <div class="header-title-area">
                <div class="node-icon">🍓</div>
                <div class="header-text">
                    <h1>樹莓派 5 邊緣節點即時儀表板 <span style="font-size: 0.85rem; color: var(--accent-rpi); font-weight: 600;">(Edge Local)</span></h1>
                    <div class="badges-row">
                        <span class="badge badge-live">節點運行中</span>
                        <span class="badge" style="background:rgba(56,189,248,0.15); color:var(--accent-cyan); font-family:'JetBrains Mono',monospace;">台北時間 (GMT+8)</span>
                        <span class="badge badge-dev" id="badgeDeviceId">Device: rpi_edge</span>
                        <span class="badge badge-server" id="badgeServerUrl">Server: Connecting...</span>
                        <span style="font-size: 0.78rem; color: var(--text-dim); margin-left: 4px;" id="lastUpdated">更新中...</span>
                    </div>
                </div>
            </div>
            <div class="controls">
                <button class="btn btn-rpi" id="btnSnap" onclick="triggerInstantCapture()">
                    <span>📸</span> 立即拍照與同步
                </button>
                <button class="btn btn-sync" id="btnSync" onclick="triggerInstantSync()">
                    <span>📡</span> 立即同步至 Server
                </button>
                <button class="btn btn-outline active" id="btnToggleAuto" onclick="toggleAutoRefresh()">
                    自動更新: 15s
                </button>
                <button class="btn btn-outline" onclick="fetchData()">
                    🔄 刷新
                </button>
            </div>
        </header>

        <!-- Notification Toast -->
        <div id="toastNotice">
            <span id="toastMsg"></span>
            <span style="cursor:pointer; opacity:0.7;" onclick="hideToast()">✕</span>
        </div>

        <!-- Metric Stat Cards -->
        <div class="grid-stats">
            <div class="stat-card temp">
                <div class="stat-title">🌡️ SCD41 最新溫度</div>
                <div class="stat-val" id="statTemp" style="color: var(--accent-amber);">-- °C</div>
                <div class="stat-sub" id="statTempTime">等待取樣...</div>
            </div>
            <div class="stat-card hum">
                <div class="stat-title">💧 SCD41 最新濕度</div>
                <div class="stat-val" id="statHum" style="color: var(--accent-cyan);">-- %</div>
                <div class="stat-sub" id="statHumTime">等待取樣...</div>
            </div>
            <div class="stat-card co2">
                <div class="stat-title">🌿 CO2 二氧化碳濃度</div>
                <div class="stat-val" id="statCo2" style="color: var(--accent-emerald);">-- ppm</div>
                <div class="stat-sub" id="statCo2Status">正常品質</div>
            </div>
            <div class="stat-card hvac">
                <div class="stat-title">❄️ HVAC 冷氣狀態 / 功率</div>
                <div class="stat-val" id="statHvac">--</div>
                <div class="stat-sub" id="statHvacPower">0.0 W</div>
            </div>
            <div class="stat-card db">
                <div class="stat-title">📦 本地資料庫總筆數</div>
                <div class="stat-val" id="statTotal" style="color: var(--accent-rose);">0 筆</div>
                <div class="stat-sub" id="statCamCount">RGB: 0 | IR: 0</div>
            </div>
            <div class="stat-card queue">
                <div class="stat-title">⏳ 待上傳同步隊列 (Queue)</div>
                <div class="stat-val" id="statQueue" style="color: var(--accent-cyan);">0 筆</div>
                <div class="stat-sub" id="statQueueDetail">本地完全同步</div>
            </div>
        </div>

        <!-- Latest Dual Camera Side-by-Side -->
        <div class="section-header">
            <div class="section-title">
                <span>📷 本地最新雙相機拍攝畫面 (RGB & FLIR Lepton IR 熱影像)</span>
            </div>
            <span style="font-size: 0.82rem; color: var(--text-dim);">每 <span id="sampleIntervalText">5</span> 分鐘排程取樣 | 顯示時間為 <strong>GMT+8 台北時間</strong></span>
        </div>

        <div class="camera-grid">
            <!-- RGB Camera Card -->
            <div class="camera-card">
                <div class="camera-header">
                    <span class="camera-title">🟢 RGB 可見光相機 (PiCamera)</span>
                    <span class="camera-meta" id="rgbTime">--</span>
                </div>
                <div class="camera-img-box" id="rgbImgBox">
                    <img id="rgbImg" src="" alt="RGB 影像" style="display:none;" onerror="handleImageError('rgb')" onclick="openModal(this.src, '🟢 RGB 可見光相機畫面')">
                    <div id="rgbPlaceholder" class="camera-empty-placeholder">
                        <span style="font-size:2.5rem;">📷</span>
                        <span>等待 RGB 拍攝取樣...</span>
                    </div>
                    <div id="rgbErrorCard" class="camera-error-box" style="display:none; flex-direction:column; align-items:center; justify-content:center; gap:6px; padding:16px; text-align:center;">
                        <span style="font-size:2rem;">⚠️</span>
                        <div style="font-weight:700; color:var(--accent-rose); font-size:0.9rem;">RGB 照片讀取失敗 (檔案未生成或損毀)</div>
                        <div id="rgbErrorDetail" style="font-size:0.75rem; color:var(--text-muted); max-width:85%; word-break:break-all;"></div>
                        <button class="btn btn-outline" style="padding:4px 10px; font-size:0.75rem; margin-top:4px;" onclick="triggerInstantCapture()">📸 重新拍照</button>
                    </div>
                </div>
                <div class="camera-footer">
                    <div>
                        <span id="rgbSize">大小: --</span>
                        <span id="rgbSyncTag" class="sync-tag" style="margin-left:8px; display:none;">已同步</span>
                    </div>
                    <a id="rgbLink" href="#" target="_blank" style="display:none;">🔍 查看原圖</a>
                </div>
            </div>

            <!-- IR Camera Card -->
            <div class="camera-card">
                <div class="camera-header">
                    <span class="camera-title">🔴 FLIR Lepton IR 熱影像 (Lepton 3.X)</span>
                    <span class="camera-meta" id="irTime">--</span>
                </div>
                <div class="camera-img-box" id="irImgBox">
                    <img id="irImg" src="" alt="IR 影像" style="display:none;" onerror="handleImageError('ir')" onclick="openModal(this.src, '🔴 FLIR Lepton IR 熱感應畫面')">
                    <div id="irPlaceholder" class="camera-empty-placeholder">
                        <span style="font-size:2.5rem;">🔥</span>
                        <span>等待 IR 拍攝取樣...</span>
                    </div>
                    <div id="irErrorCard" class="camera-error-box" style="display:none; flex-direction:column; align-items:center; justify-content:center; gap:6px; padding:16px; text-align:center;">
                        <span style="font-size:2rem;">⚠️</span>
                        <div style="font-weight:700; color:var(--accent-rose); font-size:0.9rem;">IR 照片讀取失敗 (檔案未生成或損毀)</div>
                        <div id="irErrorDetail" style="font-size:0.75rem; color:var(--text-muted); max-width:85%; word-break:break-all;"></div>
                        <button class="btn btn-outline" style="padding:4px 10px; font-size:0.75rem; margin-top:4px;" onclick="triggerInstantCapture()">📸 重新拍照</button>
                    </div>
                </div>
                <div class="camera-footer">
                    <div>
                        <span id="irSize">大小: --</span>
                        <span id="irSyncTag" class="sync-tag" style="margin-left:8px; display:none;">已同步</span>
                    </div>
                    <a id="irLink" href="#" target="_blank" style="display:none;">🔍 查看原圖</a>
                </div>
            </div>
        </div>

        <!-- Tabs Navigation -->
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('env', this)">📊 環境感測記錄 (SCD41)</button>
            <button class="tab-btn" onclick="switchTab('hvac', this)">❄️ HVAC 冷氣記錄</button>
            <button class="tab-btn" onclick="switchTab('gallery', this)">🖼️ 本地相片藝廊</button>
            <button class="tab-btn" onclick="switchTab('hardware', this)">🔌 硬體與系統狀態</button>
        </div>

        <!-- SCD41 Table -->
        <div id="tabEnv" class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>取樣時間 (GMT+8 台北時間)</th>
                        <th>溫度 (°C)</th>
                        <th>濕度 (%)</th>
                        <th>CO2 (ppm)</th>
                        <th>同步狀態</th>
                    </tr>
                </thead>
                <tbody id="envTableBody">
                    <tr><td colspan="6" style="text-align: center; color: var(--text-dim); padding:24px;">載入中...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- HVAC Table -->
        <div id="tabHvac" class="table-wrap" style="display: none;">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>記錄時間 (GMT+8 台北時間)</th>
                        <th>運轉狀態</th>
                        <th>耗電功率 (W)</th>
                        <th>同步狀態</th>
                    </tr>
                </thead>
                <tbody id="hvacTableBody">
                    <tr><td colspan="5" style="text-align: center; color: var(--text-dim); padding:24px;">載入中...</td></tr>
                </tbody>
            </table>
        </div>

        <!-- Gallery Tab -->
        <div id="tabGallery" class="table-wrap" style="display: none;">
            <div class="gallery-filter">
                <span style="font-size:0.8rem; color:var(--text-muted);">篩選相片：</span>
                <button class="active" onclick="filterGallery('ALL', this)">全部 (<span id="countAllImgs">0</span>)</button>
                <button onclick="filterGallery('RGB', this)">RGB 可見光 (<span id="countRgbImgs">0</span>)</button>
                <button onclick="filterGallery('IR', this)">IR 熱影像 (<span id="countIrImgs">0</span>)</button>
            </div>
            <div class="gallery-grid" id="galleryContainer">
                <div style="grid-column: 1/-1; text-align: center; color: var(--text-dim); padding: 30px;">載入本地相片中...</div>
            </div>
        </div>

        <!-- Hardware & System Info Tab -->
        <div id="tabHardware" class="table-wrap" style="display: none;">
            <div class="hw-grid" id="hwGridContainer">
                <div class="hw-item">
                    <div class="hw-label">🍓 設備識別碼 (Device ID)</div>
                    <div class="hw-val" id="hwDeviceId">--</div>
                </div>
                <div class="hw-item">
                    <div class="hw-label">📡 遠端伺服器 (Server Target)</div>
                    <div class="hw-val" id="hwServerUrl">--</div>
                </div>
                <div class="hw-item">
                    <div class="hw-label">🌡️ SCD41 感測器</div>
                    <div class="hw-val" style="color:var(--accent-emerald);">已啟用 (I2C Bus 2)</div>
                </div>
                <div class="hw-item">
                    <div class="hw-label">📷 RGB 可見光相機</div>
                    <div class="hw-val" style="color:var(--accent-cyan);">PiCamera (rpicam-apps)</div>
                </div>
                <div class="hw-item">
                    <div class="hw-label">🔴 FLIR Lepton 3.X IR</div>
                    <div class="hw-val" style="color:var(--accent-rose);">SPI Kernel Driver (160x120)</div>
                </div>
                <div class="hw-item">
                    <div class="hw-label">❄️ HVAC Modbus 監控</div>
                    <div class="hw-val" id="hwHvacStatus">--</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Image Lightbox Modal -->
    <div id="imgModal" class="modal" onclick="closeModal()">
        <span class="modal-close" onclick="closeModal()">✕</span>
        <div class="modal-content" onclick="event.stopPropagation()">
            <img id="modalImg" src="">
            <div id="modalTitle" style="color:var(--text-muted); font-size:0.9rem; font-weight:600;"></div>
        </div>
    </div>

    <script>
        let autoRefreshInterval = null;
        let isAuto = true;
        let allGalleryImages = [];
        let currentFilter = 'ALL';

        function showToast(msg, type = 'success') {
            const toast = document.getElementById('toastNotice');
            const toastMsg = document.getElementById('toastMsg');
            toastMsg.textContent = msg;
            toast.className = type === 'success' ? 'toast-success' : 'toast-error';
            toast.style.display = 'flex';
            setTimeout(hideToast, 6000);
        }

        function hideToast() {
            document.getElementById('toastNotice').style.display = 'none';
        }

        async function fetchData() {
            try {
                const res = await fetch('/api/v1/overview');
                if (!res.ok) throw new Error('API 連線異常 (HTTP ' + res.status + ')');
                const data = await res.json();
                renderOverview(data);

                const imgRes = await fetch('/api/v1/images/list');
                if (imgRes.ok) {
                    allGalleryImages = await imgRes.json();
                    renderGallery(allGalleryImages, currentFilter);
                }

                document.getElementById('lastUpdated').textContent = '最後更新: ' + new Date().toLocaleTimeString();
            } catch (err) {
                console.error(err);
                document.getElementById('lastUpdated').textContent = '連線失敗: ' + err.message;
            }
        }

        function renderOverview(data) {
            document.getElementById('badgeDeviceId').textContent = 'Device: ' + (data.device_id || 'rpi_edge');
            document.getElementById('badgeServerUrl').textContent = 'Server: ' + (data.server_url || 'local');
            document.getElementById('hwDeviceId').textContent = data.device_id || '--';
            document.getElementById('hwServerUrl').textContent = data.server_url || '--';
            document.getElementById('sampleIntervalText').textContent = data.sample_interval_minutes || 5;

            const counts = data.counts || {};
            document.getElementById('statTotal').textContent = (counts.total_records || 0) + ' 筆';
            document.getElementById('statCamCount').textContent = `RGB: ${counts.camera_rgb_total || 0} | IR: ${counts.camera_ir_total || 0}`;
            
            const queueCount = counts.total_unsynced || 0;
            document.getElementById('statQueue').textContent = queueCount + ' 筆';
            if (queueCount === 0) {
                document.getElementById('statQueue').style.color = 'var(--accent-emerald)';
                document.getElementById('statQueueDetail').textContent = '✓ 本地資料全部已同步';
            } else {
                document.getElementById('statQueue').style.color = 'var(--accent-amber)';
                document.getElementById('statQueueDetail').textContent = `⚠️ 待上傳: Env ${counts.env_unsynced||0} | HVAC ${counts.hvac_unsynced||0} | 照片 ${(counts.camera_rgb_unsynced||0)+(counts.camera_ir_unsynced||0)}`;
            }

            // Latest Env (SCD41)
            if (data.latest_env && data.latest_env.length > 0) {
                const latest = data.latest_env[0];
                document.getElementById('statTemp').textContent = (latest.temperature != null ? Number(latest.temperature).toFixed(1) : '--') + ' °C';
                document.getElementById('statHum').textContent = (latest.humidity != null ? Number(latest.humidity).toFixed(1) : '--') + ' %';
                document.getElementById('statCo2').textContent = (latest.co2_ppm != null ? latest.co2_ppm : '--') + ' ppm';
                document.getElementById('statTempTime').textContent = formatTime(latest.timestamp);
                document.getElementById('statHumTime').textContent = formatTime(latest.timestamp);

                if (latest.co2_ppm > 1200) {
                    document.getElementById('statCo2Status').textContent = '🚨 濃度嚴重偏高，請立即通風！';
                    document.getElementById('statCo2Status').style.color = 'var(--accent-rose)';
                } else if (latest.co2_ppm > 1000) {
                    document.getElementById('statCo2Status').textContent = '⚠️ 濃度偏高，建議保持通風';
                    document.getElementById('statCo2Status').style.color = 'var(--accent-amber)';
                } else {
                    document.getElementById('statCo2Status').textContent = '🌿 空氣品質優良';
                    document.getElementById('statCo2Status').style.color = 'var(--accent-emerald)';
                }
            }

            // Latest HVAC
            if (data.latest_hvac && data.latest_hvac.length > 0) {
                const latestHvac = data.latest_hvac[0];
                const stateStr = latestHvac.hvac_state === 1 ? '🟢 運轉中 (ON)' : (latestHvac.hvac_state === 0 ? '⚪ 關閉 (OFF)' : '⚠️ 離線 / 未配置');
                document.getElementById('statHvac').textContent = stateStr;
                document.getElementById('statHvacPower').textContent = (latestHvac.power_w != null ? Number(latestHvac.power_w).toFixed(1) : '0.0') + ' W';
                document.getElementById('hwHvacStatus').textContent = latestHvac.hvac_state >= 0 ? 'Connected (State: ' + stateStr + ')' : 'Offline / Standalone';
            } else {
                document.getElementById('statHvac').textContent = '⚪ 未連線';
                document.getElementById('hwHvacStatus').textContent = 'Modbus 未連線';
            }

            // Latest RGB Camera Image
            const rgbImg = document.getElementById('rgbImg');
            const rgbPh = document.getElementById('rgbPlaceholder');
            const rgbErr = document.getElementById('rgbErrorCard');
            const rgbLink = document.getElementById('rgbLink');
            const rgbSyncTag = document.getElementById('rgbSyncTag');

            if (data.latest_rgb_images && data.latest_rgb_images.length > 0) {
                const rgb = data.latest_rgb_images[0];
                rgbErr.style.display = 'none';
                rgbPh.style.display = 'none';
                rgbImg.style.display = 'block';
                rgbImg.src = rgb.url + '?t=' + Date.now();
                rgbLink.href = rgb.url;
                rgbLink.style.display = 'inline';
                document.getElementById('rgbTime').textContent = formatTime(rgb.timestamp);
                document.getElementById('rgbSize').textContent = `大小: ${(rgb.size_bytes / 1024).toFixed(1)} KB`;
                
                rgbSyncTag.style.display = 'inline-block';
                rgbSyncTag.className = rgb.is_synced ? 'sync-tag synced' : 'sync-tag pending';
                rgbSyncTag.textContent = rgb.is_synced ? '✓ 已同步' : '⏳ 待上傳';
            } else {
                rgbImg.style.display = 'none';
                rgbErr.style.display = 'none';
                rgbPh.style.display = 'flex';
                rgbLink.style.display = 'none';
                rgbSyncTag.style.display = 'none';
                document.getElementById('rgbTime').textContent = '--';
                document.getElementById('rgbSize').textContent = '大小: --';
            }

            // Latest IR Camera Image
            const irImg = document.getElementById('irImg');
            const irPh = document.getElementById('irPlaceholder');
            const irErr = document.getElementById('irErrorCard');
            const irLink = document.getElementById('irLink');
            const irSyncTag = document.getElementById('irSyncTag');

            if (data.latest_ir_images && data.latest_ir_images.length > 0) {
                const ir = data.latest_ir_images[0];
                irErr.style.display = 'none';
                irPh.style.display = 'none';
                irImg.style.display = 'block';
                irImg.src = ir.url + '?t=' + Date.now();
                irLink.href = ir.url;
                irLink.style.display = 'inline';
                document.getElementById('irTime').textContent = formatTime(ir.timestamp);
                document.getElementById('irSize').textContent = `大小: ${(ir.size_bytes / 1024).toFixed(1)} KB`;

                irSyncTag.style.display = 'inline-block';
                irSyncTag.className = ir.is_synced ? 'sync-tag synced' : 'sync-tag pending';
                irSyncTag.textContent = ir.is_synced ? '✓ 已同步' : '⏳ 待上傳';
            } else {
                irImg.style.display = 'none';
                irErr.style.display = 'none';
                irPh.style.display = 'flex';
                irLink.style.display = 'none';
                irSyncTag.style.display = 'none';
                document.getElementById('irTime').textContent = '--';
                document.getElementById('irSize').textContent = '大小: --';
            }

            // Render SCD41 Table
            const envTbody = document.getElementById('envTableBody');
            if (data.latest_env && data.latest_env.length > 0) {
                envTbody.innerHTML = data.latest_env.map(e => `
                    <tr>
                        <td><strong>#${e.id}</strong></td>
                        <td>${formatTime(e.timestamp)}</td>
                        <td><strong style="color:var(--accent-amber);">${e.temperature != null ? Number(e.temperature).toFixed(2) : '-'}</strong></td>
                        <td><strong style="color:var(--accent-cyan);">${e.humidity != null ? Number(e.humidity).toFixed(2) : '-'}</strong></td>
                        <td><strong style="color:var(--accent-emerald);">${e.co2_ppm != null ? e.co2_ppm : '-'}</strong></td>
                        <td><span class="sync-tag ${e.is_synced ? 'synced' : 'pending'}">${e.is_synced ? '✓ 已同步' : '⏳ 待上傳'}</span></td>
                    </tr>
                `).join('');
            } else {
                envTbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color: var(--text-dim); padding:24px;">目前本地 SQLite 尚無環境感測數據</td></tr>';
            }

            // Render HVAC Table
            const hvacTbody = document.getElementById('hvacTableBody');
            if (data.latest_hvac && data.latest_hvac.length > 0) {
                hvacTbody.innerHTML = data.latest_hvac.map(h => `
                    <tr>
                        <td><strong>#${h.id}</strong></td>
                        <td>${formatTime(h.timestamp)}</td>
                        <td>${h.hvac_state === 1 ? '🟢 運轉中' : (h.hvac_state === 0 ? '⚪ 關閉' : '⚠️ 離線')}</td>
                        <td><strong>${h.power_w != null ? Number(h.power_w).toFixed(1) + ' W' : '-'}</strong></td>
                        <td><span class="sync-tag ${h.is_synced ? 'synced' : 'pending'}">${h.is_synced ? '✓ 已同步' : '⏳ 待上傳'}</span></td>
                    </tr>
                `).join('');
            } else {
                hvacTbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-dim); padding:24px;">目前本地 SQLite 尚無 HVAC 記錄</td></tr>';
            }
        }

        function handleImageError(type) {
            const img = document.getElementById(type + 'Img');
            const errBox = document.getElementById(type + 'ErrorCard');
            const errDetail = document.getElementById(type + 'ErrorDetail');
            const ph = document.getElementById(type + 'Placeholder');
            if (img) img.style.display = 'none';
            if (ph) ph.style.display = 'none';
            if (errBox) {
                errBox.style.display = 'flex';
                const cleanSrc = img ? img.src.split('?')[0] : '';
                errDetail.textContent = `路徑: ${cleanSrc} (請檢查相機排線連接或硬體模組狀態)`;
            }
        }

        function renderGallery(images, filter = 'ALL') {
            const container = document.getElementById('galleryContainer');
            if (!images || images.length === 0) {
                container.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-dim); padding: 30px;">本地目錄尚無拍攝照片</div>';
                return;
            }

            const rgbCount = images.filter(i => i.type === 'RGB').length;
            const irCount = images.filter(i => i.type === 'IR').length;
            document.getElementById('countAllImgs').textContent = images.length;
            document.getElementById('countRgbImgs').textContent = rgbCount;
            document.getElementById('countIrImgs').textContent = irCount;

            const filtered = filter === 'ALL' ? images : images.filter(i => i.type === filter);
            if (filtered.length === 0) {
                container.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-dim); padding: 30px;">無符合篩選條件的相片</div>';
                return;
            }

            container.innerHTML = filtered.map(img => `
                <div class="gallery-item">
                    <img src="${img.url}" alt="${img.name}" loading="lazy" onerror="this.onerror=null; this.style.opacity=0.3; this.title='載入失敗';" onclick="openModal('${img.url}', '${img.type === 'IR' ? '🔴 FLIR Lepton IR 熱影像' : '🟢 RGB 可見光相機'} (${img.name})')">
                    <div class="gallery-meta">
                        <span style="font-weight:700; color:${img.type === 'IR' ? 'var(--accent-rose)' : 'var(--accent-emerald)'};">${img.type === 'IR' ? '🔴 IR' : '🟢 RGB'}</span>
                        <span style="font-size:0.7rem; color:var(--text-dim);">${formatTime(img.mtime)}</span>
                    </div>
                </div>
            `).join('');
        }

        function filterGallery(type, btn) {
            currentFilter = type;
            document.querySelectorAll('.gallery-filter button').forEach(b => b.classList.remove('active'));
            if (btn) btn.classList.add('active');
            renderGallery(allGalleryImages, currentFilter);
        }

        async function triggerInstantCapture() {
            const btn = document.getElementById('btnSnap');
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span> 拍攝與同步中...';
            try {
                const res = await fetch('/api/v1/actions/capture', { method: 'POST' });
                const json = await res.json();
                if (!res.ok) throw new Error(json.detail || '拍攝失敗');
                showToast(json.message || '立即拍照與同步完成！', 'success');
                await fetchData();
            } catch (err) {
                console.error(err);
                showToast('拍照失敗: ' + err.message, 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = originalText;
            }
        }

        async function triggerInstantSync() {
            const btn = document.getElementById('btnSync');
            const originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span> 上傳同步中...';
            try {
                const res = await fetch('/api/v1/actions/sync', { method: 'POST' });
                const json = await res.json();
                if (!res.ok) throw new Error(json.detail || '同步失敗');
                showToast(json.message || '同步完成！', 'success');
                await fetchData();
            } catch (err) {
                console.error(err);
                showToast('同步失敗: ' + err.message, 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = originalText;
            }
        }

        function formatTime(isoStr) {
            if (!isoStr) return '--';
            let s = String(isoStr).trim();
            const hasTimezone = s.endsWith('Z') || /[+-]\d{2}(:\d{2})?$/.test(s);
            if (!hasTimezone) {
                // SQLite CURRENT_TIMESTAMP is in UTC, ensure 'Z' is appended for proper UTC interpretation
                s = s.replace(' ', 'T') + 'Z';
            } else if (s.includes(' ') && !s.includes('T')) {
                s = s.replace(' ', 'T');
            }
            const d = new Date(s);
            if (isNaN(d.getTime())) return isoStr;
            
            // 強制轉換為 GMT+8 (Asia/Taipei)
            const options = {
                timeZone: 'Asia/Taipei',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            };
            return new Intl.DateTimeFormat('zh-TW', options).format(d) + ' (GMT+8)';
        }

        function switchTab(tab, btn) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            if (btn) btn.classList.add('active');
            document.getElementById('tabEnv').style.display = tab === 'env' ? 'block' : 'none';
            document.getElementById('tabHvac').style.display = tab === 'hvac' ? 'block' : 'none';
            document.getElementById('tabGallery').style.display = tab === 'gallery' ? 'block' : 'none';
            document.getElementById('tabHardware').style.display = tab === 'hardware' ? 'block' : 'none';
        }

        function openModal(src, title) {
            if (!src) return;
            document.getElementById('modalImg').src = src;
            document.getElementById('modalTitle').textContent = title || '';
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
                btn.classList.add('active');
                autoRefreshInterval = setInterval(fetchData, 15000);
            } else {
                btn.textContent = '自動更新: 關閉';
                btn.classList.remove('active');
                clearInterval(autoRefreshInterval);
            }
        }

        // Init
        fetchData();
        autoRefreshInterval = setInterval(fetchData, 15000);
    </script>
</body>
</html>
"""
