# -*- coding: utf-8 -*-
"""持股風險紅黃綠燈：熔斷（崩盤）前市場訊號 + 持股個股訊號。

市場層級（每 10 分鐘、含開盤前 08:00 起）：
  * VIX 恐慌指數水位／單日飆升
  * S&P 500 期貨（ES=F）急跌
  * 加權指數（^TWII）盤中急殺
  * EWT（美股掛牌台灣 ETF）＝台指期夜盤的免費代理指標（假設：夜間台股情緒）
  * 亞股連鎖（日經、KOSPI）
個股層級（持股清單 watchlist.txt，僅交易時段）：
  * 對昨收跌幅
  * 委賣/委買五檔掛單量失衡（流動性急凍近似）
  * 急跌＋爆量（沿用量能監控的段量狀態，疑似出貨倒賣）

燈號規則：任一訊號達紅燈門檻 → 紅；僅達黃燈門檻 → 黃；否則綠。
個股燈號會再疊加市場燈號（市場紅 + 個股跌 → 個股升紅）。
結果寫入 docs/data/watch_alerts.json，紅燈另附加到 alerts.json 供警示卡顯示。

門檻都在 config.py（risk_* 開頭）。台股個股無熔斷機制，本模組是
「崩盤前兆」啟發式警示，不構成投資建議。
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
from . import monitor

log = logging.getLogger("screener.risk")

GREEN, YELLOW, RED = "green", "yellow", "red"
_RANK = {GREEN: 0, YELLOW: 1, RED: 2}


def _now():
    if ZoneInfo:
        return datetime.now(ZoneInfo(CONFIG["timezone"]))
    return datetime.now()


load_holdings = ds.load_holdings


def _grade(value, thresholds, direction):
    """value 與 [黃, 紅] 門檻比較。direction='ge' 越大越危險，'le' 越小越危險。"""
    if value is None:
        return None
    yellow, red = thresholds
    if direction == "ge":
        if value >= red:
            return RED
        if value >= yellow:
            return YELLOW
    else:
        if value <= red:
            return RED
        if value <= yellow:
            return YELLOW
    return GREEN


def _sig(label, value, light, unit="%"):
    return {"label": label, "value": value, "light": light or GREEN,
            "unit": unit}


def assess_market(ind=None):
    """市場層級訊號。回傳 {light, signals: [...]}。ind 可注入供離線測試。"""
    ind = ind if ind is not None else ds.fetch_market_indicators()
    sigs = []

    vix = ind.get("vix")
    if vix:
        sigs.append(_sig("VIX 恐慌指數", vix["price"],
                         _grade(vix["price"], CONFIG["risk_vix_level"], "ge"),
                         unit=""))
        sigs.append(_sig("VIX 單日變化", vix["change_pct"],
                         _grade(vix["change_pct"],
                                CONFIG["risk_vix_spike_pct"], "ge")))
    es = ind.get("es")
    if es:
        sigs.append(_sig("S&P500 期貨", es["change_pct"],
                         _grade(es["change_pct"],
                                CONFIG["risk_es_drop_pct"], "le")))
    twii = ind.get("twii")
    if twii:
        sigs.append(_sig("加權指數", twii["change_pct"],
                         _grade(twii["change_pct"],
                                CONFIG["risk_twii_drop_pct"], "le")))
    ewt = ind.get("ewt")
    if ewt:
        sigs.append(_sig("台灣ETF(EWT,夜間代理)", ewt["change_pct"],
                         _grade(ewt["change_pct"],
                                CONFIG["risk_ewt_drop_pct"], "le")))
    for key, label in (("n225", "日經225"), ("kospi", "韓股KOSPI")):
        q = ind.get(key)
        if q:
            sigs.append(_sig(label, q["change_pct"],
                             _grade(q["change_pct"],
                                    CONFIG["risk_asia_drop_pct"], "le")))

    light = GREEN
    for s in sigs:
        if _RANK[s["light"]] > _RANK[light]:
            light = s["light"]
    return {"light": light, "signals": sigs}


def _stock_light(code, q, vol_state, market_light):
    """單一持股燈號。q 來自 fetch_intraday_quotes_full，
    vol_state 來自 monitor_state.json 的 {last_cum, intervals}。"""
    reasons = []
    light = GREEN

    def bump(to, reason):
        nonlocal light
        reasons.append(reason)
        if _RANK[to] > _RANK[light]:
            light = to

    chg = q.get("change_pct")
    g = _grade(chg, CONFIG["risk_stock_drop_pct"], "le")
    if g in (YELLOW, RED):
        bump(g, f"跌幅 {chg}%")

    ask, bid = q.get("ask_lots"), q.get("bid_lots")
    if ask and bid and bid > 0 and ask >= CONFIG["risk_imbalance_min_ask_lots"]:
        ratio = ask / bid
        g = _grade(ratio, CONFIG["risk_imbalance_ratio"], "ge")
        if g in (YELLOW, RED):
            bump(g, f"委賣/委買掛單 {ratio:.1f} 倍（賣壓{ask:.0f}張/買盤{bid:.0f}張）")

    # 急跌 + 爆量（疑似出貨）：沿用量能監控的累積量狀態算最近段量
    if vol_state and vol_state.get("last_cum") is not None \
            and vol_state.get("intervals", 0) > 1:
        cum = q.get("volume_lots") or 0
        delta = cum - vol_state["last_cum"]
        avg = vol_state["last_cum"] / vol_state["intervals"]
        if avg > 0 and delta > avg * CONFIG["risk_dump_vol_ratio"] \
                and chg is not None and chg < 0:
            bump(RED, f"下跌中爆量：最近段 {delta:.0f} 張 = 平均段量 {avg:.0f} 張的 "
                      f"{delta / avg:.1f} 倍，疑似出貨")

    # 市場燈號疊加：市場紅且個股明顯下跌 → 紅；市場非綠 → 至少黃
    if market_light == RED and chg is not None \
            and chg <= CONFIG["risk_market_overlay_drop_pct"]:
        bump(RED, "市場紅燈且個股下跌")
    elif market_light != GREEN:
        bump(YELLOW, "市場出現風險訊號")

    return light, reasons


def assess(force=False, market_only=None, indicators=None, quotes=None):
    """執行一次風險評估並寫檔。回傳結果 dict。
    market_only=None 時自動判斷：非交易時段只評估市場訊號。
    indicators / quotes 可注入供離線測試。"""
    now = _now()
    in_hours = monitor._in_market_hours(now)
    if market_only is None:
        market_only = not in_hours
    if not force and now.weekday() >= 5:
        return None                                # 週末不評估

    market = assess_market(indicators)
    codes = load_holdings()
    stocks = {}

    if codes and (not market_only or force):
        state = monitor._load_state()
        today = now.strftime("%Y-%m-%d")
        vol_states = state["stocks"] if state.get("date") == today else {}
        q_all = quotes if quotes is not None else \
            ds.fetch_intraday_quotes_full(codes)
        for code in codes:
            q = q_all.get(code)
            if not q:
                stocks[code] = {"light": GREEN, "reasons": ["查無即時行情"],
                                "price": None, "change_pct": None}
                continue
            light, reasons = _stock_light(code, q, vol_states.get(code),
                                          market["light"])
            stocks[code] = {"light": light, "reasons": reasons,
                            "name": q.get("name", ""),
                            "price": q.get("price"),
                            "change_pct": q.get("change_pct")}

    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market": market,
        "stocks": stocks,
    }
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    path = os.path.join(CONFIG["data_dir"], "watch_alerts.json")

    # 紅燈「轉紅」時追加到 alerts.json（避免每 10 分鐘重複轟炸）
    prev = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:                                        # noqa: BLE001
        pass
    _append_red_alerts(now, market, stocks, prev)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def _append_red_alerts(now, market, stocks, prev):
    new_alerts = []
    prev_market = (prev.get("market") or {}).get("light")
    if market["light"] == RED and prev_market != RED:
        hot = [f"{s['label']} {s['value']}{s['unit']}"
               for s in market["signals"] if s["light"] == RED]
        new_alerts.append({
            "time": now.isoformat(timespec="seconds"),
            "code": "", "name": "市場風險",
            "message": "🛑 市場紅燈（熔斷前兆訊號）：" + "；".join(hot) +
                       "。請留意持股，評估是否減碼。",
        })
    prev_stocks = prev.get("stocks") or {}
    for code, s in stocks.items():
        if s["light"] == RED and \
                (prev_stocks.get(code) or {}).get("light") != RED:
            new_alerts.append({
                "time": now.isoformat(timespec="seconds"),
                "code": code, "name": s.get("name", ""),
                "price": s.get("price"),
                "message": (f"🛑 持股紅燈 {code} {s.get('name', '')}："
                            + "；".join(s["reasons"]) + "。注意崩跌風險！"),
            })
    if new_alerts:
        alerts = monitor.load_alerts()
        alerts.extend(new_alerts)
        monitor._save_alerts(alerts)
        for a in new_alerts:
            log.warning("風險警示: %s", a["message"])
