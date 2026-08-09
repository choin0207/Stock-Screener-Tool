# -*- coding: utf-8 -*-
"""一年歷史回測研究：用過去約一年的全市場資料，驗證「哪些指標值得加入篩選」
與「哪些訊號常出現在下跌之前」。

資料（逐日抓取並快取於 .cache/research/，重跑只補新交易日）：
- 上市收盤/成交量：TWSE MI_INDEX（單日全市場一次請求）
- 上櫃收盤/成交量：TPEx 新版 afterTrading API，失敗時退回舊版
  stk_wn1430（舊版僅取收盤、不取成交量）
- 三大法人買賣超：沿用 datasources.fetch_t86 / fetch_tpex_inst（本身有快取）

評估方法（全市場 4 碼個股、日頻）：
- 買進指標：訊號日收盤進場，看未來 5/10/20 個交易日報酬、D+20 勝率，
  與「全市場所有樣本點」基準比較（lift = 平均 D+20 報酬 − 基準）
- 跌前訊號：訊號日後 5 個交易日內收盤最低點跌逾 perf_drop_alert_pct(-5%)
  視為「下跌事件」，比較觸發後下跌機率與全市場基準（lift = 倍數）

結果寫入 docs/data/research.json，由網頁「指標回測研究」卡片顯示；
其中可即時計算的跌前訊號（法人連賣、外資/投信連賣、爆量下跌、高點回落）
已接入 performance.py 的追蹤股警示引擎，門檻定義與本回測一致。

已知限制（誠實標註於輸出 notes）：
- 條件④（合約負債）用「現行」財報資料庫回測過去一年，有前視偏誤，僅供參考
- 舊版 TPEx 備援無成交量，該日上櫃股的量能類指標不評估
- 統計未含交易成本與滑價；樣本僅一年，屬單一市況期間
"""

import json
import logging
import os
import time
from datetime import date, datetime, timedelta

from .config import CONFIG
from . import datasources as ds

log = logging.getLogger("screener.research")

FWD_MAX = 20                 # 買進指標最長評估天期
DROP_HORIZON = 5             # 跌前訊號：未來幾個交易日內
HISTORY_NEED = 60            # 評估起點需要的歷史天數（60日新高/均線）
_ERR_LIMIT = 6               # 連續網路失敗超過此數 → 中止（避免存到爛資料）


# ---------------------------------------------------------------------------
# 逐日資料抓取（含快取）
# ---------------------------------------------------------------------------

