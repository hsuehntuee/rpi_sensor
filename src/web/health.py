"""儀表板「系統狀態」：磁碟、感測器、相機、冷氣遙控（Server 連不上不示警，資料暫存本機）。

每則示警分兩種：
  contact=False  客戶可自行處理（拔插 USB、重開機、換電源…），畫面上給處理方法，不寫 log
  contact=True   需要聯繫我們；第一次出現時寫一行 WARNING log（同一則不重複寫）
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config import Settings
from src.db.local_sqlite import LocalDatabase

LOGGER = logging.getLogger(__name__)

DISK_WARN_MB = 5 * 1024
DISK_CRIT_MB = 2 * 1024
PURGE_ALERT_DAYS = 30
REBOOT = "請重新啟動樹莓派（拔掉電源 10 秒再插回）"

_logged_contact_alerts: set[str] = set()


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.strptime(str(ts)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _age_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} 分鐘"
    if minutes < 48 * 60:
        return f"{minutes // 60} 小時"
    return f"{minutes // 1440} 天"


def collect_health(
    settings: Settings,
    database: LocalDatabase,
    hardware_status: dict[str, Any] | None = None,
    ac_remote: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    alerts: list[dict[str, Any]] = []
    result: dict[str, Any] = {"checked_at": now.isoformat(), "alerts": alerts}

    def alert(level: str, title: str, detail: str, action: str, contact: bool) -> None:
        alerts.append({"level": level, "title": title, "detail": detail, "action": action, "contact": contact})

    def check(name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            alert("warning", f"監控項目「{name}」讀取失敗", f"{type(exc).__name__}: {exc}",
                  "請截圖此畫面聯繫我們", contact=True)

    # 1. 磁碟空間與緊急刪照片
    def disk() -> None:
        total, _, free = shutil.disk_usage(settings.image_dir)
        free_mb, total_mb = free // 2**20, total // 2**20
        result["disk"] = {"total_mb": total_mb, "free_mb": free_mb,
                          "used_pct": round(100 * (1 - free / total), 1) if total else None,
                          "retention_days": settings.image_retention_days,
                          "emergency_purge_below_mb": settings.min_disk_free_mb}
        if free_mb < DISK_CRIT_MB:
            alert("critical", "儲存空間即將用完",
                  f"只剩 {free_mb / 1024:.1f} GB。低於 {settings.min_disk_free_mb / 1024:.1f} GB 時系統會自動刪除最舊的照片（包含尚未上傳的）。",
                  "請聯繫我們", contact=True)
        elif free_mb < DISK_WARN_MB:
            alert("warning", "儲存空間偏低", f"剩餘 {free_mb / 1024:.1f} GB / 共 {total_mb / 1024:.0f} GB。",
                  "確認網路正常即可，資料上傳後會自動清理舊照片；持續幾天都沒改善再聯繫我們", contact=False)

        purge = database.get_last_disk_purge()
        result["last_disk_purge"] = purge
        if purge:
            when = datetime.fromisoformat(purge["time"])
            if now - when < timedelta(days=PURGE_ALERT_DAYS):
                lost = purge.get("unsynced_deleted", 0)
                if lost:
                    alert("critical", "空間不足，系統已自動刪除未上傳的照片",
                          f"{_age_text(now - when)}前刪除 {purge.get('deleted_photos', 0)} 張照片，其中 {lost} 張尚未上傳到 Server，已永久遺失。",
                          "請聯繫我們", contact=True)
                else:
                    alert("warning", "空間不足，系統已自動刪除舊照片",
                          f"{_age_text(now - when)}前刪除 {purge.get('deleted_photos', 0)} 張照片，皆已上傳到 Server，資料沒有遺失。",
                          "不需處理", contact=False)

    # 2. 上傳積壓、感測器、相機
    def activity() -> None:
        act = database.get_activity()
        result["activity"] = act

        stale = timedelta(minutes=3 * max(settings.sample_interval_minutes, 1))
        hw = hardware_status or {}
        for key, ts_key, label in (("scd41", "last_env", "溫濕度/CO2 感測器 (SCD41)"),
                                   ("camera_rgb", "last_rgb", "RGB 相機"),
                                   ("camera_ir", "last_ir", "熱影像相機 (FLIR Lepton)")):
            if "Emulated" in str(hw.get(key, "")):
                alert("critical", f"未偵測到{label}", "開機時沒有找到這個硬體，這項資料不會記錄。",
                      f"可先試著{REBOOT}；請截圖此畫面聯繫我們", contact=True)
                continue
            last = _parse_utc(act[ts_key])
            if last is None or now - last > stale:
                alert("critical", f"{label}沒有新資料",
                      "從未記錄過資料。" if last is None else f"最後一筆是 {_age_text(now - last)}前。",
                      f"可先試著{REBOOT}；請截圖此畫面聯繫我們", contact=True)

    # 3. 冷氣遙控器
    def ac() -> None:
        if ac_remote is None:
            result["ac"] = {"enabled": False}
            return
        try:
            result["ac"] = {"enabled": True, **ac_remote.status()}
        except Exception as exc:
            result["ac"] = {"enabled": True, "connected": False, "error": str(exc)}
            alert("warning", "冷氣遙控器 (ESP32) 未連線", str(exc),
                  f"拔掉 ESP32 的 USB 線再插回；仍未連線則{REBOOT}", contact=False)

    for name, fn in (("磁碟", disk), ("資料與硬體", activity), ("冷氣遙控", ac)):
        check(name, fn)
    alerts.sort(key=lambda a: (not a["contact"], a["level"] != "critical"))
    _log_new_contact_alerts(alerts)
    return result


def _log_new_contact_alerts(alerts: list[dict[str, Any]]) -> None:
    """需要聯繫的示警只在第一次出現時寫一行 log；消失後再出現才會再寫"""
    current = {a["title"] for a in alerts if a["contact"]}
    for a in alerts:
        if a["contact"] and a["title"] not in _logged_contact_alerts:
            LOGGER.warning("[需聯繫] %s：%s", a["title"], a["detail"])
    _logged_contact_alerts.intersection_update(current)
    _logged_contact_alerts.update(current)
