# -*- coding: utf-8 -*-
"""💰選股（value_screen）成效追蹤（使用者 2026-10-03 需求）：
每日記錄篩選結果與候選股（對照組），追蹤 120 個交易日（≈半年）股價，
累積「條件 × 獲利機率 × 殖利率」統計 → docs/data/value_perf.json。

紀錄欄位（壓縮）：c=代號 nm=名稱 d=入選日 ep=進場價(入選日收盤)
sel=是否全符合（False=僅差K值等的對照組） cond=入選時條件快照
n=已追蹤交易日 last/lp=最近日期/收盤 mu/md=期間最高/最低報酬%
r={"5","20","60","120"} 里程碑報酬% done=滿120日定案
報酬皆不含股息（高殖利率組實際報酬另有配息貢獻，前端註明）。"""

import json
import logging
import os

from .config import CONFIG

log = logging.getLogger("screener.value_perf")

TRACK_DAYS = 120                  # ≈ 半年交易日
MILESTONES = (5, 20, 60, 120)
MAX_RECORDS = 3000


def _path():
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], "value_perf.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {"records": []}


def _ret(rec):
    if rec.get("lp") and rec.get("ep"):
        return (rec["lp"] - rec["ep"]) / rec["ep"] * 100
    return None


def _stats(recs):
    """里程碑勝率/平均報酬 + 目前浮動。"""
    out = {}
    for h in MILESTONES:
        vals = [rec["r"].get(str(h)) for rec in recs]
        vals = [v for v in vals if v is not None]
        if vals:
            out[str(h)] = {"n": len(vals),
                           "win": round(100 * sum(v > 0 for v in vals)
                                        / len(vals)),
                           "avg": round(sum(vals) / len(vals), 2)}
    cur = [_ret(rec) for rec in recs]
    cur = [v for v in cur if v is not None]
    if cur:
        out["now"] = {"n": len(cur),
                      "win": round(100 * sum(v > 0 for v in cur) / len(cur)),
                      "avg": round(sum(cur) / len(cur), 2)}
    return out


def _groups(recs):
    """條件分組統計：K值/殖利率/倍數/技術面 → 獲利機率對照。"""
    kd_max = CONFIG["value_kd_max"]

    def g(name, fn):
        sub = [r for r in recs if fn(r.get("cond") or {})]
        return (name, {"n": len(sub), **_stats(sub)}) if sub else None

    defs = [
        (f"K≤{kd_max:.0f}進場", lambda c: c.get("k") is not None
         and c["k"] <= kd_max),
        (f"K>{kd_max:.0f}未進場(對照)", lambda c: c.get("k") is not None
         and c["k"] > kd_max),
        ("殖利率≥6%", lambda c: (c.get("y") or 0) >= 6),
        ("殖利率3~6%", lambda c: 3 <= (c.get("y") or 0) < 6),
        ("殖利率<3%", lambda c: 0 < (c.get("y") or -1) < 3),
        ("倍數4~5", lambda c: (c.get("lr") or 0) >= 4),
        ("倍數3~4", lambda c: 3 <= (c.get("lr") or 0) < 4),
        ("技術多頭", lambda c: c.get("tech") == "bull"),
        ("技術盤整", lambda c: c.get("tech") == "neutral"),
        ("技術空頭", lambda c: c.get("tech") == "bear"),
    ]
    out = {}
    for d in defs:
        item = g(*d)
        if item:
            out[item[0]] = item[1]
    return out


def update(rows, stocks, trade_date):
    """rows=value_screen 候選列（含 pass/all）、stocks=快照 stocks、
    trade_date=本次交易日。回傳 (新增筆數, 追蹤中筆數)。"""
    data = _load()
    recs = data.get("records", [])
    open_codes = {r["c"] for r in recs if not r.get("done")}

    # 1) 新增：今日候選（全符合 sel=True；其餘=對照組），同檔已有未定案紀錄不重複
    added = 0
    for r in rows:
        if r["code"] in open_codes or r.get("price") in (None, 0):
            continue
        t = r.get("tech") or {}
        recs.append({
            "c": r["code"], "nm": r.get("name", ""), "d": trade_date,
            "ep": r["price"], "sel": bool(r.get("all")),
            "cond": {"y": r.get("yield_pct"), "div": r.get("div_est"),
                     "k": r.get("k"), "lr": r.get("liab_ratio"),
                     "eps": r.get("eps"), "tech": t.get("sum")},
            "n": 0, "last": trade_date, "lp": r["price"],
            "mu": 0.0, "md": 0.0,
            "r": {}, "done": False,
        })
        open_codes.add(r["code"])
        added += 1

    # 2) 追蹤：用當日快照收盤更新未定案紀錄（stale 不入帳）
    for rec in recs:
        if rec.get("done") or rec["last"] >= trade_date:
            continue
        s = stocks.get(rec["c"]) or {}
        if s.get("c") is None or s.get("stale"):
            continue
        rec["n"] += 1
        rec["last"] = trade_date
        rec["lp"] = s["c"]
        ret = _ret(rec)
        if ret is not None:
            rec["mu"] = round(max(rec.get("mu", 0), ret), 2)
            rec["md"] = round(min(rec.get("md", 0), ret), 2)
            if rec["n"] in MILESTONES:
                rec["r"][str(rec["n"])] = round(ret, 2)
        if rec["n"] >= TRACK_DAYS:
            rec["done"] = True

    recs = recs[-MAX_RECORDS:]
    sel = [r for r in recs if r["sel"]]
    rest = [r for r in recs if not r["sel"]]
    from . import performance
    out = {
        "generated_at": performance._now().isoformat(timespec="seconds"),
        "start_date": min((r["d"] for r in recs), default=trade_date),
        "records": recs,
        "summary": {
            "sel": _stats(sel), "rest": _stats(rest),
            "n_sel": len(sel), "n_rest": len(rest),
            "groups": _groups(recs),
        },
    }
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    n_open = sum(1 for r in recs if not r.get("done"))
    log.info("選股成效追蹤：新增 %d 筆，追蹤中 %d 筆", added, n_open)
    return added, n_open
