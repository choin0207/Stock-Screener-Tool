# -*- coding: utf-8 -*-
"""一年歷史回測訓練：用過去約一年的實際資料「重演」每日 ①② 篩選，

1. 找出哪些因子真正預測「入選後 20 個交易日上漲」→ 產出評分權重，
   套回每日篩選結果的「評分」欄（可解釋的 IC 加權，不用黑箱模型）
2. 統計哪些指標最常在「跌破入選價 5%」之前出現（提前幾天、命中率）
   → 校準跌前警訊規則、在前端顯示歷史驗證過的警示指標排行

資料來源與限制：
- 三大法人 T86（上市）＋TPEx（上櫃）：逐日抓、逐日快取（.cache/），
  第一次跑約需 20-30 分鐘，之後增量
- 個股日K與成交量：Yahoo chart API，一檔一請求（range=15mo）
- 財報（④⑤）：MOPS 依「訊號當時已公布的季度」抓，有限速與上限
  （backtest_max_mops_pairs），超過上限的舊訊號不含 ④⑤ 因子
- 內部人轉讓（③）與集保大戶「無歷史資料」，無法回測

樣本一年僅數百筆，統計結果僅供參考，權重每次重跑會隨市場變動。
結果寫入 docs/data/backtest.json。
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:                                          # pragma: no cover
    ZoneInfo = None

from .config import CONFIG
from . import datasources as ds

log = logging.getLogger("screener.backtest")

TRACK = 20                 # 觀察入選後幾個交易日
DROP_PCT = -5.0            # 跌破入選價此 % 視為下跌事件（與 performance 一致）

# 評分權重使用的「每日篩選也算得出來」的因子
LIVE_FACTORS = ("inst_ratio", "net_ratio", "liab_ratio", "eps")

FACTOR_LABELS = {
    "inst_ratio": "外資+投信買超放大倍數（①）",
    "net_ratio": "法人買賣超放大倍數（②）",
    "inst_lots": "外資+投信買超張數",
    "mom20": "入選前20日漲幅",
    "vol_ratio": "入選日量能（對20日均量）",
    "liab_ratio": "合約負債/資本額（④）",
    "eps": "EPS（⑤）",
}

WARN_LABELS = {
    "w_foreign2": "外資連2日賣超",
    "w_trust2": "投信連2日賣超",
    "w_inst2": "法人合計連2日賣超",
    "w_foreign_big": "外資單日賣超吃掉入選日買超",
    "w_vol_dump": "爆量下跌（量>5日均2倍且跌>2%）",
    "w_ma5": "跌破5日均線",
    "w_streak3": "連3黑",
}


# ---------------------------------------------------------------------------
# 歷史資料收集
# ---------------------------------------------------------------------------

def _ymd_from_ts(ts):
    if ZoneInfo:
        return datetime.fromtimestamp(ts, ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
    return datetime.utcfromtimestamp(ts + 8 * 3600).strftime("%Y%m%d")


def _yahoo_daily(symbol, rng="15mo"):
    """回傳 {ymd: [close, volume]}（升冪），失敗回傳 {}。"""
    data = ds._get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": rng, "interval": "1d"})
    try:
        res = data["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        out = {}
        for i, t in enumerate(ts):
            c, v = q["close"][i], q["volume"][i]
            if c is None:
                continue
            out[_ymd_from_ts(t)] = [round(c, 2), round((v or 0) / 1000, 1)]
        return out
    except Exception:                                        # noqa: BLE001
        return {}


def _price_range():
    """依 backtest_days 選 Yahoo range：1年內用 15mo，3年用 5y。"""
    return "15mo" if CONFIG.get("backtest_days", 365) <= 380 else "5y"


def fetch_price_history(code, market=None):
    """個股日K歷史（收盤、成交張數）。逐檔快取（快取名含 range）。"""
    rng = _price_range()
    name = f"bt_px_{rng}_{code}.json"
    cached = ds._cache_load(name, max_age_hours=24 * 3)
    if cached is not None:
        return cached
    suffixes = [".TWO", ".TW"] if market == "otc" else [".TW", ".TWO"]
    px = {}
    for suf in suffixes:
        px = _yahoo_daily(code + suf, rng)
        if px:
            break
        time.sleep(0.5)
    ds._cache_save(name, px)
    time.sleep(0.4)
    return px


def trading_days(lookback_days):
    """由加權指數取得回測期間的交易日清單（升冪 ymd）。"""
    px = _yahoo_daily("%5ETWII", _price_range())
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    return sorted(d for d in px if d >= cutoff)


def load_t86_history(days, progress_every=20):
    """逐日抓上市+上櫃法人買賣超（單位：股）。回傳 {ymd: {code: {...}}}。"""
    hist = {}
    delay = CONFIG.get("backtest_request_delay_sec", 1.2)
    for i, ymd in enumerate(days):
        cached = ds._cache_load(f"t86_{ymd}.json") is not None
        merged = dict(ds.fetch_t86(ymd) or {})
        merged.update(ds.fetch_tpex_inst(ymd) or {})
        if merged:
            hist[ymd] = merged
        if not cached:
            time.sleep(delay)
        if (i + 1) % progress_every == 0:
            log.info("法人歷史 %d/%d 天（%s）", i + 1, len(days), ymd)
    return hist


# ---------------------------------------------------------------------------
# 重演 ①② 篩選 → 訊號
# ---------------------------------------------------------------------------

def replay_signals(days, t86_hist):
    """對每個交易日套用與每日篩選相同的 ①② 規則。回傳訊號清單。"""
    surge, net = CONFIG["inst_surge_ratio"], CONFIG["net_buy_ratio"]
    signals = []
    for i in range(1, len(days)):
        d_today, d_prev = days[i], days[i - 1]
        today, prev = t86_hist.get(d_today), t86_hist.get(d_prev)
        if not today or not prev:
            continue
        for code, cur in today.items():
            # 個股為 4 碼且不以 00 開頭（排除 0050 等 4 碼 ETF）
            if len(code) != 4 or not code.isdigit() or code.startswith("00"):
                continue
            p = prev.get(code)
            if not p:
                continue
            inst_t = cur["foreign_net"] + cur["trust_net"]
            inst_p = p["foreign_net"] + p["trust_net"]
            c1 = (cur["foreign_net"] > 0 and cur["trust_net"] > 0 and
                  inst_t > max(inst_p, 0) * surge and inst_t > 0)
            c2 = (cur["total_net"] > 0 and
                  cur["total_net"] > abs(p["total_net"]) * net)
            if not (c1 and c2):
                continue
            signals.append({
                "code": code, "date": d_today,
                "inst_ratio": inst_t / inst_p if inst_p > 0 else 10.0,
                "net_ratio": (cur["total_net"] / abs(p["total_net"])
                              if p["total_net"] else 10.0),
                "inst_lots": round(inst_t / 1000, 1),
                "entry_foreign_lots": round(cur["foreign_net"] / 1000, 1),
                "entry_trust_lots": round(cur["trust_net"] / 1000, 1),
            })
    return signals


# ---------------------------------------------------------------------------
# 歷史財報（訊號當時已公布的季度）
# ---------------------------------------------------------------------------

def published_period(d):
    """date → 該日已公布的最新一般季報 (民國年, 季)。
    公布期限：Q1=5/15、Q2=8/14、Q3=11/14、年報=3/31。"""
    y = d.year
    if d >= date(y, 11, 15):
        return y - 1911, 3
    if d >= date(y, 8, 15):
        return y - 1911, 2
    if d >= date(y, 5, 16):
        return y - 1911, 1
    if d >= date(y, 4, 1):
        return y - 1 - 1911, 4
    return y - 1 - 1911, 3


def fetch_fin_quarter(code, year, season):
    """抓指定季度的合約負債/股本/EPS（仟元）。逐 (code,季) 快取。"""
    name = f"bt_fin_{code}_{year}Q{season}.json"
    cached = ds._cache_load(name)
    if cached is not None:
        return cached
    result = {"contract_liab_k": None, "capital_k": None, "eps": None,
              "period": f"{year + 1911}Q{season}"}
    time.sleep(CONFIG["mops_delay_sec"])
    bs = ds._mops_post("ajax_t164sb03", code, year, season)
    if bs and "查無" not in bs:
        result["contract_liab_k"] = (
            ds._html_row_value(bs, "合約負債－流動")
            or ds._html_row_value(bs, "合約負債—流動")
            or ds._html_row_value(bs, "合約負債"))
        result["capital_k"] = (ds._html_row_value(bs, "股本合計")
                               or ds._html_row_value(bs, "普通股股本"))
        time.sleep(CONFIG["mops_delay_sec"])
        pl = ds._mops_post("ajax_t164sb04", code, year, season)
        result["eps"] = (ds._html_row_value(pl, "基本每股盈餘")
                         or ds._html_row_value(pl, "基本每股盈餘（元）"))
    ds._cache_save(name, result)
    return result


def attach_financials(signals):
    """為訊號補上「當時已公布」的 ④⑤ 因子，受 backtest_max_mops_pairs 上限。"""
    pairs = {}
    for s in signals:
        d = datetime.strptime(s["date"], "%Y%m%d").date()
        y, q = published_period(d)
        pairs.setdefault((s["code"], y, q), []).append(s)
    todo = sorted(pairs, key=lambda k: max(s["date"] for s in pairs[k]),
                  reverse=True)
    cap = CONFIG.get("backtest_max_mops_pairs", 250)
    skipped = max(0, len(todo) - cap)
    for n, key in enumerate(todo[:cap]):
        code, y, q = key
        fin = fetch_fin_quarter(code, y, q)
        for s in pairs[key]:
            cap_k = fin.get("capital_k")
            s["liab_ratio"] = (fin["contract_liab_k"] / cap_k
                               if fin.get("contract_liab_k") is not None
                               and cap_k else None)
            s["eps"] = fin.get("eps")
        if (n + 1) % 20 == 0:
            log.info("歷史財報 %d/%d 組", n + 1, min(cap, len(todo)))
    if skipped:
        log.warning("財報組合 %d 組超過上限 %d，%d 組較舊訊號不含④⑤因子",
                    len(todo), cap, skipped)
    return skipped


# ---------------------------------------------------------------------------
# 報酬結果與跌前指標
# ---------------------------------------------------------------------------

def attach_outcomes(signals, prices):
    """補上進場價、D+5/10/20 報酬、動能與量能因子、是否發生下跌事件。"""
    kept = []
    for s in signals:
        px = prices.get(s["code"]) or {}
        dates = sorted(px)
        if s["date"] not in px:
            continue
        i = dates.index(s["date"])
        entry = px[s["date"]][0]
        if not entry:
            continue
        s["entry_close"] = entry

        def ret(n):
            if i + n < len(dates):
                return round((px[dates[i + n]][0] - entry) / entry * 100, 2)
            return None

        s["ret5"], s["ret10"], s["ret20"] = ret(5), ret(10), ret(20)

        # 一週（5 個交易日）內走勢：最大漲幅、上漲天數、見高點日
        week = dates[i + 1:i + 6]
        ups, mx, peak_day, prev_c = 0, None, None, entry
        for j, d in enumerate(week):
            c = px[d][0]
            if c > prev_c:
                ups += 1
            prev_c = c
            g = (c - entry) / entry * 100
            if mx is None or g > mx:
                mx, peak_day = g, j + 1
        s["max_up5_pct"] = round(mx, 2) if mx is not None else None
        s["up_days5"] = ups if week else None
        s["peak_day5"] = peak_day

        # 一個月（20 個交易日）內首次漲達 5%/7%/10% 的天數（未達為 None）
        month = dates[i + 1:i + 1 + TRACK]
        for th, key in ((5, "hit5_day"), (7, "hit7_day"), (10, "hit10_day")):
            s[key] = next((j + 1 for j, d in enumerate(month)
                           if px[d][0] >= entry * (1 + th / 100)), None)
        win = dates[i + 1:i + 1 + TRACK]
        s["drop_date"] = next(
            (d for d in win
             if px[d][0] <= entry * (1 + DROP_PCT / 100)), None)
        s["mom20"] = (round((entry - px[dates[i - 20]][0])
                            / px[dates[i - 20]][0] * 100, 2)
                      if i >= 20 and px[dates[i - 20]][0] else None)
        vols = [px[d][1] for d in dates[max(0, i - 20):i] if px[d][1]]
        s["vol_ratio"] = (round(px[s["date"]][1] / (sum(vols) / len(vols)), 2)
                          if vols and px[s["date"]][1] else None)
        kept.append(s)
    return kept


def _flows_lots(t86_hist, ymd, code):
    d = (t86_hist.get(ymd) or {}).get(code)
    if not d:
        return None
    return (d["foreign_net"] / 1000, d["trust_net"] / 1000,
            d["total_net"] / 1000)


def eval_warning_indicators(signals, prices, t86_hist):
    """統計各跌前指標的命中率：precision=發警後真的跌的比例、
    recall=下跌事件中事先有警告的比例、lead=提前幾個交易日（中位數）。"""
    res = {k: {"fired": 0, "tp": 0, "fp": 0, "late": 0, "leads": []}
           for k in WARN_LABELS}
    drops_total = 0
    for s in signals:
        px = prices.get(s["code"]) or {}
        dates = sorted(px)
        if s["date"] not in px:
            continue
        i = dates.index(s["date"])
        win = dates[i:i + 1 + TRACK]
        entry = s["entry_close"]
        entry_f = s["entry_foreign_lots"]
        drop = s.get("drop_date")
        if drop:
            drops_total += 1

        fired = {}                      # key → 首次觸發日

        def fire(key, d):
            fired.setdefault(key, d)

        closes = [px[d][0] for d in win]
        vols = [px[d][1] for d in win]
        flows = [_flows_lots(t86_hist, d, s["code"]) for d in win]
        for j in range(1, len(win)):
            d = win[j]
            f_now, f_prev = flows[j], flows[j - 1]
            if f_now and f_prev:
                if f_now[0] < 0 and f_prev[0] < 0:
                    fire("w_foreign2", d)
                if f_now[1] < 0 and f_prev[1] < 0:
                    fire("w_trust2", d)
                if f_now[2] < 0 and f_prev[2] < 0:
                    fire("w_inst2", d)
            if f_now and entry_f > 0 and -f_now[0] >= entry_f:
                fire("w_foreign_big", d)
            chg = (closes[j] - closes[j - 1]) / closes[j - 1] * 100 \
                if closes[j - 1] else 0
            pv = [v for v in vols[max(0, j - 5):j] if v]
            if pv and vols[j] and vols[j] > 2 * sum(pv) / len(pv) and chg <= -2:
                fire("w_vol_dump", d)
            if j >= 5:
                ma5 = sum(closes[j - 4:j + 1]) / 5
                ma5p = sum(closes[j - 5:j]) / 5
                if closes[j] < ma5 and closes[j - 1] >= ma5p:
                    fire("w_ma5", d)
            if j >= 3 and closes[j] < closes[j - 1] < closes[j - 2] \
                    < closes[j - 3]:
                fire("w_streak3", d)

        for key, fd in fired.items():
            r = res[key]
            r["fired"] += 1
            if not drop:
                r["fp"] += 1
            elif fd <= drop:
                r["tp"] += 1
                r["leads"].append(win.index(drop) - win.index(fd))
            else:
                r["late"] += 1

    out = []
    for key, r in res.items():
        leads = sorted(r["leads"])
        out.append({
            "key": key, "label": WARN_LABELS[key],
            "fired": r["fired"], "tp": r["tp"], "fp": r["fp"],
            "late": r["late"], "drops_total": drops_total,
            "precision": round(r["tp"] / r["fired"] * 100, 1)
            if r["fired"] else None,
            "recall": round(r["tp"] / drops_total * 100, 1)
            if drops_total else None,
            "lead_median": leads[len(leads) // 2] if leads else None,
        })
    out.sort(key=lambda x: -(x["precision"] or 0))
    return out, drops_total


# ---------------------------------------------------------------------------
# 一週漲幅機率（依 ④⑤ 條件分組，供每日篩選結果註記「相似條件歷史表現」）
# ---------------------------------------------------------------------------

def _bucket(s):
    """依訊號當時的 ④⑤ 條件分組：45=皆符合 4=僅④ 5=僅⑤ 0=皆未達 u=財報未知。"""
    liab, eps = s.get("liab_ratio"), s.get("eps")
    if liab is None and eps is None:
        return "u"
    c4 = liab is not None and liab > CONFIG["contract_liability_capital_ratio"]
    c5 = eps is not None and eps > CONFIG["profitability_threshold"]
    return {"11": "45", "10": "4", "01": "5", "00": "0"}[
        f"{int(c4)}{int(c5)}"]


def week_stats(signals):
    """一週內漲 ≥2/4/6% 的歷史機率、平均上漲天數、最大漲幅中位數。"""
    def stat(rows):
        xs = [s for s in rows if s.get("max_up5_pct") is not None]
        if not xs:
            return {"n": 0}
        mx = sorted(s["max_up5_pct"] for s in xs)

        def p(th):
            return round(sum(1 for s in xs if s["max_up5_pct"] >= th)
                         / len(xs) * 100, 1)

        ups = [s["up_days5"] for s in xs if s.get("up_days5") is not None]
        peaks = [s["peak_day5"] for s in xs if s.get("peak_day5")]
        r5 = [s["ret5"] for s in xs if s.get("ret5") is not None]
        r10 = [s["ret10"] for s in xs if s.get("ret10") is not None]

        def pos(vals):
            return (round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
                    if vals else None)

        def hit(key):
            ds_ = sorted(s[key] for s in xs if s.get(key))
            return {"p": round(len(ds_) / len(xs) * 100, 1),
                    "med_days": ds_[len(ds_) // 2] if ds_ else None}

        return {
            "n": len(xs), "p2": p(2), "p4": p(4), "p6": p(6),
            "med_max_up": mx[len(mx) // 2],
            "avg_up_days": round(sum(ups) / len(ups), 1) if ups else None,
            "med_peak_day": sorted(peaks)[len(peaks) // 2] if peaks else None,
            "avg_ret5": round(sum(r5) / len(r5), 2) if r5 else None,
            # 一週/兩週後仍收漲的機率、連 5 個交易日全收紅的機率
            "w1_pos": pos(r5), "w2_pos": pos(r10),
            "streak5": (round(sum(1 for s in xs if s.get("up_days5") == 5)
                              / len(xs) * 100, 1) if xs else None),
            # 一個月內漲達 5%/7%/10% 的機率與中位所需交易日
            "m5": hit("hit5_day"), "m7": hit("hit7_day"),
            "m10": hit("hit10_day"),
        }

    # 第二維度：外資+投信買超規模三分位（張），讓相似條件更貼近個股
    xs = sorted(s["inst_lots"] for s in signals
                if s.get("inst_lots") is not None)
    cut1 = xs[len(xs) // 3] if xs else 0
    cut2 = xs[2 * len(xs) // 3] if xs else 0

    def size_bucket(s):
        v = s.get("inst_lots") or 0
        return "L" if v >= cut2 else ("M" if v >= cut1 else "S")

    buckets = {}
    for b in ("45", "4", "5", "0", "u"):
        rows = [s for s in signals if _bucket(s) == b]
        buckets[b] = stat(rows)
        for sb in ("S", "M", "L"):
            buckets[f"{b}|{sb}"] = stat(
                [s for s in rows if size_bucket(s) == sb])
    for sb in ("S", "M", "L"):
        buckets[sb] = stat([s for s in signals if size_bucket(s) == sb])

    return {"overall": stat(signals), "buckets": buckets,
            "inst_cuts": [round(cut1, 1), round(cut2, 1)]}


# ---------------------------------------------------------------------------
# 因子分析與權重
# ---------------------------------------------------------------------------

def _ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    rk = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            rk[order[k]] = avg
        i = j + 1
    return rk


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    if vx == 0 or vy == 0:
        return None
    return round(cov / vx / vy, 3)


def factor_analysis(signals):
    """各因子對 D+20 報酬的 IC 與高/低分組勝率；產出評分權重。"""
    rows = []
    for key in FACTOR_LABELS:
        pts = [(s[key], s["ret20"]) for s in signals
               if s.get(key) is not None and s.get("ret20") is not None]
        if len(pts) < 10:
            rows.append({"key": key, "label": FACTOR_LABELS[key],
                         "n": len(pts), "ic": None,
                         "top_win": None, "bot_win": None})
            continue
        xs, ys = zip(*pts)
        ic = spearman(list(xs), list(ys))
        srt = sorted(pts, key=lambda p: p[0])
        third = max(1, len(srt) // 3)
        bot, top = srt[:third], srt[-third:]
        rows.append({
            "key": key, "label": FACTOR_LABELS[key], "n": len(pts),
            "ic": ic,
            "top_win": round(sum(1 for p in top if p[1] > 0)
                             / len(top) * 100, 1),
            "bot_win": round(sum(1 for p in bot if p[1] > 0)
                             / len(bot) * 100, 1),
        })

    # 權重：live 因子的正 IC 正規化（負 IC 因子不給權重，避免反向解讀）
    ics = {r["key"]: max(r["ic"] or 0, 0) for r in rows
           if r["key"] in LIVE_FACTORS}
    tot = sum(ics.values())
    weights = ({k: round(v / tot, 3) for k, v in ics.items() if v > 0}
               if tot > 0 else {})
    return rows, weights


def live_score(r, weights):
    """用回測權重替每日篩選結果打 0-100 分（因子轉 0-1 後加權）。"""
    fp, tp = r.get("foreign_net_prev") or 0, r.get("trust_net_prev") or 0
    f, t = r.get("foreign_net") or 0, r.get("trust_net") or 0
    inst_t, inst_p = f + t, fp + tp
    xp, x = r.get("total_net_prev") or 0, r.get("total_net") or 0
    xs = {
        "inst_ratio": (min(inst_t / inst_p, 10) / 10 if inst_p > 0
                       else (1.0 if inst_t > 0 else 0.0)),
        "net_ratio": (min(x / xp, 10) / 10 if xp > 0
                      else (1.0 if x > 0 else 0.0)),
        "liab_ratio": (min((r.get("contract_liab_k") or 0)
                           / r["capital_k"], 3) / 3
                       if r.get("capital_k") else 0.0),
        "eps": min(max(r.get("eps") or 0, 0), 10) / 10,
    }
    tot = sum(weights.get(k, 0) for k in xs)
    if tot <= 0:
        return None
    return round(100 * sum(weights.get(k, 0) * v for k, v in xs.items())
                 / tot, 1)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_backtest():
    log.info("開始一年回測 ...")
    days = trading_days(CONFIG.get("backtest_days", 365))
    if len(days) < 60:
        raise RuntimeError("取不到足夠的交易日資料（^TWII）")
    log.info("回測期間 %s ~ %s（%d 個交易日）", days[0], days[-1], len(days))

    t86_hist = load_t86_history(days)
    signals = replay_signals(days, t86_hist)
    log.info("重演出 %d 筆 ①② 訊號（%d 檔）",
             len(signals), len({s['code'] for s in signals}))

    # 市場別（決定 Yahoo 後綴）：以現有快照為準，查不到就自動嘗試
    market = {}
    try:
        with open(os.path.join(CONFIG["data_dir"], "market_snapshot.json"),
                  encoding="utf-8") as fobj:
            market = {c: v.get("m") for c, v in
                      json.load(fobj).get("stocks", {}).items()}
    except Exception:                                        # noqa: BLE001
        pass
    prices = {}
    codes = sorted({s["code"] for s in signals})
    for n, code in enumerate(codes):
        prices[code] = fetch_price_history(code, market.get(code))
        if (n + 1) % 50 == 0:
            log.info("股價歷史 %d/%d 檔", n + 1, len(codes))

    signals = attach_outcomes(signals, prices)
    fin_skipped = attach_financials(signals)
    warn_rows, drops_total = eval_warning_indicators(signals, prices,
                                                     t86_hist)
    factor_rows, weights = factor_analysis(signals)

    r20 = [s["ret20"] for s in signals if s.get("ret20") is not None]
    outcome = {
        "n": len(signals), "n_ret20": len(r20),
        "win_rate_20d": round(sum(1 for v in r20 if v > 0)
                              / len(r20) * 100, 1) if r20 else None,
        "avg_ret20": round(sum(r20) / len(r20), 2) if r20 else None,
        "drops_total": drops_total,
        "drop_rate": round(drops_total / len(signals) * 100, 1)
        if signals else None,
    }
    now = (datetime.now(ZoneInfo(CONFIG["timezone"]))
           if ZoneInfo else datetime.now())
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "period": {"from": days[0], "to": days[-1], "days": len(days)},
        "outcome": outcome,
        "factors": factor_rows,
        "weights": weights,
        "warnings": warn_rows,
        "week_stats": week_stats(signals),
        "fin_pairs_skipped": fin_skipped,
        "note": ("樣本為近一年通過①②的訊號；④⑤用訊號當時已公布的季報，"
                 "③內部人與大戶無歷史資料未納入。樣本數有限，"
                 "權重與命中率僅供參考、非投資建議。"),
    }
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    with open(os.path.join(CONFIG["data_dir"], "backtest.json"), "w",
              encoding="utf-8") as fobj:
        json.dump(out, fobj, ensure_ascii=False, indent=1)
    log.info("回測完成：%d 筆訊號、20日勝率 %s%%、下跌事件 %d 次",
             outcome["n"], outcome["win_rate_20d"], drops_total)
    return out


def load_weights():
    """讀回測產出的評分權重（無檔案時回傳 None）。"""
    try:
        with open(os.path.join(CONFIG["data_dir"], "backtest.json"),
                  encoding="utf-8") as fobj:
            w = json.load(fobj).get("weights")
            return w or None
    except Exception:                                        # noqa: BLE001
        return None
