# -*- coding: utf-8 -*-
"""篩選成效追蹤：驗證「每日篩出的股票」是否值得作為買進依據。

每日篩選成功後，把當日 🏆(5/5) 與 🟡(①②+④) 組入選股記錄下來，
以入選當日收盤價為「進場價」，之後每個交易日利用篩選時已抓好的
全市場行情（不需額外呼叫 API）補上當日收盤價，追蹤 20 個交易日
（約一個月），計算：

- 最新報酬、D+5 / D+10 / D+20 報酬、期間最高與最低報酬
- 已滿一個月（定案）訊號的勝率與平均報酬（總計與分組）

結果寫入 docs/data/performance.json，由網頁「篩選成效」卡片顯示。
"""

import json
import logging
import os
from datetime import datetime

from .config import CONFIG

log = logging.getLogger("screener.performance")

TRACK_DAYS = 20            # 追蹤的交易日數（約一個月）
STALE_CALENDAR_DAYS = 45   # 入選超過此日曆天數仍湊不滿 20 筆價格 → 直接定案
MAX_RECORDS = 400          # 檔案最多保留幾筆紀錄（舊的先刪）


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


def update(results, quotes, trade_date):
    """每日篩選成功後呼叫。results=篩選結果列表、quotes=全市場行情。"""
    data = load()
    recs = data.get("records", [])
    by_key = {(r["code"], r["entry_date"]): r for r in recs}

    # 1. 新增今日 🏆/🟡 組入選股（同日重跑只更新、不重複記錄）
    for r in results:
        t = _tier(r)
        if t > 1:
            continue
        close = (quotes.get(r["code"]) or {}).get("close")
        if close is None:
            log.warning("績效追蹤：%s 無收盤價，本日略過", r["code"])
            continue
        key = (r["code"], trade_date)
        if key in by_key:
            by_key[key].update({"tier": t, "name": r.get("name", ""),
                                "entry_close": close})
            by_key[key]["prices"][trade_date] = close
        else:
            rec = {"code": r["code"], "name": r.get("name", ""), "tier": t,
                   "entry_date": trade_date, "entry_close": close,
                   "prices": {trade_date: close}, "done": False}
            recs.append(rec)
            by_key[key] = rec

    # 2. 所有追蹤中的紀錄補上今日收盤並重算報酬
    for rec in recs:
        if rec.get("done"):
            continue
        close = (quotes.get(rec["code"]) or {}).get("close")
        if close is not None:
            rec["prices"][trade_date] = close
        _derive(rec, trade_date)

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
    }
    data.update({
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "track_days": TRACK_DAYS,
        "trade_date": trade_date,
        "records": recs,
        "summary": summary,
    })
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log.info("績效追蹤：共 %d 筆訊號（追蹤中 %d、已定案 %d）",
             len(recs), summary["tracking"], len(done))
    return data
