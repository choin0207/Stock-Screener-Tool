# -*- coding: utf-8 -*-
"""篩選成效追蹤：驗證「每日篩出的股票」是否值得作為買進依據。

每日篩選成功後，把當日 🏆(5/5) 與 🟡(①②+④) 組入選股記錄下來，
以入選當日收盤價為「進場價」，之後每個交易日利用篩選時已抓好的
全市場行情（不需額外呼叫 API）補上當日收盤價，追蹤 20 個交易日
（約一個月），計算：

- 最新報酬、D+5 / D+10 / D+20 報酬、期間最高與最低報酬
- 已滿一個月（定案）訊號的勝率與平均報酬（總計與分組）

另附「跌前警告」引擎：追蹤期間每天重新檢視五條件背後的因素，
任一因素轉弱（外資/投信轉賣、法人連賣、內部人轉讓增加、
合約負債下降、獲利轉弱）即記錄警訊並附加到 alerts.json 供
網頁警示卡與通知顯示；若股價之後跌破入選價（perf_drop_alert_pct），
回頭歸因「哪些因素在下跌前先變壞、提前幾天」，並累積各因素的
命中率統計，作為日後的跌前警告指標。

結果寫入 docs/data/performance.json，由網頁「篩選成效」卡片顯示。
"""

import json
import logging
import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:                                          # pragma: no cover
    ZoneInfo = None

from .config import CONFIG

log = logging.getLogger("screener.performance")

TRACK_DAYS = 20            # 追蹤的交易日數（約一個月）
STALE_CALENDAR_DAYS = 45   # 入選超過此日曆天數仍湊不滿 20 筆價格 → 直接定案
MAX_RECORDS = 400          # 檔案最多保留幾筆紀錄（舊的先刪）

# 跌前警告因素（factor → (顯示名稱, 對應篩選條件；「技」=技術訊號)）
FACTORS = {
    "foreign":  ("外資轉賣", "①"),
    "trust":    ("投信轉賣", "①"),
    "inst":     ("法人連賣", "②"),
    "insider":  ("內部人轉讓增加", "③"),
    "contract": ("合約負債下降", "④"),
    "eps":      ("獲利轉弱", "⑤"),
    "vol_dump": ("爆量下跌", "技"),
    "ma5":      ("跌破5日線", "技"),
    "streak":   ("連3黑", "技"),
}


def _path():
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], "performance.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {"updated_at": None, "track_days": TRACK_DAYS,
                "records": [], "summary": None}


def _now():
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(CONFIG["timezone"]))
        except Exception:                                    # noqa: BLE001
            pass
    return datetime.now()


def _tier(r):
    """與 screener.run_daily_screen 的分組規則一致。"""
    conds = r.get("conds") or {}
    gate = conds.get("c1") and conds.get("c2")
    if r.get("passed") == 5:
        return 0
    if gate and conds.get("c4"):
        return 1
    if conds.get("c4"):
        return 2
    return 3


def _date(s):
    return datetime.strptime(s, "%Y%m%d").date()


def _pct(price, base):
    if price is None or not base:
        return None
    return round((price - base) / base * 100, 2)


def _derive(rec, trade_date):
    """依 prices 重新計算報酬欄位與是否定案。"""
    entry = rec["entry_close"]
    after = sorted(d for d in rec["prices"] if d > rec["entry_date"])
    rec["days_tracked"] = len(after)
    last_d = after[-1] if after else rec["entry_date"]
    rec["last_date"] = last_d
    rec["last_close"] = rec["prices"][last_d]
    rec["ret_pct"] = _pct(rec["last_close"], entry)
    for n in (5, 10, 20):
        rec["ret_d%d" % n] = (_pct(rec["prices"][after[n - 1]], entry)
                              if len(after) >= n else None)
    vals = [rec["prices"][d] for d in after]
    rec["max_ret_pct"] = _pct(max(vals), entry) if vals else None
    rec["min_ret_pct"] = _pct(min(vals), entry) if vals else None
    stale = (_date(trade_date) - _date(rec["entry_date"])).days \
        >= STALE_CALENDAR_DAYS
    rec["done"] = len(after) >= TRACK_DAYS or stale


def _final_ret(rec):
    """定案報酬：優先 D+20，資料不足（停牌等）用最後一筆。"""
    if rec.get("ret_d20") is not None:
        return rec["ret_d20"]
    return rec.get("ret_pct")


def _stats(recs):
    vals = [v for v in (_final_ret(r) for r in recs) if v is not None]
    if not vals:
        return {"n": len(recs), "win_rate": None, "avg_ret": None}
    wins = sum(1 for v in vals if v > 0)
    return {"n": len(recs),
            "win_rate": round(wins / len(vals) * 100, 1),
            "avg_ret": round(sum(vals) / len(vals), 2)}


