# -*- coding: utf-8 -*-
"""技術面判讀：對篩選結果與自選股計算 均線排列/KD/MACD/趨勢高低點結構/量價關係。

使用者需求（2026-10-02）：篩選股票太多無法判斷，加上技術指標輔助過濾。
資料來源：Yahoo chart API 6 個月日K（一檔一請求，約 0.35s/檔），
於每日篩選成功後執行（獨立 try/except，失敗不影響篩選）。
產出 docs/data/technical.json，前端選股/自選/漲停/開盤強勢各分頁共用。"""

import json
import logging
import os
import time

from . import datasources as ds
from .config import CONFIG

log = logging.getLogger("screener.technical")

MAX_CODES = 250          # 一次最多算幾檔（篩選結果靠前者優先）
DELAY_SEC = 0.35


def _data_path(name):
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], name)


def fetch_daily_k(code, market="tse"):
    """抓 6 個月日K。回傳 (closes, highs, lows, vols)；失敗回傳 None。"""
    sfx = ".TWO" if market == "otc" else ".TW"
    data = ds._get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{sfx}",
        params={"range": "6mo", "interval": "1d"})
    try:
        q = data["chart"]["result"][0]["indicators"]["quote"][0]
        rows = [(c, h, l, v or 0) for c, h, l, v in
                zip(q["close"], q["high"], q["low"], q["volume"])
                if c is not None and h is not None and l is not None]
        if len(rows) < 65:                       # 需足夠樣本算 60 日均線
            return None
        return ([r[0] for r in rows], [r[1] for r in rows],
                [r[2] for r in rows], [r[3] for r in rows])
    except Exception:                                        # noqa: BLE001
        return None


def _ma(xs, n):
    return [sum(xs[i - n + 1:i + 1]) / n if i >= n - 1 else None
            for i in range(len(xs))]


def _ema(xs, n):
    k = 2 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _kd(closes, highs, lows, n=9):
    """標準 KD(9)：K/D 以 1/3 平滑，初始 50。"""
    k = d = 50.0
    ks, dvs = [], []
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1):i + 1])
        hi = max(highs[max(0, i - n + 1):i + 1])
        rsv = (closes[i] - lo) / (hi - lo) * 100 if hi > lo else 50.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        dvs.append(d)
    return ks, dvs


def _macd(closes):
    dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    sig = _ema(dif, 9)
    hist = [a - b for a, b in zip(dif, sig)]
    return dif, sig, hist


def _cross(fast, slow, lookback=3):
    """近 lookback 日內 fast 是否上穿/下穿 slow。回傳 'gc'/'dc'/None。"""
    for i in range(1, min(lookback, len(fast) - 1) + 1):
        if fast[-i] is None or slow[-i] is None \
                or fast[-i - 1] is None or slow[-i - 1] is None:
            continue
        if fast[-i] > slow[-i] and fast[-i - 1] <= slow[-i - 1]:
            return "gc"
        if fast[-i] < slow[-i] and fast[-i - 1] >= slow[-i - 1]:
            return "dc"
    return None


