# -*- coding: utf-8 -*-
"""盤中量能監控：每 10 分鐘檢查一次監控清單的累積成交量。

判斷方式：
  * 每次檢查記錄各股「當日累積成交量」，相減得到「最近一段（10 分鐘）成交量」。
  * 當日先前各段的平均量 = (本段之前的累積量) / 已經過的段數。
  * 若 最近一段量 > 平均量 * volume_spike_ratio（預設 10 倍），發出警示。

註：證交所即時 API 提供的是總成交量，未區分內外盤（買量/賣量），
    故以「爆量」作為大量買盤的近似判斷，警示訊息會附上現價與漲跌供判讀。
"""

import json
import logging
import os
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo
except ImportError:                                          # Python < 3.9
    ZoneInfo = None

from .config import CONFIG
from . import datasources as ds
from . import screener

log = logging.getLogger("screener.monitor")


def _data_path(name):
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], name)


def _load_state():
    """量能監控狀態存檔，讓 GitHub Actions 每 10 分鐘的獨立執行也能接續計算。
    格式: {"date": "YYYY-MM-DD", "stocks": {code: {"last_cum": float, "intervals": int}}}"""
    try:
        with open(_data_path("monitor_state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {"date": None, "stocks": {}}


def _save_state(state):
    with open(_data_path("monitor_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def _alert_path():
    return _data_path("alerts.json")


def load_alerts():
    try:
        with open(_alert_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return []


def _save_alerts(alerts):
    with open(_alert_path(), "w", encoding="utf-8") as f:
        json.dump(alerts[-200:], f, ensure_ascii=False, indent=2)


def _now():
    if ZoneInfo:
        return datetime.now(ZoneInfo(CONFIG["timezone"]))
    return datetime.now()


def _limit_up_codes():
    """由最新快照找出「收盤=漲停價」的個股（與前端判定邏輯一致）。"""
    try:
        with open(_data_path("market_snapshot.json"), encoding="utf-8") as f:
            stocks = json.load(f).get("stocks", {})
    except Exception:                                        # noqa: BLE001
        return []

    def tick(p):
        return (0.01 if p < 10 else 0.05 if p < 50 else 0.1 if p < 100
                else 0.5 if p < 500 else 1 if p < 1000 else 5)

    out = []
    for code, s in stocks.items():
        if len(code) != 4 or not code.isdigit() or code.startswith("00"):
            continue
        c, ch = s.get("c"), s.get("ch")
        if c is None or ch is None or ch <= 0 or c - ch <= 0:
            continue
        prev = c - ch
        raw = prev * 1.1
        t = tick(raw)
        lim = int((raw + 1e-9) / t) * t
        if abs(c - lim) < t / 2 and ch / prev >= 0.08:
            out.append((code, s.get("m", "tse")))
    return out


def check_limitup_open_strength():
    """前日漲停鎖定股的開盤 30 分鐘委買觀察：
    09:00–09:40 每輪更新 limitup_watch.json（委買五檔掛單量排序）；
    09:30 後首次執行時，把「委買 ≥ limitup_bid_min_lots 張」前 5 名
    發進 alerts.json（每日一次）。回傳觀察檔數。"""
    now = _now()
    hm = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")
    if now.weekday() >= 5 or not ("09:00" <= hm <= "09:40"):
        return 0
    lus = _limit_up_codes()
    if not lus:
        return 0
    codes = [c for c, _ in lus]
    q = ds.fetch_intraday_quotes_full(codes, dict(lus))
    rows = []
    for c in codes:
        v = q.get(c)
        if not v or v.get("price") is None:
            continue
        prev = v.get("prev_close")
        rows.append({
            "code": c, "name": v.get("name", ""),
            "bid_lots": v.get("bid_lots"), "ask_lots": v.get("ask_lots"),
            "price": v.get("price"), "vol": v.get("volume_lots"),
            "chg_pct": (round((v["price"] - prev) / prev * 100, 2)
                        if prev else None),
        })
    rows.sort(key=lambda r: -(r["bid_lots"] or 0))

    path = _data_path("limitup_watch.json")
    prev_state = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev_state = json.load(f)
    except Exception:                                        # noqa: BLE001
        pass
    alerted = (prev_state.get("alerted", False)
               if prev_state.get("date") == today else False)

    min_lots = CONFIG["limitup_bid_min_lots"]
    if hm >= "09:28" and not alerted:
        top = [r for r in rows if (r["bid_lots"] or 0) >= min_lots][:5]
        if top:
            msg = "、".join(
                f"{r['code']} {r['name']}（委買 {r['bid_lots']:,.0f} 張"
                f"，現價 {r['price']}"
                + (f" {r['chg_pct']:+.1f}%" if r['chg_pct'] is not None else "")
                + "）" for r in top)
            alerts = load_alerts()
            alerts.append({
                "time": now.isoformat(timespec="seconds"),
                "code": top[0]["code"], "name": "漲停續強觀察",
                "message": (f"🔥 前日漲停＋開盤30分委買≥{min_lots:,}張 "
                            f"前 {len(top)} 名：{msg}"),
            })
            _save_alerts(alerts)
            log.info("漲停續強警示：%d 檔", len(top))
        alerted = True

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": today, "time": now.isoformat(timespec="seconds"),
                   "min_bid_lots": min_lots, "alerted": alerted,
                   "rows": rows}, f, ensure_ascii=False)
    return len(rows)


def update_intraday_quotes():
    """盤中價同步：抓「每日篩選結果＋持股清單」的現價寫入
    docs/data/intraday_quotes.json，前端在盤中覆蓋顯示（約每 10 分鐘更新）。
    僅交易日 08:55–14:10 執行；回傳更新檔數。"""
    now = _now()
    hm = now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("08:55" <= hm <= "14:10"):
        return 0
    res = screener.load_results().get("results", [])
    codes = [r["code"] for r in res]
    market_map = {r["code"]: r.get("market", "tse") for r in res}
    for c in ds.load_holdings():
        if c not in codes:
            codes.append(c)
    for c, m in _limit_up_codes():          # 昨日漲停股（常被關注）一併涵蓋
        if c not in codes:
            codes.append(c)
            market_map[c] = m
    if not codes:
        return 0
    raw = ds.fetch_intraday_quotes_full(codes, market_map)
    quotes = {}
    for c, v in raw.items():
        if v.get("price") is None:
            continue
        quotes[c] = {"p": v["price"], "pc": v.get("prev_close"),
                     "o": v.get("open"), "v": v.get("volume_lots")}
    with open(_data_path("intraday_quotes.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": now.isoformat(timespec="seconds"),
                   "quotes": quotes}, f, ensure_ascii=False)
    log.info("盤中價同步 %d 檔", len(quotes))
    return len(quotes)


def _in_market_hours(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return False
    o = dtime(*map(int, CONFIG["market_open"].split(":")))
    c = dtime(*map(int, CONFIG["market_close"].split(":")))
    return o <= now.time() <= c


def check_volume_spike(force=False):
    """排程每 10 分鐘呼叫一次。回傳本次新產生的警示 list。"""
    now = _now()
    if not force and not _in_market_hours(now):
        return []

    codes = screener.watchlist()
    if not codes:
        return []

    state = _load_state()
    today = now.strftime("%Y-%m-%d")
    if state["date"] != today:                 # 換日重置
        state = {"date": today, "stocks": {}}

    market_map = {r["code"]: r.get("market", "tse")
                  for r in screener.load_results().get("results", [])}
    quotes = ds.fetch_intraday_volumes(codes, market_map)
    ratio = CONFIG["volume_spike_ratio"]
    new_alerts = []

    for code, q in quotes.items():
        cum = q["volume_lots"]
        st = state["stocks"].setdefault(code, {"last_cum": None, "intervals": 0})

        if st["last_cum"] is not None:
            delta = cum - st["last_cum"]               # 最近一段成交量
            prev_cum = st["last_cum"]
            n = st["intervals"]
            avg = prev_cum / n if n > 0 else None      # 先前每段平均量
            if avg and avg > 0 and delta > avg * ratio:
                info = screener.load_results()
                detail = next((r for r in info.get("results", [])
                               if r["code"] == code), {})
                alert = {
                    "time": now.isoformat(timespec="seconds"),
                    "code": code,
                    "name": detail.get("name", ""),
                    "price": q.get("price"),
                    "interval_lots": round(delta, 1),
                    "avg_interval_lots": round(avg, 1),
                    "ratio": round(delta / avg, 1),
                    "cum_lots": round(cum, 1),
                    "message": (f"{code} {detail.get('name', '')} 最近10分鐘"
                                f"成交 {delta:,.0f} 張，為當日平均段量 "
                                f"{avg:,.0f} 張的 {delta / avg:.1f} 倍，"
                                f"現價 {q.get('price')}，請注意！"),
                }
                new_alerts.append(alert)
                log.warning("量能警示: %s", alert["message"])

        st["last_cum"] = cum
        st["intervals"] += 1

    _save_state(state)
    if new_alerts:
        alerts = load_alerts()
        alerts.extend(new_alerts)
        _save_alerts(alerts)
    return new_alerts
