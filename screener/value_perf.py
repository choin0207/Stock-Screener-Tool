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

# 下車警訊（獲利了結訊號，僅對入選組逐日檢查；2026-10-07 使用者需求）
WARN_SIGS = {
    "peak_pullback": "高點回落逾8%（獲利保護）",
    "ma5_break": "收盤跌破5日均線",
    "vol_dump": "爆量下跌（量>5日均2倍且跌>2%）",
    "inst_sell2": "法人連2日賣超",
    "streak3": "連3日收黑",
}
PULLBACK_PCT = -8.0               # 高點回落警訊門檻
DROP_PCT = -10.0                  # 從高點回落逾此% = 大幅下跌事件（做歸因統計）


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


def _check_warnings(rec):
    """逐日下車警訊（獲利了結訊號）：寫 rec['warn']=目前成立訊號列表、
    rec['sig_first']={訊號: 首次出現的px索引}；高點回落逾 DROP_PCT 記
    rec['drop'] 大幅下跌事件並歸因哪些訊號先出現、提前幾個交易日。"""
    px = rec.get("px") or []
    if len(px) < 2:
        return
    vx, xh = rec.get("vx") or [], rec.get("xh") or []
    cur, peak = px[-1], max(px)
    active = []
    if peak > 0 and (cur - peak) / peak * 100 <= PULLBACK_PCT:
        active.append("peak_pullback")
    if len(px) >= 5 and cur < sum(px[-5:]) / 5:
        active.append("ma5_break")
    if len(vx) >= 6:
        v5 = sum(vx[-6:-1]) / 5
        chg = cur / px[-2] - 1 if px[-2] else 0
        if v5 and vx[-1] > v5 * 2 and chg < -0.02:
            active.append("vol_dump")
    if len(xh) >= 2 and xh[-1] < 0 and xh[-2] < 0:
        active.append("inst_sell2")
    if len(px) >= 4 and px[-1] < px[-2] < px[-3] < px[-4]:
        active.append("streak3")
    rec["warn"] = active
    first = rec.setdefault("sig_first", {})
    for sig in active:
        first.setdefault(sig, len(px) - 1)
    if "drop" not in rec and peak > 0 \
            and (cur - peak) / peak * 100 <= DROP_PCT:
        rec["drop"] = {
            "d": rec["last"],
            "from_peak": round((cur - peak) / peak * 100, 1),
            "pre": [{"sig": sg, "lead": len(px) - 1 - i}
                    for sg, i in first.items() if len(px) - 1 - i > 0],
        }


def _drop_stats(sel_recs):
    """大幅下跌事件歸因：各訊號在下跌前出現的次數與中位提前天數。"""
    drops = [r for r in sel_recs if r.get("drop")]
    sigs = {}
    for sig, label in WARN_SIGS.items():
        pre = [p for r in drops for p in r["drop"]["pre"] if p["sig"] == sig]
        fired = sum(1 for r in sel_recs if sig in (r.get("sig_first") or {}))
        if fired:
            leads = sorted(p["lead"] for p in pre)
            sigs[sig] = {"label": label, "fired": fired, "pre_n": len(pre),
                         "med_lead": leads[len(leads) // 2] if leads else None}
    return {"n_drops": len(drops), "sigs": sigs}


def _suggestions(groups, min_n=10):
    """依分組報酬差異自動產生「可修正指標」建議（樣本各滿 min_n 筆才比較）。"""
    def best(g):
        st = groups.get(g) or {}
        return st.get("20") or st.get("now")

    out = []
    pairs = [
        ("K≤20進場", "K>20未進場(對照)", "「K值≤20 超賣進場」條件"),
        ("倍數4~5", "倍數3~4", "「合約負債倍數」偏好高倍"),
        ("技術多頭", "技術空頭", "「技術面多頭」傾向"),
        ("殖利率≥6%", "殖利率<3%", "「高殖利率」傾向"),
    ]
    for a, b, name in pairs:
        sa, sb = best(a), best(b)
        if not (sa and sb and sa["n"] >= min_n and sb["n"] >= min_n):
            continue
        diff = sa["avg"] - sb["avg"]
        if diff >= 2:
            out.append(f"✅ {name}有效：{a} 平均 {sa['avg']:+}%"
                       f" 優於 {b} {sb['avg']:+}%（樣本 {sa['n']}/{sb['n']}）")
        elif diff <= -2:
            out.append(f"🔧 建議檢討{name}：{a} 平均 {sa['avg']:+}%"
                       f" 反而落後 {b} {sb['avg']:+}%"
                       f"（樣本 {sa['n']}/{sb['n']}），可考慮放寬或調整門檻")
        else:
            out.append(f"➖ {name}差異不明顯：{sa['avg']:+}% vs {sb['avg']:+}%"
                       f"（樣本 {sa['n']}/{sb['n']}）")
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
        if rec["sel"]:                       # 入選組：累積價量/法人序列→下車警訊
            px = rec.setdefault("px", [])
            vx = rec.setdefault("vx", [])
            xh = rec.setdefault("xh", [])
            px.append(s["c"])
            vx.append(s.get("v") or 0)
            xh.append(s.get("x") or 0)
            del px[:-130], vx[:-130], xh[:-5]
            _check_warnings(rec)
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
            "drop_stats": _drop_stats(sel),
            "suggestions": _suggestions(_groups(recs)),
        },
    }
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    n_open = sum(1 for r in recs if not r.get("done"))
    log.info("選股成效追蹤：新增 %d 筆，追蹤中 %d 筆", added, n_open)
    return added, n_open