def _cache(name):
    d = os.path.join(CONFIG["cache_dir"], "research")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _cache_load(name):
    try:
        with open(_cache(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return None


def _cache_save(name, obj):
    with open(_cache(name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _sleep():
    time.sleep(CONFIG.get("research_delay_sec", 1.2))


def _twse_prices_day(ymd):
    """單日上市全市場 {code: [close, vol_lots]}。
    回傳 {}=非交易日（會快取）、None=網路/解析失敗（不快取，下次重試）。"""
    c = _cache_load(f"px_twse_{ymd}.json")
    if c is not None:
        return {} if c.get("empty") else c.get("c", {})

    data = ds._get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                        params={"date": ymd, "type": "ALLBUT0999",
                                "response": "json"})
    _sleep()
    if data is None:
        return None
    if data.get("stat") != "OK":
        _cache_save(f"px_twse_{ymd}.json", {"empty": 1})
        return {}

    best = None
    for t in data.get("tables", []) or [data]:
        fields = t.get("fields", [])
        if any("證券代號" in f for f in fields) and \
           any("收盤價" in f for f in fields):
            if best is None or len(t.get("data", [])) > len(best.get("data", [])):
                best = t
    if not best:
        return None
    fields = best["fields"]

    def col(kw):
        for i, f in enumerate(fields):
            if kw in f:
                return i
        return None

    ic, iv, ip = col("證券代號"), col("成交股數"), col("收盤價")
    out = {}
    for row in best.get("data", []):
        try:
            code = str(row[ic]).strip()
        except Exception:                                    # noqa: BLE001
            continue
        close = ds._num(row[ip]) if ip is not None and ip < len(row) else None
        vol = ds._num(row[iv]) if iv is not None and iv < len(row) else None
        if close is None:
            continue
        out[code] = [close, round(vol / 1000, 1) if vol else None]
    if not out:
        return None
    _cache_save(f"px_twse_{ymd}.json", {"c": out})
    return out


def _tpex_prices_day(ymd):
    """單日上櫃 {code: [close, vol_lots]}。失敗回傳 {}（不影響上市評估）。"""
    c = _cache_load(f"px_tpex_{ymd}.json")
    if c is not None:
        return {} if c.get("empty") else c.get("c", {})

    roc = f"{int(ymd[:4]) - 1911}/{ymd[4:6]}/{ymd[6:]}"
    headers = {"Referer": "https://www.tpex.org.tw/"}
    out = {}

    # 新版 API：tables[].fields 有欄位名稱
    for path in ("www/zh-tw/afterTrading/dailyQuotes",
                 "www/zh-tw/afterTrading/otc"):
        data = None
        try:
            r = ds._session().get(f"https://www.tpex.org.tw/{path}",
                                  params={"date": roc, "type": "EW",
                                          "response": "json"},
                                  headers=headers,
                                  timeout=CONFIG["request_timeout"])
            r.raise_for_status()
            data = r.json()
        except Exception as e:                               # noqa: BLE001
            log.debug("TPEx 歷史行情 %s 失敗: %s", path, e)
        _sleep()
        for t in (data or {}).get("tables", []):
            fields = t.get("fields", [])

            def col(*kws):
                for i, f in enumerate(fields):
                    if all(k in f for k in kws):
                        return i
                return None

            ic, ip, iv = col("代號"), col("收盤"), col("成交股數")
            if ic is None or ip is None or not t.get("data"):
                continue
            for row in t["data"]:
                code = str(row[ic]).strip()
                close = ds._num(row[ip]) if ip < len(row) else None
                vol = ds._num(row[iv]) if iv is not None and iv < len(row) \
                    else None
                if close is None or not code:
                    continue
                out[code] = [close,
                             round(vol / 1000, 1) if vol else None]
            if out:
                break
        if out:
            break

    if not out:
        # 舊版備援：aaData 固定欄位（0代號 1名稱 2收盤），成交量欄位
        # 各版面不一致，僅取收盤，量設 None（量能類指標該日不評估）
        try:
            r = ds._session().get(
                "https://www.tpex.org.tw/web/stock/aftertrading/"
                "otc_quotes_no1430/stk_wn1430_result.php",
                params={"l": "zh-tw", "d": roc, "se": "EW", "o": "json"},
                headers=headers, timeout=CONFIG["request_timeout"])
            r.raise_for_status()
            for row in r.json().get("aaData", []):
                code = str(row[0]).strip()
                close = ds._num(row[2]) if len(row) > 2 else None
                if code and close is not None:
                    out[code] = [close, None]
        except Exception as e:                               # noqa: BLE001
            log.warning("TPEx 歷史行情（舊版備援）%s 失敗: %s", ymd, e)
        _sleep()

    _cache_save(f"px_tpex_{ymd}.json", {"c": out} if out else {"empty": 1})
    return out


def _flows_day(ymd):
    """單日全市場法人買賣超 {code: [外資, 投信, 合計]}（張）。"""
    out = {}
    for fetch in (ds.fetch_t86, ds.fetch_tpex_inst):
        try:
            d = fetch(ymd)
        except Exception:                                    # noqa: BLE001
            d = {}
        _sleep()
        for code, v in (d or {}).items():
            out[code] = [round(v.get("foreign_net", 0) / 1000, 1),
                         round(v.get("trust_net", 0) / 1000, 1),
                         round(v.get("total_net", 0) / 1000, 1)]
    return out


def build_dataset(lookback_days=None, progress_every=20):
    """掃過去 lookback_days 個日曆天，回傳
    {"days": [ymd...], "px": {code: [[close, vol]...]}, "fl": {code: [[f,t,x]...]}}
    px/fl 的內層 list 與 days 對齊，缺資料處為 None。"""
    lookback = lookback_days or CONFIG.get("research_lookback_days", 400)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback)

    day_px, day_fl, days = [], [], []
    errors = 0
    d = start
    while d <= end:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        ymd = d.strftime("%Y%m%d")
        twse = _twse_prices_day(ymd)
        if twse is None:
            errors += 1
            if errors >= _ERR_LIMIT:
                raise RuntimeError(
                    f"連續 {errors} 個交易日抓取失敗（最後：{ymd}），"
                    "可能碰上維護時段或被限流，請稍後重跑")
            log.warning("%s 上市行情抓取失敗，本次跳過（下次重試）", ymd)
            d += timedelta(days=1)
            continue
        errors = 0
        if twse:                              # 有上市收盤 → 交易日
            px = dict(twse)
            px.update(_tpex_prices_day(ymd))
            days.append(ymd)
            day_px.append(px)
            day_fl.append(_flows_day(ymd))
            if len(days) % progress_every == 0:
                log.info("資料收集進度：%d 個交易日（至 %s）", len(days), ymd)
        d += timedelta(days=1)

    # 轉成以個股為主的對齊陣列（只留 4 碼個股且歷史足夠者）
    n = len(days)
    counts = {}
    for px in day_px:
        for code in px:
            if len(code) == 4 and code.isdigit():
                counts[code] = counts.get(code, 0) + 1
    min_hist = CONFIG.get("research_min_history", 120)
    keep = {c for c, k in counts.items() if k >= min_hist}

    px_panel = {c: [None] * n for c in keep}
    fl_panel = {c: [None] * n for c in keep}
    for i in range(n):
        for code, v in day_px[i].items():
            if code in keep:
                px_panel[code][i] = v
        for code, v in day_fl[i].items():
            if code in keep:
                fl_panel[code][i] = v
    log.info("資料集：%d 個交易日、%d 檔個股（歷史 ≥%d 日）",
             n, len(keep), min_hist)
    return {"days": days, "px": px_panel, "fl": fl_panel}


# ---------------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------------

def _series(px_row, fl_row):
    """單一個股的對齊序列 → dict of arrays（含衍生指標）。"""
    n = len(px_row)
    close = [p[0] if p else None for p in px_row]
    vol = [p[1] if p else None for p in px_row]
    f = [x[0] if x else None for x in fl_row]
    t = [x[1] if x else None for x in fl_row]
    x = [x[2] if x else None for x in fl_row]

    def sma(arr, w):
        out = [None] * n
        s, cnt = 0.0, 0
        buf = []
        for i, v in enumerate(arr):
            buf.append(v)
            if v is not None:
                s += v
                cnt += 1
            if len(buf) > w:
                old = buf.pop(0)
                if old is not None:
                    s -= old
                    cnt -= 1
            if cnt >= max(3, w // 2):        # 容忍少量缺值
                out[i] = s / cnt
        return out

    ma5, ma20 = sma(close, 5), sma(close, 20)
    v20 = sma(vol, 20)

    rsi = [None] * n
    gains, losses = [None] * n, [None] * n
    for i in range(1, n):
        if close[i] is not None and close[i - 1] is not None:
            ch = close[i] - close[i - 1]
            gains[i] = max(ch, 0.0)
            losses[i] = max(-ch, 0.0)
    g14, l14 = sma(gains, 14), sma(losses, 14)
    for i in range(n):
        if g14[i] is not None and l14[i] is not None:
            rsi[i] = 100.0 if l14[i] == 0 else \
                100.0 - 100.0 / (1.0 + g14[i] / l14[i])

    return {"close": close, "vol": vol, "f": f, "t": t, "x": x,
            "ma5": ma5, "ma20": ma20, "v20": v20, "rsi": rsi}


def _hi(close, i, w):
    """close[i-w:i] 的最高（不含 i），全 None 回傳 None。"""
    vals = [v for v in close[max(0, i - w):i] if v is not None]
    return max(vals) if vals else None


# 買進候選指標：fn(S, i) → bool（close[i] 已確認非 None）
def _buy_factors():
    cfg = CONFIG

    def cond12(S, i):
        f, t, x = S["f"][i], S["t"][i], S["x"][i]
        if None in (f, t, x) or S["fl_prev"] is None:
            return False
        f0, t0, x0 = S["fl_prev"]
        inst, inst0 = f + t, f0 + t0
        c1 = f > 0 and t > 0 and inst > 0 and \
            inst > max(inst0, 0) * cfg["inst_surge_ratio"]
        c2 = x0 > 0 and x > x0 * cfg["net_buy_ratio"]
        return c1 and c2

    def volsurge_up(S, i):
        v, v20, c, c0 = S["vol"][i], S["v20"][i - 1] if i else None, \
            S["close"][i], S["close"][i - 1] if i else None
        return None not in (v, v20, c, c0) and v20 > 0 and \
            v >= 3.0 * v20 and c > c0

    def cond12_vol(S, i):
        v, v20 = S["vol"][i], S["v20"][i - 1] if i else None
        return cond12(S, i) and None not in (v, v20) and v20 > 0 and \
            v >= 2.0 * v20

    def streak(arr, i, k):
        return i >= k - 1 and all(arr[j] is not None and arr[j] > 0
                                  for j in range(i - k + 1, i + 1))

    return [
        ("cond12", "現行①+②（外投同買放大＋法人放大）",
         "現行篩選的法人動能門檻，作為比較基準", cond12),
        ("cond12_vol", "①+② 且量增2倍",
         "現行門檻再加「成交量 ≥ 20日均量2倍」", cond12_vol),
        ("foreign3", "外資連3日買超",
         "外資買賣超連續 3 個交易日為正",
         lambda S, i: streak(S["f"], i, 3)),
        ("trust3", "投信連3日買超",
         "投信買賣超連續 3 個交易日為正",
         lambda S, i: streak(S["t"], i, 3)),
        ("inst_heavy", "法人買超佔量2成",
         "三大法人合計買超 ≥ 當日成交量 20%",
         lambda S, i: None not in (S["x"][i], S["vol"][i]) and
         S["vol"][i] and S["vol"][i] > 0 and S["x"][i] > 0 and
         S["x"][i] >= 0.2 * S["vol"][i]),
        ("volsurge_up", "爆量上漲",
         "成交量 ≥ 20日均量3倍且收紅", volsurge_up),
        ("high60", "創60日新高",
         "收盤突破過去 60 個交易日高點",
         lambda S, i: _hi(S["close"], i, 60) is not None and
         S["close"][i] > _hi(S["close"], i, 60)),
        ("golden", "5日均線上穿20日",
         "MA5 由下往上穿越 MA20",
         lambda S, i: i > 0 and
         None not in (S["ma5"][i], S["ma20"][i],
                      S["ma5"][i - 1], S["ma20"][i - 1]) and
         S["ma5"][i - 1] <= S["ma20"][i - 1] and S["ma5"][i] > S["ma20"][i]),
        ("rsi30", "RSI 低於30（超賣）",
         "14日 RSI < 30",
         lambda S, i: S["rsi"][i] is not None and S["rsi"][i] < 30),
        ("bias_neg", "負乖離逾10%",
         "收盤低於 20 日均線 10% 以上",
         lambda S, i: S["ma20"][i] is not None and S["ma20"][i] > 0 and
         S["close"][i] / S["ma20"][i] - 1 <= -0.10),
    ]


# 跌前訊號候選：fn(S, i) → bool。live= 已接入警示引擎的 performance 因素鍵
def _drop_factors():
    def sell2(arr, i):
        return i >= 1 and all(arr[j] is not None and arr[j] < 0
                              for j in (i - 1, i))

    def bigsell_vol(S, i):
        v, v20 = S["vol"][i], S["v20"][i - 1] if i else None
        c, c0 = S["close"][i], S["close"][i - 1] if i else None
        if None in (v, v20, c, c0) or v20 <= 0 or c0 <= 0:
            return False
        ratio = CONFIG.get("perf_tech_vol_ratio", 3.0)
        down = CONFIG.get("perf_tech_down_pct", -3.0)
        return v >= ratio * v20 and (c / c0 - 1) * 100 <= down

    def dd_high(S, i):
        hi = _hi(S["close"], i, 20)
        if hi is None or hi <= 0:
            return False
        return (S["close"][i] / hi - 1) * 100 <= \
            CONFIG.get("perf_tech_dd_pct", -8.0)

    return [
        ("inst_sell2", "法人連2日賣超",
         "三大法人合計連 2 日賣超（警示引擎現行因素）", "inst",
         lambda S, i: sell2(S["x"], i)),
        ("foreign_sell2", "外資連2日賣超",
         "外資連 2 日賣超（警示引擎現行因素）", "foreign",
         lambda S, i: sell2(S["f"], i)),
        ("trust_sell2", "投信連2日賣超",
         "投信連 2 日賣超（警示引擎現行因素）", "trust",
         lambda S, i: sell2(S["t"], i)),
        ("bigsell_vol", "爆量下跌",
         "量 ≥ 20日均量3倍且單日跌逾3%", "tech_voldown", bigsell_vol),
        ("dd_high", "自20日高點回落8%",
         "收盤自近 20 日高點回落 8% 以上", "tech_dd", dd_high),
        ("break_ma20", "跌破20日均線",
         "收盤由上往下跌破 MA20", None,
         lambda S, i: i > 0 and
         None not in (S["ma20"][i], S["ma20"][i - 1],
                      S["close"][i - 1]) and
         S["close"][i - 1] >= S["ma20"][i - 1] and
         S["close"][i] < S["ma20"][i]),
        ("rsi_fall70", "RSI 自70高檔回落",
         "14日 RSI 由 70 以上跌回 70 以下", None,
         lambda S, i: i > 0 and None not in (S["rsi"][i], S["rsi"][i - 1])
         and S["rsi"][i - 1] >= 70 > S["rsi"][i]),
        ("bias_high", "正乖離逾15%（過熱）",
         "收盤高於 20 日均線 15% 以上", None,
         lambda S, i: S["ma20"][i] is not None and S["ma20"][i] > 0 and
         S["close"][i] / S["ma20"][i] - 1 >= 0.15),
        ("down3", "連3日下跌",
         "收盤連續 3 個交易日走低", None,
         lambda S, i: i >= 3 and
         all(S["close"][j] is not None for j in range(i - 3, i + 1)) and
         all(S["close"][j] < S["close"][j - 1]
             for j in range(i - 2, i + 1))),
    ]


# ---------------------------------------------------------------------------
# 評估
# ---------------------------------------------------------------------------

def _fwd_returns(close, i):
    """(fwd5, fwd10, fwd20, 未來5日內最低收盤跌幅%)；缺資料處為 None。"""
    base = close[i]
    out = []
    for h in (5, 10, 20):
        c = close[i + h] if i + h < len(close) else None
        out.append((c / base - 1) * 100 if c is not None else None)
    lows = [c for c in close[i + 1:i + 1 + DROP_HORIZON] if c is not None]
    low = (min(lows) / base - 1) * 100 if len(lows) >= 3 else None
    return out[0], out[1], out[2], low


class _Acc:
    """單一指標/基準的統計累加器。"""

    def __init__(self):
        self.n = 0
        self.s5 = self.n5 = self.s10 = self.n10 = self.s20 = self.n20 = 0
        self.win20 = 0
        self.drop_n = self.drop_hit = 0

    def add(self, f5, f10, f20, low):
        self.n += 1
        if f5 is not None:
            self.s5 += f5
            self.n5 += 1
        if f10 is not None:
            self.s10 += f10
            self.n10 += 1
        if f20 is not None:
            self.s20 += f20
            self.n20 += 1
            if f20 > 0:
                self.win20 += 1
        if low is not None:
            self.drop_n += 1
            if low <= CONFIG["perf_drop_alert_pct"]:
                self.drop_hit += 1

    def out(self):
        def avg(s, n):
            return round(s / n, 2) if n else None
        return {
            "n": self.n,
            "fwd5": avg(self.s5, self.n5),
            "fwd10": avg(self.s10, self.n10),
            "fwd20": avg(self.s20, self.n20),
            "win20": round(self.win20 / self.n20 * 100, 1)
            if self.n20 else None,
            "drop_rate": round(self.drop_hit / self.drop_n * 100, 1)
            if self.drop_n else None,
        }


def evaluate(dataset, financials=None):
    """跑完整評估，回傳 research.json 的內容 dict。"""
    days, px, fl = dataset["days"], dataset["px"], dataset["fl"]
    n = len(days)
    min_vol = CONFIG.get("research_min_vol_lots", 30)
    buy_defs, drop_defs = _buy_factors(), _drop_factors()

    base = _Acc()
    buy_acc = {k: _Acc() for k, *_ in buy_defs}
    drop_acc = {k: _Acc() for k, *_ in drop_defs}
    c4_acc = {"0.5": _Acc(), "1.0": _Acc()}
    c4_sets = {"0.5": set(), "1.0": set()}
    for code, fin in (financials or {}).items():
        liab, cap = fin.get("contract_liab_k"), fin.get("capital_k")
        if liab is None or not cap:
            continue
        if liab > cap * 0.5:
            c4_sets["0.5"].add(code)
        if liab > cap * 1.0:
            c4_sets["1.0"].add(code)

    stocks_used = 0
    for code, px_row in px.items():
        vols = sorted(v[1] for v in px_row if v and v[1] is not None)
        if not vols or vols[len(vols) // 2] < min_vol:
            continue                        # 過濾極低流動性股（統計噪音）
        stocks_used += 1
        S = _series(px_row, fl[code])
        close = S["close"]
        for i in range(HISTORY_NEED, n - 1):
            if close[i] is None:
                continue
            f5, f10, f20, low = _fwd_returns(close, i)
            if f5 is None and low is None:
                continue
            base.add(f5, f10, f20, low)
            for t4 in ("0.5", "1.0"):
                if code in c4_sets[t4]:
                    c4_acc[t4].add(f5, f10, f20, low)
            # 前一日法人資料（cond12 用）
            S["fl_prev"] = fl[code][i - 1]
            for key, _, _, fn in buy_defs:
                try:
                    if fn(S, i):
                        buy_acc[key].add(f5, f10, f20, low)
                except Exception:                            # noqa: BLE001
                    pass
            for key, _, _, _, fn in drop_defs:
                try:
                    if fn(S, i):
                        drop_acc[key].add(f5, f10, f20, low)
                except Exception:                            # noqa: BLE001
                    pass

    b = base.out()
    min_n = CONFIG.get("research_min_samples", 150)
    buy_lift_pp = CONFIG.get("research_buy_lift_pp", 1.0)
    drop_lift_x = CONFIG.get("research_drop_lift", 1.3)

    buy_out = []
    for key, label, desc, _ in buy_defs:
        o = buy_acc[key].out()
        lift = (round(o["fwd20"] - b["fwd20"], 2)
                if o["fwd20"] is not None and b["fwd20"] is not None else None)
        buy_out.append({"key": key, "label": label, "desc": desc, **o,
                        "lift_pp": lift,
                        "adopted": bool(lift is not None and o["n"] >= min_n
                                        and lift >= buy_lift_pp)})
    buy_out.sort(key=lambda r: (r["lift_pp"] is None,
                                -(r["lift_pp"] or 0)))

    drop_out = []
    for key, label, desc, live, _ in drop_defs:
        o = drop_acc[key].out()
        lift = (round(o["drop_rate"] / b["drop_rate"], 2)
                if o["drop_rate"] is not None and b["drop_rate"] else None)
        drop_out.append({"key": key, "label": label, "desc": desc,
                         "live": live, **o, "lift_x": lift,
                         "adopted": bool(lift is not None and o["n"] >= min_n
                                         and lift >= drop_lift_x)})
    drop_out.sort(key=lambda r: (r["lift_x"] is None, -(r["lift_x"] or 0)))

    cond4 = {t: {**c4_acc[t].out(), "stocks": len(c4_sets[t])}
             for t in c4_acc}

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": days[0] if days else None,
                   "end": days[-1] if days else None,
                   "trading_days": n},
        "universe": {"stocks": stocks_used,
                     "eval_points": base.n,
                     "min_vol_lots": min_vol},
        "baseline": b,
        "buy_factors": buy_out,
        "drop_factors": drop_out,
        "cond4_test": cond4,
        "drop_event_def": f"訊號後 {DROP_HORIZON} 個交易日內收盤最低點跌逾 "
                          f"{-CONFIG['perf_drop_alert_pct']:.0f}%",
        "notes": [
            "買進指標以訊號日收盤進場、未含交易成本；lift_pp = 平均 D+20 "
            "報酬減去全市場基準（百分點）。",
            "跌前訊號 lift_x = 觸發後下跌機率 ÷ 全市場基準機率（倍）。",
            "條件④檢驗用「現行」財報資料庫回測過去一年，有前視偏誤，"
            "數字僅供方向參考。",
            "上櫃行情若走舊版備援僅有收盤價，該日上櫃股不評估量能類指標。",
            "樣本僅約一年、屬單一市況期間；統計顯著性有限，"
            "建議至少每季重跑一次觀察穩定度。",
        ],
    }


def run():
    """完整流程：收集資料 → 評估 → 寫 docs/data/research.json。"""
    dataset = build_dataset()
    if len(dataset["days"]) < 100:
        raise RuntimeError(
            f"僅收集到 {len(dataset['days'])} 個交易日，資料不足以回測")
    try:
        with open(os.path.join(CONFIG["data_dir"], "financials.json"),
                  encoding="utf-8") as f:
            financials = json.load(f)
    except Exception:                                        # noqa: BLE001
        financials = {}
    result = evaluate(dataset, financials)
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    out = os.path.join(CONFIG["data_dir"], "research.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    log.info("回測完成：%d 檔、%d 個樣本點 → %s",
             result["universe"]["stocks"],
             result["universe"]["eval_points"], out)
    return result