# ---------------------------------------------------------------------------
# 跌前警告：五條件因素轉弱偵測 + 下跌歸因
# ---------------------------------------------------------------------------

def _warn(rec, new_alerts, trade_date, factor, detail):
    """記錄一次因素轉弱。同一紀錄同一因素只在首次觸發時發 alert。"""
    label, cond = FACTORS[factor]
    w = rec.setdefault("warnings", {})
    if factor in w:
        w[factor].update({"last_date": trade_date, "detail": detail,
                          "count": w[factor].get("count", 0) + 1})
        return
    w[factor] = {"label": label, "cond": cond, "first_date": trade_date,
                 "last_date": trade_date, "count": 1, "detail": detail}
    src = "技術訊號" if cond == "技" else f"篩選條件{cond}因素"
    new_alerts.append({
        "time": _now().isoformat(timespec="seconds"),
        "code": rec["code"], "name": rec.get("name", ""),
        "message": (f"⚠️ 轉弱警訊 {rec['code']} {rec.get('name', '')}："
                    f"{label}（{src}）— {detail}。"
                    f"入選日 {rec['entry_date']}，目前報酬 "
                    f"{rec.get('ret_pct') if rec.get('ret_pct') is not None else '—'}%。"),
    })
    log.warning("轉弱警訊 %s %s: %s", rec["code"], label, detail)


def _flow_checks(rec, trade_date, new_alerts):
    """條件①②因素：外資/投信由買轉賣、三大法人連 2 日賣超。"""
    flows = rec.get("flows") or {}
    today = flows.get(trade_date)
    if not today:
        return
    dates = sorted(d for d in flows if d < trade_date)
    prev = flows[dates[-1]] if dates else None
    entry = flows.get(rec["entry_date"]) or [0, 0, 0]
    f, t, x = today

    def turned(cur, prev_v, entry_v):
        # 單日賣超吃掉入選日買超，或連 2 日賣超
        return cur < 0 and ((entry_v > 0 and -cur >= entry_v) or
                            (prev_v is not None and prev_v < 0))

    if turned(f, prev[0] if prev else None, entry[0]):
        _warn(rec, new_alerts, trade_date, "foreign",
              f"今日外資賣超 {-f:,.0f} 張（入選日買超 {entry[0]:,.0f} 張）")
    if turned(t, prev[1] if prev else None, entry[1]):
        _warn(rec, new_alerts, trade_date, "trust",
              f"今日投信賣超 {-t:,.0f} 張（入選日買超 {entry[1]:,.0f} 張）")
    if x < 0 and prev and prev[2] < 0:
        _warn(rec, new_alerts, trade_date, "inst",
              f"三大法人連 2 日賣超（今日 {-x:,.0f} 張）")


def _fin_checks(rec, trade_date, fin_now, new_alerts):
    """條件④⑤因素：新一季財報的合約負債下降、EPS 轉弱。"""
    fe = rec.get("fin_at_entry") or {}
    if not fin_now or not fe.get("period"):
        return
    p_now, p_e = fin_now.get("period"), fe.get("period")
    if not p_now or p_now == p_e:
        return                                 # 尚無新一季財報
    c_now, cap_now = fin_now.get("contract_liab_k"), fin_now.get("capital_k")
    c_e = fe.get("contract_liab_k")
    if c_now is not None and c_e:
        drop = (c_e - c_now) / c_e * 100
        ratio = CONFIG["contract_liability_capital_ratio"]
        below = cap_now is not None and c_now <= cap_now * ratio
        if drop >= CONFIG["perf_contract_drop_pct"] or below:
            msg = (f"合約負債 {c_e / 100000:,.2f} 億 → {c_now / 100000:,.2f} 億"
                   f"（{p_e}→{p_now}）")
            if below:
                msg += "，已跌破條件④門檻"
            _warn(rec, new_alerts, trade_date, "contract", msg)
    e_now, e_e = fin_now.get("eps"), fe.get("eps")
    if e_now is not None and e_now < CONFIG["profitability_threshold"] and \
            (e_e is None or e_now < e_e):
        _warn(rec, new_alerts, trade_date, "eps",
              f"EPS {e_e if e_e is not None else '—'} → {e_now}"
              f"（{p_e}→{p_now}），低於條件⑤門檻")