def analyze(closes, highs, lows, vols):
    """回傳單檔技術面摘要 dict（欄位說明見 update() docstring）。"""
    ma5, ma20, ma60 = _ma(closes, 5), _ma(closes, 20), _ma(closes, 60)
    # 均線排列：多頭=5>20>60 且均線向上；空頭相反
    align = 0
    if ma5[-1] > ma20[-1] > ma60[-1] and ma5[-1] > ma5[-2] \
            and ma20[-1] > ma20[-2] and ma60[-1] >= ma60[-2]:
        align = 1
    elif ma5[-1] < ma20[-1] < ma60[-1] and ma5[-1] < ma5[-2] \
            and ma20[-1] < ma20[-2]:
        align = -1
    ma_x = _cross(ma5, ma20)

    ks, dvs = _kd(closes, highs, lows)
    kd_x = _cross(ks, dvs)
    kd_zone = "ob" if ks[-1] > 80 else ("os" if ks[-1] < 20 else None)
    # 鈍化：連 3 日 K>80（強勢）或 K<20（弱勢）
    blunt = None
    if all(v > 80 for v in ks[-3:]):
        blunt = "high"
    elif all(v < 20 for v in ks[-3:]):
        blunt = "low"

    dif, sig, hist = _macd(closes)
    macd_zero = 1 if (dif[-1] > 0 and sig[-1] > 0) else \
                (-1 if (dif[-1] < 0 and sig[-1] < 0) else 0)
    macd_x = _cross(dif, sig)
    hist_up = hist[-1] > hist[-2]

    # 趨勢高低點結構：近20日高低點 vs 前20日（更高高點+更高低點=爬樓梯）
    trend = "flat"
    if len(highs) >= 40:
        h2, h1 = max(highs[-20:]), max(highs[-40:-20])
        l2, l1 = min(lows[-20:]), min(lows[-40:-20])
        if h2 > h1 and l2 > l1:
            trend = "hh"                         # 更高高點＋更高低點
        elif h2 < h1 and l2 < l1:
            trend = "ll"                         # 更低高點＋更低低點

    # 量價關係（最近一日 vs 前5日均量）
    v5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 and sum(vols[-6:-1]) else None
    chg = closes[-1] / closes[-2] - 1 if closes[-2] else 0
    vp = "中性"
    if v5:
        r = vols[-1] / v5
        if chg > 0.005 and r > 1.2:
            vp = "價漲量增"
        elif chg > 0.005 and r < 0.8:
            vp = "價漲量縮"
        elif abs(chg) < 0.01 and r > 2:
            vp = "爆量不漲"
        elif chg < -0.005 and r > 1.5:
            vp = "價跌量增"

    # 綜合判定：均線排列 ±2、趨勢結構 ±1、MACD零軸 ±1、動能 ±0.5、量價 ±0.5
    sc = align * 2 + {"hh": 1, "ll": -1}.get(trend, 0) + macd_zero \
        + (0.5 if hist_up else -0.5) \
        + {"價漲量增": 0.5, "價跌量增": -0.5, "爆量不漲": -0.5}.get(vp, 0)
    summary = "bull" if sc >= 2 else ("bear" if sc <= -2 else "neutral")

    return {
        "sum": summary, "sc": round(sc, 1),
        "ma": {"a": align, "x": ma_x,
               "v": [round(ma5[-1], 2), round(ma20[-1], 2),
                     round(ma60[-1], 2)]},
        "kd": {"k": round(ks[-1], 1), "d": round(dvs[-1], 1),
               "x": kd_x, "z": kd_zone, "b": blunt},
        "macd": {"zero": macd_zero, "x": macd_x, "hup": hist_up,
                 "h": round(hist[-1], 3)},
        "tr": trend, "vp": vp,
    }


def update(codes, market_map=None, trade_date=""):
    """對 codes（依優先序，超過 MAX_CODES 截斷）計算技術面，
    寫 docs/data/technical.json：{generated_at, trade_date, stocks:{code:{...}}}。
    欄位：sum=bull/neutral/bear 綜合、sc=分數、ma.a=排列(1/0/-1)、
    ma.x/kd.x/macd.x=近3日金叉gc/死叉dc、kd.z=ob超買/os超賣、kd.b=鈍化、
    macd.zero=零軸上下、macd.hup=柱體增長、tr=hh爬樓梯/ll下樓梯/flat、
    vp=量價關係。回傳成功檔數。"""
    market_map = market_map or {}
    seen, order = set(), []
    for c in codes:
        if len(c) == 4 and c.isdigit() and not c.startswith("00") \
                and c not in seen:
            seen.add(c)
            order.append(c)
    order = order[:MAX_CODES]
    stocks = {}
    for c in order:
        k = fetch_daily_k(c, market_map.get(c, "tse"))
        if k:
            try:
                stocks[c] = analyze(*k)
            except Exception:                                # noqa: BLE001
                log.warning("技術面計算失敗 %s", c)
        time.sleep(DELAY_SEC)
    from . import performance
    with open(_data_path("technical.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at":
                   performance._now().isoformat(timespec="seconds"),
                   "trade_date": trade_date, "stocks": stocks},
                  f, ensure_ascii=False)
    log.info("技術面判讀 %d/%d 檔", len(stocks), len(order))
    return len(stocks)
