from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import Settings
from src.db.local_sqlite import LocalDatabase
from src.web.health import collect_health

LOGGER = logging.getLogger(__name__)


class AcCommand(BaseModel):
    """一次送出完整冷氣狀態（冷氣遙控器本來就是每按一次送全部設定）"""
    power: bool
    mode: str = "cool"
    temp: float = 26
    fan: str = "auto"
    swing: bool = False


def create_edge_app(
    settings: Settings,
    database: LocalDatabase,
    capture_callback: Callable[[], None] | None = None,
    sync_callback: Callable[[], int] | None = None,
    hardware_status: dict[str, Any] | None = None,
    ac_remote: Any = None,
) -> FastAPI:
    """建立樹莓派邊緣節點專屬的 FastAPI 儀表板應用程式"""
    app = FastAPI(
        title="樹莓派測站 API",
        description="內網測站：讀取溫濕度/CO₂、照片、系統狀態，並控制冷氣。互動文件在 /docs。",
        version="1.0.0",
    )
    # 內網使用、無帳密：允許內網任何網頁直接呼叫
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
            },
            "latest_env": latest_env,
            "latest_rgb_images": [_fmt_cam(c) for c in latest_rgb],
            "latest_ir_images": [_fmt_cam(c) for c in latest_ir],
        }

    def _iso_utc(ts: Any) -> str | None:
        """SQLite 的 UTC 時間字串 → ISO 8601（2026-10-29T03:35:00Z）"""
        return f"{str(ts).replace(' ', 'T')}Z" if ts else None

    def _env_row(row: dict[str, Any]) -> dict[str, Any]:
        return {"timestamp": _iso_utc(row["timestamp"]), "temperature": row["temperature"],
                "humidity": row["humidity"], "co2_ppm": row["co2_ppm"]}

    @app.get("/api/v1/env/latest")
    def env_latest() -> dict[str, Any]:
        """最新一筆溫度 (°C)、相對濕度 (%)、CO₂ (ppm)"""
        rows = database.get_env_history(limit=1)
        if not rows:
            raise HTTPException(status_code=404, detail="尚無資料")
        return {"device_id": settings.device_id, **_env_row(rows[0])}

    @app.get("/api/v1/env")
    def env_history(
        limit: int = Query(100, ge=1, le=10000, description="最多幾筆（新到舊）"),
        since: datetime | None = Query(None, description="只回傳這個時間之後的資料，ISO 8601，例如 2026-10-29T00:00:00+08:00"),
    ) -> dict[str, Any]:
        """歷史溫濕度/CO₂，新到舊；每 5 分鐘一筆"""
        since_utc = None
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        rows = database.get_env_history(limit=limit, since=since_utc)
        return {"device_id": settings.device_id, "count": len(rows), "data": [_env_row(r) for r in rows]}

    @app.get("/api/v1/images/latest/{kind}")
    def image_latest(kind: str) -> FileResponse:
        """最新一張照片（JPEG）。kind = rgb（可見光）或 ir（熱影像）"""
        image_type = kind.upper()
        if image_type not in ("RGB", "IR"):
            raise HTTPException(status_code=404, detail="kind 必須是 rgb 或 ir")
        rows = database.get_latest_cameras(limit=1, image_type=image_type)
        if not rows or not Path(rows[0]["file_path"]).is_file():
            raise HTTPException(status_code=404, detail="尚無照片")
        return FileResponse(rows[0]["file_path"], media_type="image/jpeg",
                            headers={"X-Captured-At": _iso_utc(rows[0]["timestamp"]) or ""})

    @app.get("/api/v1/images/list")
    def list_local_images(limit: int = 60, type: str | None = None) -> list[dict[str, Any]]:
        """回傳本地最新 X 筆相片清單（預設最新 60 筆，避免遍歷全磁碟導致 CPU 與記憶體載入過大）"""
        # 優先路徑：直接自 SQLite 索引查詢最新 X 筆紀錄（極速 0.2ms，零磁碟 I/O 負擔）
        try:
            db_type = type.upper() if type and type.upper() in ("RGB", "IR") else None
            records = database.get_latest_cameras(limit=limit, image_type=db_type)
            if records:
                images = []
                for row in records:
                    fpath = row.get("file_path", "")
                    p = Path(fpath)
                    try:
                        rel_path = p.relative_to(image_dir).as_posix()
                    except Exception:
                        rel_path = p.name
                    size_bytes = _get_file_size(fpath)
                    images.append({
                        "name": p.name,
                        "relative_path": rel_path,
                        "url": _resolve_image_url(fpath),
                        "size_bytes": size_bytes,
                        "type": str(row.get("image_type", "RGB")).upper(),
                        "mtime": str(row.get("timestamp", "")),
                    })
                return images
        except Exception as exc:
            LOGGER.debug("Could not fetch image list from DB, falling back to scandir: %s", exc)

        # 備援路徑：若 DB 尚未初始化，以非遞迴方式快速取最新的 limit 筆檔案
        images = []
        if image_dir.exists():
            try:
                entries = [
                    e for e in os.scandir(image_dir)
                    if e.is_file() and e.name.lower().endswith((".jpg", ".jpeg", ".png"))
                ]
                entries.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                for entry in entries:
                    is_ir = "ir" in entry.name.lower()
                    img_type = "IR" if is_ir else "RGB"
                    if type and type.upper() != img_type:
                        continue
                    stat = entry.stat()
                    images.append({
                        "name": entry.name,
                        "relative_path": entry.name,
                        "url": f"/static/images/{entry.name}",
                        "size_bytes": stat.st_size,
                        "type": img_type,
                        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    })
                    if len(images) >= limit:
                        break
            except Exception as exc:
                LOGGER.warning("Scandir fallback failed: %s", exc)
        return images

    @app.post("/api/v1/actions/capture")
    def action_instant_capture() -> dict[str, Any]:
        """手動觸發立即拍攝 (RGB & IR) 並嘗試上傳同步 (若離線則安全保存於本地)"""
        if capture_callback is None:
            raise HTTPException(status_code=501, detail="Instant capture is not supported in current mode")
        
        with action_lock:
            try:
                capture_callback()
            except Exception as exc:  # 錯誤顯示在畫面上，不寫 log
                raise HTTPException(status_code=500, detail=f"拍照失敗: {exc}")

            synced_count = 0
            if sync_callback is not None:
                try:
                    synced_count = sync_callback()
                except Exception as sync_exc:
                    LOGGER.debug("Manual capture succeeded, but instant sync skipped (offline): %s", sync_exc)

            msg = "雙相機立即拍攝成功！" + ("（已同步至 Server）" if synced_count > 0 else "（已離線保存，待連網後同步）")
            return {
                "status": "success",
                "message": msg,
                "synced_count": synced_count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

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
            except Exception as exc:  # 多半是網路問題，顯示在畫面上，不寫 log
                raise HTTPException(status_code=500, detail=f"同步至伺服器失敗: {exc}")

    @app.get("/api/v1/system/health")
    def system_health() -> dict[str, Any]:
        """需要客戶回報的狀況（儀表板最上方的示警區）"""
        return collect_health(settings, database, hardware_status, ac_remote)

    @app.get("/api/v1/ac")
    def ac_status() -> dict[str, Any]:
        """冷氣遙控狀態（IR 是單向的：state 是最後送出或從原廠遙控器聽到的設定）"""
        latest = database.get_latest_env(limit=1)
        result: dict[str, Any] = {
            "name": settings.ac_name or settings.device_id,
            "protocol": settings.ac_protocol,
            "connected": False,
            "room": latest[0] if latest else None,
        }
        if ac_remote is None:
            return {**result, "error": "冷氣遙控未啟用 (AC_SERIAL_PORT=off)"}
        try:
            return {**result, **ac_remote.status()}
        except Exception as exc:  # 顯示在面板上（附自行處理方法），不寫 log
            return {**result, "error": str(exc)}

    @app.post("/api/v1/ac")
    def ac_set(command: AcCommand) -> dict[str, Any]:
        """控制冷氣：一次送出完整狀態。
        power: true/false；mode: cool/dry/fan/heat/auto；temp: 16–31；fan: auto/low/medium/high；swing: true/false。
        回傳送出後的狀態（同 GET /api/v1/ac）。"""
        if ac_remote is None:
            raise HTTPException(status_code=503, detail="冷氣遙控未啟用 (AC_SERIAL_PORT=off)")
        try:
            ac_remote.set_state(**command.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"冷氣指令送出失敗: {exc}")
        return ac_status()

    @app.get("/ac", response_class=HTMLResponse)
    def serve_ac_page() -> str:
        return AC_PAGE_HTML

    @app.get("/dashboard", response_class=HTMLResponse)
    @app.get("/view", response_class=HTMLResponse)
    @app.get("/", response_class=HTMLResponse)
    def serve_edge_dashboard() -> str:
        return EDGE_DASHBOARD_HTML

    return app


AC_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>冷氣遙控</title>
    <style>
        :root {
            --bg-body: #0e1117; --bg-card: #161b22; --bg-card-alt: #1c2230;
            --text-main: #e6e9ef; --text-muted: #9aa3b2; --text-dim: #6b7486;
            --accent-cyan: #4f8cff; --accent-emerald: #2fb67c; --accent-amber: #e5a23b;
            --accent-rose: #ef5b5b; --accent-violet: #4f8cff;
            --border-subtle: #262d3a; --border-bright: #353e4f;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", sans-serif; background: var(--bg-body);
               color: var(--text-main); padding: 20px 16px 40px; min-height: 100vh; }
        .wrap { max-width: 520px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
        a { color: var(--accent-cyan); text-decoration: none; font-size: 0.9rem; }
        .card { background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 16px;
                padding: 20px; }
        .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
        h1 { font-size: 1.3rem; font-weight: 800; }
        .badge { font-size: 0.75rem; padding: 3px 10px; border-radius: 9999px; font-weight: 600; }
        .on { background: rgba(16,185,129,0.15); color: var(--accent-emerald); border: 1px solid rgba(16,185,129,0.3); }
        .off { background: rgba(244,63,94,0.12); color: var(--accent-rose); border: 1px solid rgba(244,63,94,0.3); }
        .sub { font-size: 0.8rem; color: var(--text-dim); margin-top: 6px; }
        .temp-row { display: flex; align-items: center; justify-content: center; gap: 24px; margin: 8px 0 4px; }
        .temp { font-size: 4.2rem; font-weight: 800; min-width: 3.2ch; text-align: center; font-variant-numeric: tabular-nums; }
        .temp small { font-size: 1.4rem; color: var(--text-muted); }
        .round { width: 56px; height: 56px; border-radius: 50%; font-size: 1.8rem; }
        button { background: var(--bg-card-alt); color: var(--text-main); border: 1px solid var(--border-bright);
                 border-radius: 10px; cursor: pointer; font-weight: 600; font-size: 0.95rem; padding: 10px 0; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .power { width: 100%; padding: 14px; font-size: 1.1rem; }
        .power.is-on { background: #1f6f4f; border-color: var(--accent-emerald); }
        .label { font-size: 0.8rem; color: var(--text-muted); font-weight: 600; margin-bottom: 8px; }
        .seg { display: grid; gap: 8px; }
        .seg.c5 { grid-template-columns: repeat(5, 1fr); }
        .seg.c4 { grid-template-columns: repeat(4, 1fr); }
        .seg button.active { background: var(--accent-cyan); border-color: var(--accent-cyan); color: #fff; }
        .row { display: flex; justify-content: space-between; align-items: center; }
        .status { font-size: 0.85rem; color: var(--text-muted); min-height: 1.2em; text-align: center; }
        .status.err { color: var(--accent-rose); }
        .room { display: flex; gap: 20px; font-size: 0.9rem; color: var(--text-muted); }
        .room b { color: var(--text-main); font-size: 1.1rem; }
    </style>
</head>
<body>
<div class="wrap">
    <a href="/dashboard">← 回儀表板</a>

    <div class="card">
        <div class="head">
            <h1 id="acName">冷氣</h1>
            <span class="badge off" id="conn">連線中...</span>
        </div>
        <div class="sub" id="meta">--</div>
        <div class="room" style="margin-top:12px;">
            <span>室溫 <b id="roomTemp">--</b></span>
            <span>濕度 <b id="roomHum">--</b></span>
        </div>
    </div>

    <div class="card">
        <div class="temp-row">
            <button class="round" onclick="bumpTemp(-1)">−</button>
            <div class="temp"><span id="temp">26</span><small>°C</small></div>
            <button class="round" onclick="bumpTemp(1)">+</button>
        </div>
        <div class="status" id="status"></div>
    </div>

    <div class="card" style="display:flex; flex-direction:column; gap:18px;">
        <button class="power" id="powerBtn" onclick="togglePower()">開機</button>
        <div>
            <div class="label">模式</div>
            <div class="seg c5" id="modeSeg">
                <button data-v="cool">冷氣</button><button data-v="dry">除濕</button>
                <button data-v="fan">送風</button><button data-v="heat">暖氣</button>
                <button data-v="auto">自動</button>
            </div>
        </div>
        <div>
            <div class="label">風速</div>
            <div class="seg c4" id="fanSeg">
                <button data-v="auto">自動</button><button data-v="low">弱</button>
                <button data-v="medium">中</button><button data-v="high">強</button>
            </div>
        </div>
        <div class="row">
            <div class="label" style="margin:0;">擺風</div>
            <button id="swingBtn" style="width:96px;" onclick="toggleSwing()">關</button>
        </div>
    </div>

    <div class="sub" style="text-align:center;">
        紅外線是單向的：畫面顯示的是最後送出（或接收頭聽到原廠遙控器）的設定，不一定等於冷氣實際狀態。
    </div>
</div>

<script>
    const state = {power: false, mode: 'cool', temp: 26, fan: 'auto', swing: false};
    // dirty: 本地有改過但還沒送出（例如關機時先選模式），輪詢時不要蓋掉
    let busy = false, dirty = false, tempTimer = null;

    function render() {
        document.getElementById('temp').textContent = state.temp;
        const p = document.getElementById('powerBtn');
        p.textContent = state.power ? '運轉中（按此關機）' : '開機';
        p.classList.toggle('is-on', state.power);
        for (const [seg, key] of [['modeSeg', 'mode'], ['fanSeg', 'fan']]) {
            document.querySelectorAll('#' + seg + ' button').forEach(b =>
                b.classList.toggle('active', b.dataset.v === state[key]));
        }
        document.getElementById('swingBtn').textContent = state.swing ? '開' : '關';
    }

    function setStatus(msg, err) {
        const el = document.getElementById('status');
        el.textContent = msg || '';
        el.classList.toggle('err', !!err);
    }

    function applyInfo(info) {
        document.getElementById('acName').textContent = info.name || '冷氣';
        const c = document.getElementById('conn');
        c.textContent = info.connected ? 'ESP32 已連線' : '未連線';
        c.className = 'badge ' + (info.connected ? 'on' : 'off');
        document.getElementById('meta').textContent = info.connected
            ? `協定 ${info.protocol || '-'} · ${info.port}` : (info.error || '');
        if (info.room) {
            document.getElementById('roomTemp').textContent = info.room.temperature != null ? Number(info.room.temperature).toFixed(1) + ' °C' : '--';
            document.getElementById('roomHum').textContent = info.room.humidity != null ? Number(info.room.humidity).toFixed(0) + ' %' : '--';
        }
        if (info.state && !busy && !dirty) {
            Object.assign(state, {power: !!info.state.power, mode: info.state.mode || 'cool',
                                  temp: Math.round(info.state.temp || 26), fan: info.state.fan || 'auto',
                                  swing: !!info.state.swing});
            if (!['auto', 'low', 'medium', 'high'].includes(state.fan)) state.fan = 'auto';
            render();
        }
    }

    async function refresh() {
        try {
            const r = await fetch('/api/v1/ac');
            applyInfo(await r.json());
        } catch (e) { setStatus('無法連到樹莓派', true); }
    }

    async function send() {
        busy = true;
        setStatus('送出中...');
        try {
            const r = await fetch('/api/v1/ac', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify(state)});
            const j = await r.json();
            if (!r.ok) throw new Error(j.detail || r.status);
            busy = false;
            dirty = false;
            applyInfo(j);
            setStatus('已送出 ' + new Date().toLocaleTimeString('zh-TW', {hour12: false}));
        } catch (e) {
            busy = false;
            setStatus('送出失敗：' + e.message, true);
            refresh();
        }
    }

    function togglePower() { state.power = !state.power; dirty = true; render(); send(); }
    function toggleSwing() { state.swing = !state.swing; dirty = true; render(); if (state.power) send(); }
    function bumpTemp(d) {
        state.temp = Math.max(16, Math.min(31, state.temp + d));
        dirty = true;
        render();
        if (!state.power) return;
        clearTimeout(tempTimer);
        tempTimer = setTimeout(send, 500);  // 連按只送最後一次
    }
    for (const [seg, key] of [['modeSeg', 'mode'], ['fanSeg', 'fan']]) {
        document.querySelectorAll('#' + seg + ' button').forEach(b => b.onclick = () => {
            state[key] = b.dataset.v; dirty = true; render(); if (state.power) send();
        });
    }

    render();
    refresh();
    setInterval(() => { if (!busy) refresh(); }, 10000);
</script>
</body>
</html>
"""


EDGE_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>邊緣節點監控</title>
    <style>
        :root {
            --bg: #0e1117;
            --surface: #161b22;
            --surface-2: #1c2230;
            --border: #262d3a;
            --border-strong: #353e4f;
            --text: #e6e9ef;
            --muted: #9aa3b2;
            --dim: #6b7486;
            --accent: #4f8cff;
            --ok: #2fb67c;
            --warn: #e5a23b;
            --crit: #ef5b5b;
            --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
            background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5;
        }
        a { color: var(--accent); text-decoration: none; }
        .page { max-width: 1320px; margin: 0 auto; padding: 20px 16px 48px; display: flex; flex-direction: column; gap: 16px; }

        /* Top bar */
        .topbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
        .brand h1 { font-size: 18px; font-weight: 700; letter-spacing: 0.01em; }
        .brand .meta { display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 2px; }
        .brand .meta b { color: var(--text); font-weight: 600; font-family: var(--mono); }
        .actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .btn {
            background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong);
            padding: 7px 14px; border-radius: 8px; font: inherit; font-weight: 600; font-size: 13px; cursor: pointer;
            display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
        }
        .btn:hover:not(:disabled) { border-color: var(--muted); }
        .btn:disabled { opacity: 0.55; cursor: progress; }
        .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
        .btn.on { color: var(--ok); border-color: rgba(47,182,124,0.5); }

        /* Card */
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }
        .card-head { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border); gap: 8px; flex-wrap: wrap; }
        .card-head h2 { font-size: 13px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
        .card-body { padding: 16px; }

        /* Health banner */
        .health { border-left: 4px solid var(--ok); }
        .health.warn { border-left-color: var(--warn); }
        .health.crit { border-left-color: var(--crit); }
        .health-summary { display: flex; align-items: center; gap: 10px; padding: 14px 16px; font-weight: 700; }
        .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); flex: none; }
        .dot.warn { background: var(--warn); } .dot.crit { background: var(--crit); }
        .health-summary small { font-weight: 400; color: var(--muted); margin-left: auto; font-size: 12px; }
        .alert-list { display: flex; flex-direction: column; border-top: 1px solid var(--border); }
        .alert { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; padding: 12px 16px; border-bottom: 1px solid var(--border); }
        .alert:last-child { border-bottom: 0; }
        .alert .tag { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px; align-self: start; margin-top: 2px; }
        .alert .tag.contact { background: rgba(239,91,91,0.15); color: var(--crit); }
        .alert .tag.self { background: rgba(229,162,59,0.15); color: var(--warn); }
        .alert .title { font-weight: 700; }
        .alert .detail { grid-column: 2; color: var(--muted); }
        .alert .action { grid-column: 2; color: var(--text); font-size: 13px; }
        .alert .action::before { content: "→ "; color: var(--dim); }

        /* KPIs */
        .kpis { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
        @media (min-width: 640px) { .kpis { grid-template-columns: repeat(3, 1fr); } }
        @media (min-width: 1100px) { .kpis { grid-template-columns: repeat(6, 1fr); } }
        .kpi { padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; min-height: 104px; }
        a.kpi { color: inherit; }
        a.kpi:hover { border-color: var(--border-strong); }
        .kpi .label { font-size: 12px; color: var(--muted); font-weight: 600; }
        .kpi .value { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.2; }
        .kpi .value small { font-size: 14px; color: var(--muted); font-weight: 500; margin-left: 2px; }
        .kpi .sub { font-size: 12px; color: var(--dim); }
        .bar { height: 6px; background: var(--surface-2); border-radius: 3px; overflow: hidden; margin-top: 4px; }
        .bar > i { display: block; height: 100%; background: var(--accent); width: 0; }
        .ok-text { color: var(--ok); } .warn-text { color: var(--warn); } .crit-text { color: var(--crit); }

        /* Cameras */
        .cams { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
        .cam-frame { aspect-ratio: 4 / 3; background: #0a0d12; display: flex; align-items: center; justify-content: center; overflow: hidden; }
        .cam-frame img { width: 100%; height: 100%; object-fit: contain; cursor: zoom-in; display: none; }
        .cam-empty { color: var(--dim); font-size: 13px; text-align: center; padding: 16px; }
        .cam-foot { display: flex; justify-content: space-between; gap: 8px; padding: 10px 16px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); flex-wrap: wrap; }
        .pill { font-size: 11px; font-weight: 700; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border-strong); color: var(--muted); }
        .pill.synced { color: var(--ok); border-color: rgba(47,182,124,0.4); }
        .pill.pending { color: var(--warn); border-color: rgba(229,162,59,0.4); }

        /* Tabs */
        .tabs { display: flex; gap: 4px; padding: 6px 8px 0; border-bottom: 1px solid var(--border); overflow-x: auto; }
        .tab { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--muted); padding: 10px 12px; font: inherit; font-weight: 600; cursor: pointer; white-space: nowrap; }
        .tab.active { color: var(--text); border-bottom-color: var(--accent); }
        .panel { display: none; }
        .panel.active { display: block; }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
        th, td { text-align: left; padding: 10px 16px; border-bottom: 1px solid var(--border); white-space: nowrap; }
        th { font-size: 12px; color: var(--muted); font-weight: 600; background: var(--surface-2); }
        tr:last-child td { border-bottom: 0; }
        .empty { text-align: center; color: var(--dim); padding: 28px; }

        .gallery-tools { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; padding: 12px 16px; }
        .seg { display: inline-flex; border: 1px solid var(--border-strong); border-radius: 8px; overflow: hidden; }
        .seg button { background: none; border: 0; color: var(--muted); padding: 6px 12px; font: inherit; font-size: 13px; cursor: pointer; }
        .seg button.active { background: var(--surface-2); color: var(--text); }
        select { background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong); border-radius: 8px; padding: 6px 8px; font: inherit; font-size: 13px; }
        .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; padding: 0 16px 16px; }
        .thumb { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: #0a0d12; }
        .thumb img { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; cursor: zoom-in; }
        .thumb div { display: flex; justify-content: space-between; padding: 6px 8px; font-size: 11px; color: var(--dim); }

        .kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
        .kv > div { padding: 12px 16px; border-bottom: 1px solid var(--border); }
        .kv dt { font-size: 12px; color: var(--muted); }
        .kv dd { font-weight: 600; word-break: break-all; }

        /* Toast & modal */
        #toast { position: fixed; right: 16px; bottom: 16px; max-width: min(420px, calc(100vw - 32px)); padding: 12px 16px; border-radius: 10px; background: var(--surface-2); border: 1px solid var(--border-strong); display: none; z-index: 20; box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
        #toast.ok { border-color: rgba(47,182,124,0.6); } #toast.err { border-color: rgba(239,91,91,0.6); }
        #modal { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; flex-direction: column; gap: 10px; z-index: 30; padding: 16px; }
        #modal.open { display: flex; }
        #modal img { max-width: 100%; max-height: 85vh; }
        #modal div { color: var(--muted); font-size: 13px; }
        .spinner { width: 12px; height: 12px; border: 2px solid rgba(255,255,255,0.3); border-top-color: #fff; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>
<div class="page">
    <div class="topbar">
        <div class="brand">
            <h1>邊緣節點監控</h1>
            <div class="meta">
                <span>裝置 <b id="deviceId">--</b></span>
                <span>Server <b id="serverUrl">--</b></span>
                <span id="lastUpdated">更新中…</span>
            </div>
        </div>
        <div class="actions">
            <a class="btn" href="/ac">冷氣遙控</a>
            <button class="btn primary" id="btnSnap" onclick="runAction('capture', this)">立即拍照</button>
            <button class="btn" id="btnSync" onclick="runAction('sync', this)">立即同步</button>
            <button class="btn on" id="btnAuto" onclick="toggleAuto()">自動更新 15s</button>
        </div>
    </div>

    <!-- 系統狀態：需要客戶回報的狀況都在這裡 -->
    <section class="card health" id="health">
        <div class="health-summary"><span class="dot" id="healthDot"></span><span id="healthText">檢查系統狀態中…</span><small id="healthHint"></small></div>
        <div class="alert-list" id="alertList" style="display:none;"></div>
    </section>

    <section class="kpis">
        <div class="card kpi"><span class="label">溫度</span><span class="value" id="kTemp">--</span><span class="sub" id="kEnvTime">--</span></div>
        <div class="card kpi"><span class="label">相對濕度</span><span class="value" id="kHum">--</span><span class="sub">SCD41</span></div>
        <div class="card kpi"><span class="label">CO₂</span><span class="value" id="kCo2">--</span><span class="sub" id="kCo2Note">--</span></div>
        <a class="card kpi" href="/ac"><span class="label">冷氣遙控</span><span class="value" id="kAc" style="font-size:20px;">--</span><span class="sub" id="kAcSub">開啟控制頁面 →</span></a>
        <div class="card kpi"><span class="label">儲存空間</span><span class="value" id="kDisk">--</span><div class="bar"><i id="kDiskBar"></i></div><span class="sub" id="kDiskSub">--</span></div>
        <div class="card kpi"><span class="label">待上傳</span><span class="value" id="kQueue">--</span><span class="sub" id="kQueueSub">--</span></div>
    </section>

    <section class="cams">
        <div class="card">
            <div class="card-head"><h2>RGB 相機</h2><span class="sub" id="rgbTime" style="font-size:12px;color:var(--dim);">--</span></div>
            <div class="cam-frame"><img id="rgbImg" alt="RGB"><div class="cam-empty" id="rgbEmpty">尚無照片</div></div>
            <div class="cam-foot"><span id="rgbSize">--</span><span><span class="pill" id="rgbSync" style="display:none;"></span> <a id="rgbLink" href="#" target="_blank" style="display:none;">原圖</a></span></div>
        </div>
        <div class="card">
            <div class="card-head"><h2>熱影像 (FLIR Lepton)</h2><span class="sub" id="irTime" style="font-size:12px;color:var(--dim);">--</span></div>
            <div class="cam-frame"><img id="irImg" alt="IR"><div class="cam-empty" id="irEmpty">尚無照片</div></div>
            <div class="cam-foot"><span id="irSize">--</span><span><span class="pill" id="irSync" style="display:none;"></span> <a id="irLink" href="#" target="_blank" style="display:none;">原圖</a></span></div>
        </div>
    </section>

    <section class="card">
        <div class="tabs">
            <button class="tab active" data-tab="env">環境紀錄</button>
            <button class="tab" data-tab="gallery">相片</button>
            <button class="tab" data-tab="system">系統資訊</button>
        </div>
        <div class="panel active" id="panel-env">
            <div class="table-wrap"><table>
                <thead><tr><th>#</th><th>時間 (台北)</th><th>溫度 °C</th><th>濕度 %</th><th>CO₂ ppm</th><th>上傳</th></tr></thead>
                <tbody id="envRows"><tr><td colspan="6" class="empty">載入中…</td></tr></tbody>
            </table></div>
        </div>
        <div class="panel" id="panel-gallery">
            <div class="gallery-tools">
                <div class="seg" id="galleryFilter">
                    <button class="active" data-f="ALL">全部 <span id="cAll">0</span></button>
                    <button data-f="RGB">RGB <span id="cRgb">0</span></button>
                    <button data-f="IR">熱影像 <span id="cIr">0</span></button>
                </div>
                <select id="galleryLimit">
                    <option value="20">最新 20 張</option><option value="60" selected>最新 60 張</option>
                    <option value="100">最新 100 張</option><option value="200">最新 200 張</option>
                </select>
            </div>
            <div class="gallery" id="gallery"><div class="empty" style="grid-column:1/-1;">載入中…</div></div>
        </div>
        <div class="panel" id="panel-system">
            <dl class="kv" id="systemInfo"></dl>
        </div>
    </section>
</div>

<div id="toast"></div>
<div id="modal" onclick="this.classList.remove('open')"><img id="modalImg" alt=""><div id="modalTitle"></div></div>

<script>
    const $ = id => document.getElementById(id);
    let overview = null, health = null, autoTimer = null;
    let galleryImages = [], galleryFilter = 'ALL', galleryLoadedAt = 0;
    const MODES = {cool: '冷氣', dry: '除濕', fan: '送風', heat: '暖氣', auto: '自動'};

    function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])); }

    function formatTime(ts) {
        if (!ts) return '--';
        let s = String(ts).trim();
        if (!(s.endsWith('Z') || /[+-]\d{2}(:\d{2})?$/.test(s))) s = s.replace(' ', 'T') + 'Z';  // SQLite 時間是 UTC
        const d = new Date(s.replace(' ', 'T'));
        if (isNaN(d)) return ts;
        return new Intl.DateTimeFormat('zh-TW', {timeZone: 'Asia/Taipei', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).format(d);
    }

    function toast(msg, ok) {
        const t = $('toast');
        t.textContent = msg;
        t.className = ok ? 'ok' : 'err';
        t.style.display = 'block';
        clearTimeout(t._timer);
        t._timer = setTimeout(() => t.style.display = 'none', ok ? 4000 : 10000);
    }

    async function getJson(url, opts) {
        const r = await fetch(url, opts);
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
        return j;
    }

    async function refresh() {
        const errors = [];
        await Promise.all([
            getJson('/api/v1/overview').then(d => { overview = d; renderOverview(); }).catch(e => errors.push('資料: ' + e.message)),
            getJson('/api/v1/system/health').then(h => { health = h; renderHealth(); }).catch(e => errors.push('系統狀態: ' + e.message)),
        ]);
        if ($('panel-gallery').classList.contains('active')) loadGallery();
        if (overview && health) renderSystem();
        $('lastUpdated').textContent = errors.length ? '更新失敗：' + errors.join('；') : '更新於 ' + new Date().toLocaleTimeString('zh-TW', {hour12: false});
        $('lastUpdated').className = errors.length ? 'crit-text' : '';
    }

    function renderHealth() {
        const alerts = health.alerts || [];
        const contact = alerts.some(a => a.contact);
        const crit = contact || alerts.some(a => a.level === 'critical');
        $('health').className = 'card health' + (crit ? ' crit' : alerts.length ? ' warn' : '');
        $('healthDot').className = 'dot' + (crit ? ' crit' : alerts.length ? ' warn' : '');
        $('healthText').textContent = !alerts.length ? '系統正常'
            : contact ? '有狀況需要聯繫我們' : `${alerts.length} 個項目需要注意（可依說明自行處理）`;
        $('healthHint').textContent = contact ? `請截圖此區塊聯繫我們（裝置 ${overview ? overview.device_id : ''}）` : '';
        $('alertList').style.display = alerts.length ? 'flex' : 'none';
        $('alertList').innerHTML = alerts.map(a => `
            <div class="alert">
                <span class="tag ${a.contact ? 'contact' : 'self'}">${a.contact ? '需聯繫' : '可自行處理'}</span>
                <span class="title">${esc(a.title)}</span>
                <span class="detail">${esc(a.detail)}</span>
                ${a.action ? `<span class="action">${esc(a.action)}</span>` : ''}
            </div>`).join('');

        const d = health.disk;
        if (d) {
            $('kDisk').innerHTML = `${(d.free_mb / 1024).toFixed(1)}<small>GB 可用</small>`;
            $('kDiskBar').style.width = (d.used_pct || 0) + '%';
            $('kDiskBar').style.background = d.free_mb < 2048 ? 'var(--crit)' : d.free_mb < 5120 ? 'var(--warn)' : 'var(--accent)';
            $('kDiskSub').textContent = `共 ${(d.total_mb / 1024).toFixed(0)} GB · 照片保留 ${d.retention_days} 天`;
        }
        const ac = health.ac || {};
        if (!ac.enabled) { $('kAc').textContent = '未啟用'; $('kAcSub').textContent = 'AC_SERIAL_PORT=off'; }
        else if (!ac.connected) { $('kAc').innerHTML = '<span class="crit-text">未連線</span>'; $('kAcSub').textContent = '檢查 ESP32 USB 線'; }
        else {
            const s = ac.state || {};
            $('kAc').textContent = s.power ? `${MODES[s.mode] || s.mode} ${s.temp}°C` : '關機';
            $('kAcSub').textContent = '開啟控制頁面 →';
        }
    }

    function renderOverview() {
        const o = overview, c = o.counts || {};
        $('deviceId').textContent = o.device_id || '--';
        $('serverUrl').textContent = o.server_url || '--';

        const e = (o.latest_env || [])[0];
        $('kTemp').innerHTML = e && e.temperature != null ? `${Number(e.temperature).toFixed(1)}<small>°C</small>` : '--';
        $('kHum').innerHTML = e && e.humidity != null ? `${Number(e.humidity).toFixed(1)}<small>%</small>` : '--';
        $('kCo2').innerHTML = e && e.co2_ppm != null ? `${e.co2_ppm}<small>ppm</small>` : '--';
        $('kEnvTime').textContent = e ? formatTime(e.timestamp) : '尚無資料';
        const co2 = e ? e.co2_ppm : null;
        $('kCo2Note').textContent = co2 == null ? '--' : co2 > 1200 ? '濃度過高，請立即通風' : co2 > 1000 ? '偏高，建議通風' : '良好';
        $('kCo2Note').className = 'sub' + (co2 > 1200 ? ' crit-text' : co2 > 1000 ? ' warn-text' : '');

        const q = c.total_unsynced || 0;
        $('kQueue').innerHTML = `${q}<small>筆</small>`;
        $('kQueue').className = 'value' + (q ? ' warn-text' : ' ok-text');
        $('kQueueSub').textContent = q ? `環境 ${c.env_unsynced || 0} · 照片 ${(c.camera_rgb_unsynced || 0) + (c.camera_ir_unsynced || 0)}` : '全部已上傳';

        renderCamera('rgb', (o.latest_rgb_images || [])[0]);
        renderCamera('ir', (o.latest_ir_images || [])[0]);

        const rows = o.latest_env || [];
        $('envRows').innerHTML = rows.length ? rows.map(r => `
            <tr><td>${r.id}</td><td>${formatTime(r.timestamp)}</td>
            <td>${r.temperature != null ? Number(r.temperature).toFixed(2) : '-'}</td>
            <td>${r.humidity != null ? Number(r.humidity).toFixed(2) : '-'}</td>
            <td>${r.co2_ppm ?? '-'}</td>
            <td><span class="pill ${r.is_synced ? 'synced' : 'pending'}">${r.is_synced ? '已上傳' : '待上傳'}</span></td></tr>`).join('')
            : '<tr><td colspan="6" class="empty">尚無環境資料</td></tr>';
    }

    function renderCamera(type, img) {
        const el = $(type + 'Img'), empty = $(type + 'Empty'), pill = $(type + 'Sync'), link = $(type + 'Link');
        if (!img) {
            el.style.display = 'none'; empty.style.display = 'block'; empty.textContent = '尚無照片';
            pill.style.display = link.style.display = 'none';
            $(type + 'Time').textContent = '--'; $(type + 'Size').textContent = '--';
            return;
        }
        if (el.dataset.ts !== img.timestamp) {  // 新照片才重新載入，避免閃爍
            el.dataset.ts = img.timestamp;
            el.onload = () => { el.style.display = 'block'; empty.style.display = 'none'; };
            el.onerror = () => { el.style.display = 'none'; empty.style.display = 'block';
                                 empty.textContent = '照片讀取失敗：' + img.url + '（檔案不存在或已損毀）'; };
            el.src = img.url + '?t=' + encodeURIComponent(img.timestamp);
            el.onclick = () => openModal(img.url, (type === 'ir' ? '熱影像 ' : 'RGB ') + formatTime(img.timestamp));
        }
        $(type + 'Time').textContent = formatTime(img.timestamp);
        $(type + 'Size').textContent = (img.size_bytes / 1024).toFixed(1) + ' KB';
        pill.style.display = 'inline-block';
        pill.className = 'pill ' + (img.is_synced ? 'synced' : 'pending');
        pill.textContent = img.is_synced ? '已上傳' : '待上傳';
        link.href = img.url; link.style.display = 'inline';
    }

    function renderSystem() {
        const hw = overview.hardware || {}, d = health.disk || {}, ac = health.ac || {};
        const purge = health.last_disk_purge;
        const items = [
            ['裝置 ID', overview.device_id], ['Server', overview.server_url],
            ['取樣週期', `每 ${overview.sample_interval_minutes} 分鐘`],
            ['SCD41 感測器', hw.scd41], ['RGB 相機', hw.camera_rgb], ['熱影像相機', hw.camera_ir],
            ['冷氣遙控 (ESP32)', !ac.enabled ? '未啟用' : ac.connected ? `已連線 ${ac.port} · ${ac.protocol || '-'}` : `未連線：${ac.error || ''}`],
            ['儲存空間', d.total_mb ? `${(d.free_mb / 1024).toFixed(1)} GB 可用 / ${(d.total_mb / 1024).toFixed(0)} GB（已用 ${d.used_pct}%）` : '--'],
            ['照片保留', d.retention_days ? `已上傳的照片保留 ${d.retention_days} 天；可用空間低於 ${(d.emergency_purge_below_mb / 1024).toFixed(1)} GB 時自動刪最舊照片` : '--'],
            ['最近一次緊急刪照片', purge ? `${formatTime(purge.time)}：刪除 ${purge.deleted_photos} 張（未上傳 ${purge.unsynced_deleted} 張）` : '從未發生'],
            ['本機資料筆數', `環境 ${overview.counts.env_total} · RGB ${overview.counts.camera_rgb_total} · 熱影像 ${overview.counts.camera_ir_total}`],
        ];
        $('systemInfo').innerHTML = items.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v ?? '--')}</dd></div>`).join('');
    }

    async function loadGallery(force) {
        if (!force && Date.now() - galleryLoadedAt < 10000) return;
        galleryLoadedAt = Date.now();
        try {
            galleryImages = await getJson('/api/v1/images/list?limit=' + $('galleryLimit').value);
            renderGallery();
        } catch (e) {
            $('gallery').innerHTML = `<div class="empty crit-text" style="grid-column:1/-1;">相片清單讀取失敗：${esc(e.message)}</div>`;
        }
    }

    function renderGallery() {
        $('cAll').textContent = galleryImages.length;
        $('cRgb').textContent = galleryImages.filter(i => i.type === 'RGB').length;
        $('cIr').textContent = galleryImages.filter(i => i.type === 'IR').length;
        const list = galleryFilter === 'ALL' ? galleryImages : galleryImages.filter(i => i.type === galleryFilter);
        $('gallery').innerHTML = list.length ? list.map((img, i) => `
            <div class="thumb"><img loading="lazy" src="${esc(img.url)}" data-i="${i}" alt="">
            <div><span>${img.type === 'IR' ? '熱影像' : 'RGB'}</span><span>${formatTime(img.mtime)}</span></div></div>`).join('')
            : '<div class="empty" style="grid-column:1/-1;">沒有照片</div>';
        $('gallery').querySelectorAll('img').forEach(el => {
            const img = list[+el.dataset.i];
            el.onclick = () => openModal(img.url, `${img.type} ${formatTime(img.mtime)} · ${img.name}`);
            el.onerror = () => { el.style.opacity = 0.25; el.title = '載入失敗'; };
        });
    }

    function openModal(src, title) {
        $('modalImg').src = src;
        $('modalTitle').textContent = title || '';
        $('modal').classList.add('open');
    }

    async function runAction(kind, btn) {
        const label = btn.textContent;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner"></span>${kind === 'capture' ? '拍照中…' : '同步中…'}`;
        try {
            const j = await getJson('/api/v1/actions/' + kind, {method: 'POST'});
            toast(j.message || '完成', true);
            await refresh();
            if ($('panel-gallery').classList.contains('active')) loadGallery(true);
        } catch (e) {
            toast((kind === 'capture' ? '拍照失敗：' : '同步失敗：') + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = label;
        }
    }

    function toggleAuto() {
        const b = $('btnAuto');
        if (autoTimer) { clearInterval(autoTimer); autoTimer = null; b.textContent = '自動更新 關閉'; b.classList.remove('on'); }
        else { autoTimer = setInterval(refresh, 15000); b.textContent = '自動更新 15s'; b.classList.add('on'); }
    }

    document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
        document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === t));
        document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + t.dataset.tab));
        if (t.dataset.tab === 'gallery') loadGallery(true);
    });
    document.querySelectorAll('#galleryFilter button').forEach(b => b.onclick = () => {
        galleryFilter = b.dataset.f;
        document.querySelectorAll('#galleryFilter button').forEach(x => x.classList.toggle('active', x === b));
        renderGallery();
    });
    $('galleryLimit').onchange = () => loadGallery(true);

    refresh();
    autoTimer = setInterval(refresh, 15000);
</script>
</body>
</html>
"""
