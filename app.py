# -*- coding: utf-8 -*-
"""台股選股 + 量能監控 Web 應用程式。

啟動：python app.py  →  瀏覽器開 http://localhost:5000

排程：
  * 每交易日 15:30（台北時間）自動執行每日篩選（盤後資料公布後）
  * 盤中（09:00–13:30）每 10 分鐘檢查監控清單成交量，爆量即產生警示
"""

import logging
import threading

from flask import Flask, jsonify, send_from_directory

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from screener.config import CONFIG
from screener import screener, monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# 前端與資料檔都在 docs/（與 GitHub Pages 共用同一份），static_url_path=""
# 讓 /data/screen_results.json、/manifest.json、/sw.js 直接以根路徑提供。
app = Flask(__name__, static_folder="docs", static_url_path="")

_screen_lock = threading.Lock()
_screen_running = {"running": False}


# ---------------------------------------------------------------------------
# 網頁
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("docs", "index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/results")
def api_results():
    return jsonify(screener.load_results())


@app.route("/api/alerts")
def api_alerts():
    return jsonify(monitor.load_alerts())


@app.route("/api/status")
def api_status():
    return jsonify({
        "screen_running": _screen_running["running"],
        "watchlist": screener.watchlist(),
        "config": {k: v for k, v in CONFIG.items()
                   if not isinstance(v, (dict,))},
    })


@app.route("/api/screen/run", methods=["POST"])
def api_run_screen():
    """手動觸發每日篩選（背景執行，避免 HTTP 逾時）。"""
    if _screen_running["running"]:
        return jsonify({"ok": False, "message": "篩選正在執行中"}), 409

    def job():
        with _screen_lock:
            _screen_running["running"] = True
            try:
                screener.run_daily_screen()
            finally:
                _screen_running["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True, "message": "已開始執行篩選，請稍後重新整理結果"})


@app.route("/api/monitor/check", methods=["POST"])
def api_check_monitor():
    """手動觸發一次量能檢查（忽略開盤時間限制，方便測試）。"""
    alerts = monitor.check_volume_spike(force=True)
    return jsonify({"ok": True, "new_alerts": alerts})


# ---------------------------------------------------------------------------
# 排程
# ---------------------------------------------------------------------------

def start_scheduler():
    sched = BackgroundScheduler(timezone=CONFIG["timezone"])

    hh, mm = CONFIG["daily_screen_time"].split(":")
    sched.add_job(screener.run_daily_screen, CronTrigger(
        day_of_week="mon-fri", hour=int(hh), minute=int(mm)),
        id="daily_screen", name="每日選股")

    sched.add_job(monitor.check_volume_spike, "interval",
                  minutes=CONFIG["monitor_interval_min"],
                  id="volume_monitor", name="盤中量能監控")

    sched.start()
    log.info("排程已啟動：每日 %s 篩選、每 %d 分鐘量能監控",
             CONFIG["daily_screen_time"], CONFIG["monitor_interval_min"])
    return sched


if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False)