def _insider_check(rec, trade_date, transfer_lots, new_alerts):
    """條件③因素：內部人「一般交易」轉讓在入選後明顯增加。"""
    if transfer_lots is None:
        return
    rec["transfer_lots_now"] = round(transfer_lots, 1)
    base = rec.get("entry_transfer_lots") or 0
    mx = CONFIG["transfer_max_lots"]
    if transfer_lots > mx or transfer_lots - base >= mx:
        _warn(rec, new_alerts, trade_date, "insider",
              f"近30日一般交易轉讓 {transfer_lots:,.0f} 張"
              f"（入選時 {base:,.0f} 張）")


def _tech_checks(rec, trade_date, new_alerts):
    """技術跌前訊號（回測驗證的候選指標）：爆量下跌、跌破5日線、連3黑。"""
    dates = sorted(d for d in rec["prices"] if d <= trade_date)
    if trade_date not in rec["prices"] or len(dates) < 2:
        return
    closes = [rec["prices"][d] for d in dates]
    j = len(dates) - 1
    chg = ((closes[j] - closes[j - 1]) / closes[j - 1] * 100
           if closes[j - 1] else 0)
    vols = rec.get("vols") or {}
    pv = [vols[d] for d in dates[max(0, j - 5):j] if vols.get(d)]
    v_now = vols.get(trade_date)
    if len(pv) >= 3 and v_now and v_now > 2 * sum(pv) / len(pv) and chg <= -2:
        _warn(rec, new_alerts, trade_date, "vol_dump",
              f"成交 {v_now:,.0f} 張為5日均量 {sum(pv) / len(pv):,.0f} 張的 "
              f"{v_now / (sum(pv) / len(pv)):.1f} 倍，且下跌 {abs(chg):.1f}%")
    if j >= 5:
        ma5 = sum(closes[j - 4:j + 1]) / 5
        ma5p = sum(closes[j - 5:j]) / 5
        if closes[j] < ma5 and closes[j - 1] >= ma5p:
            _warn(rec, new_alerts, trade_date, "ma5",
                  f"收盤 {closes[j]} 跌破5日均線 {ma5:.2f}")
    if j >= 3 and closes[j] < closes[j - 1] < closes[j - 2] < closes[j - 3]:
        _warn(rec, new_alerts, trade_date, "streak",
              f"連 3 個交易日收黑（{closes[j - 3]} → {closes[j]}）")


def _drop_check(rec, trade_date, new_alerts):
    """跌破入選價門檻 → 記錄下跌事件並歸因：哪些警訊先出現、提前幾天。"""
    if rec.get("drop") or rec.get("ret_pct") is None:
        return
    if rec["ret_pct"] > CONFIG["perf_drop_alert_pct"]:
        return
    pdates = sorted(rec["prices"])
    preceded = []
    for fac, w in (rec.get("warnings") or {}).items():
        if w["first_date"] <= trade_date:
            lead = len([d for d in pdates
                        if w["first_date"] <= d < trade_date])
            preceded.append({"factor": fac, "label": w["label"],
                             "cond": w["cond"], "lead_days": lead})
    preceded.sort(key=lambda p: -p["lead_days"])
    rec["drop"] = {"date": trade_date, "ret_pct": rec["ret_pct"],
                   "preceded": preceded}
    before = ("；跌前訊號：" + "、".join(
        f"{p['label']}(提前{p['lead_days']}日)" for p in preceded)
        if preceded else "；事前無轉弱訊號")
    new_alerts.append({
        "time": _now().isoformat(timespec="seconds"),
        "code": rec["code"], "name": rec.get("name", ""),
        "message": (f"📉 {rec['code']} {rec.get('name', '')} 跌破入選價 "
                    f"{-CONFIG['perf_drop_alert_pct']:.0f}%"
                    f"（目前 {rec['ret_pct']}%）{before}。"),
    })
    log.warning("下跌事件 %s %s%%%s", rec["code"], rec["ret_pct"], before)


def _factor_stats(recs):
    """各因素的跌前命中統計（跨所有紀錄，含已定案）。"""
    drops = [r for r in recs if r.get("drop")]
    stats = {}
    for fac, (label, cond) in FACTORS.items():
        warned = [r for r in recs if fac in (r.get("warnings") or {})]
        # 已有結果的警告（該紀錄已定案或已發生下跌）才計入準確率
        settled = [r for r in warned if r.get("done") or r.get("drop")]
        hit = [r for r in drops
               if any(p["factor"] == fac for p in r["drop"]["preceded"])]
        stats[fac] = {"label": label, "cond": cond,
                      "warned": len(warned),
                      "warn_settled": len(settled),
                      "warn_then_drop": len([r for r in settled
                                             if r.get("drop")]),
                      "hit": len(hit)}
    return {"drops_total": len(drops), "factors": stats}


def _append_alerts(new_alerts):
    """附加到 alerts.json（與量能/風險警示同一張警示卡、同一套通知）。"""
    if not new_alerts:
        return
    path = os.path.join(CONFIG["data_dir"], "alerts.json")
    try:
        with open(path, encoding="utf-8") as f:
            alerts = json.load(f)
    except Exception:                                        # noqa: BLE001
        alerts = []
    alerts.extend(new_alerts)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(alerts[-200:], f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def update(results, quotes, trade_date, t86=None, financials=None,
           transfer_fn=None):
    """每日篩選成功後呼叫。results=篩選結果、quotes=全市場行情、
    t86=當日三大法人（股）、financials=全市場財報庫、
    transfer_fn=查內部人轉讓張數的函式（可省略）。"""
    data = load()
    recs = data.get("records", [])
    by_key = {(r["code"], r["entry_date"]): r for r in recs}
    t86 = t86 or {}
    financials = financials or {}
    new_alerts = []

    def lots(code):
        d = t86.get(code)
        if not d:
            return None
        return [round(d.get("foreign_net", 0) / 1000, 1),
                round(d.get("trust_net", 0) / 1000, 1),
                round(d.get("total_net", 0) / 1000, 1)]

    # 1. 新增今日 🏆/🟡 組入選股（同日重跑只更新、不重複記錄）
    def _close(code):
        q = quotes.get(code) or {}
        return None if q.get("stale") else q.get("close")

    for r in results:
        t = _tier(r)
        if t > 1:
            continue
        close = _close(r["code"])
        if close is None:
            log.warning("績效追蹤：%s 無收盤價，本日略過", r["code"])
            continue
        key = (r["code"], trade_date)
        fin_entry = {"contract_liab_k": r.get("contract_liab_k"),
                     "capital_k": r.get("capital_k"),
                     "eps": r.get("eps"), "period": r.get("fin_period")}
        if key in by_key:
            by_key[key].update({"tier": t, "name": r.get("name", ""),
                                "entry_close": close,
                                "fin_at_entry": fin_entry,
                                "entry_transfer_lots":
                                    r.get("transfer_general_lots")})
            by_key[key]["prices"][trade_date] = close
        else:
            rec = {"code": r["code"], "name": r.get("name", ""), "tier": t,
                   "entry_date": trade_date, "entry_close": close,
                   "prices": {trade_date: close}, "flows": {},
                   "fin_at_entry": fin_entry,
                   "entry_transfer_lots": r.get("transfer_general_lots"),
                   "done": False}
            recs.append(rec)
            by_key[key] = rec

    # 2. 追蹤中的紀錄：補當日收盤與法人流向 → 轉弱檢查 → 下跌歸因
    for rec in recs:
        if rec.get("done"):
            continue
        q = quotes.get(rec["code"]) or {}
        close = None if q.get("stale") else q.get("close")
        if close is not None:
            rec["prices"][trade_date] = close
            if q.get("volume_lots") is not None:
                rec.setdefault("vols", {})[trade_date] = q["volume_lots"]
        fl = lots(rec["code"])
        if fl is not None:
            rec.setdefault("flows", {})[trade_date] = fl
        _derive(rec, trade_date)

        _flow_checks(rec, trade_date, new_alerts)
        _tech_checks(rec, trade_date, new_alerts)
        _fin_checks(rec, trade_date, financials.get(rec["code"]), new_alerts)
        if transfer_fn is not None:
            try:
                _insider_check(rec, trade_date,
                               transfer_fn(rec["code"]), new_alerts)
            except Exception:                                # noqa: BLE001
                log.exception("績效追蹤：%s 轉讓查詢失敗", rec["code"])
        _drop_check(rec, trade_date, new_alerts)

    # 3. 修剪與摘要
    recs.sort(key=lambda r: (r["entry_date"], r["code"]))
    if len(recs) > MAX_RECORDS:
        recs = recs[-MAX_RECORDS:]
    done = [r for r in recs if r.get("done")]
    summary = {
        "total": len(recs),
        "tracking": len(recs) - len(done),
        "done": _stats(done),
        "by_tier": {str(t): _stats([r for r in done if r.get("tier") == t])
                    for t in (0, 1)},
        "drop_stats": _factor_stats(recs),
    }
    data.update({
        "updated_at": _now().isoformat(timespec="seconds"),
        "track_days": TRACK_DAYS,
        "trade_date": trade_date,
        "records": recs,
        "summary": summary,
    })
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    _append_alerts(new_alerts)
    log.info("績效追蹤：共 %d 筆訊號（追蹤中 %d、已定案 %d），本次新警訊 %d 則",
             len(recs), summary["tracking"], len(done), len(new_alerts))
    return data
