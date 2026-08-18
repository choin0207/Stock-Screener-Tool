# -*- coding: utf-8 -*-
"""台股公開資料來源抓取。

資料來源（皆為官方公開資料）：
  * 台灣證交所 OpenAPI / RWD API  — 收盤行情、三大法人買賣超(T86)、持股轉讓申報
  * 集保結算所 開放資料           — 集保戶股權分散表（週資料，第 15 級 = 1,000 張以上大戶）
  * 公開資訊觀測站 (MOPS)         — 個別公司資產負債表（合約負債、股本）、綜合損益表（EPS）
  * 證交所行情資訊 (mis.twse.com.tw) — 盤中即時累積成交量

所有函式失敗時回傳空 dict / list，由呼叫端決定如何處理，避免單一來源故障
讓整個篩選中斷。
"""

import csv
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

import requests

from .config import CONFIG

log = logging.getLogger("screener.datasources")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def _get_json(url, params=None, timeout=None):
    try:
        r = _session().get(url, params=params,
                           timeout=timeout or CONFIG["request_timeout"])
        r.raise_for_status()
        return r.json()
    except Exception as e:                                   # noqa: BLE001
        log.warning("GET %s 失敗: %s", url, e)
        return None


def _num(s):
    """把 '1,234' / '1234.5' / '--' 之類的字串轉成 float，失敗回傳 None。"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "-", "N/A", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 快取
# ---------------------------------------------------------------------------

def _cache_path(name):
    d = CONFIG["cache_dir"]
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _cache_load(name, max_age_hours=None):
    p = _cache_path(name)
    if not os.path.exists(p):
        return None
    if max_age_hours is not None:
        age = time.time() - os.path.getmtime(p)
        if age > max_age_hours * 3600:
            return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return None


def _cache_save(name, obj):
    with open(_cache_path(name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. 收盤行情（上市全部個股）
# ---------------------------------------------------------------------------

def _roc_ymd(s):
    """'1150818' / '115/08/18' → '20260818'；解析失敗回傳 None。"""
    s = str(s or "").replace("/", "").strip()
    if len(s) == 7 and s.isdigit():
        return str(int(s[:3]) + 1911) + s[3:]
    if len(s) == 8 and s.isdigit():
        return s
    return None


def fetch_daily_quotes():
    """回傳 ({代號: {name, close, change, volume_lots, market}}, 資料日 ymd)。
    volume 單位：張。market: 'tse'=上市, 'otc'=上櫃。
    資料日取自 API 的 Date 欄（收盤後 API 可能延遲更新，呼叫端須驗證）。"""
    out = {}
    qdate = {"tse": None, "otc": None}

    def _retry_json(url, tries=3, wait=8):
        for i in range(tries):
            data = _get_json(url)
            if data:
                return data
            if i < tries - 1:
                time.sleep(wait)
        return None

    # 上市（證交所）
    data = _retry_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if not data:
        log.warning("上市每日行情（STOCK_DAY_ALL）重試後仍失敗，本次快照將沿用舊資料")
    for row in data or []:
        code = row.get("Code")
        if not code:
            continue
        if qdate["tse"] is None:
            qdate["tse"] = _roc_ymd(row.get("Date"))
        vol = _num(row.get("TradeVolume"))
        out[code] = {
            "name": row.get("Name", ""),
            "close": _num(row.get("ClosingPrice")),
            "change": _num(row.get("Change")),
            "volume_lots": round(vol / 1000, 1) if vol else 0,
            "market": "tse",
        }

    # 上櫃（櫃買中心）
    data = _retry_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes")
    if not data:
        log.warning("上櫃每日行情重試後仍失敗，本次快照將沿用舊資料")
    for row in data or []:
        code = row.get("SecuritiesCompanyCode")
        if not code:
            continue
        if qdate["otc"] is None:
            qdate["otc"] = _roc_ymd(row.get("Date"))
        vol = _num(row.get("TradingShares"))
        out[code] = {
            "name": row.get("CompanyName", ""),
            "close": _num(row.get("Close")),
            "change": _num(row.get("Change")),
            "volume_lots": round(vol / 1000, 1) if vol else 0,
            "market": "otc",
        }
    return out, qdate


def fetch_twse_close_quotes(ymd):
    """上市盤後正式行情（MI_INDEX，可指定日期，收盤後約半小時更新）。
    openapi 延遲時的備援。回傳 {code: {...market:'tse'}} 或 None。"""
    data = _get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                     params={"date": ymd, "type": "ALLBUT0999",
                             "response": "json"})
    if not data or data.get("stat") != "OK":
        return None
    tables = data.get("tables") or []
    if not tables and data.get("fields9"):        # 舊版扁平格式
        tables = [{"fields": data.get("fields9"), "data": data.get("data9")}]
    for t in tables:
        f = t.get("fields") or []
        if "證券代號" not in f or "收盤價" not in f:
            continue
        idx = {k: f.index(k) for k in
               ("證券代號", "證券名稱", "成交股數", "收盤價")}
        i_sign = f.index("漲跌(+/-)") if "漲跌(+/-)" in f else None
        i_diff = f.index("漲跌價差") if "漲跌價差" in f else None
        out = {}
        for row in t.get("data") or []:
            try:
                code = str(row[idx["證券代號"]]).strip()
            except Exception:                                # noqa: BLE001
                continue
            close = _num(row[idx["收盤價"]])
            vol = _num(row[idx["成交股數"]])
            chg = None
            if i_sign is not None and i_diff is not None:
                d = _num(row[i_diff])
                if d is not None:
                    chg = -d if "-" in str(row[i_sign]) else d
            out[code] = {"name": str(row[idx["證券名稱"]]).strip(),
                         "close": close, "change": chg,
                         "volume_lots": round(vol / 1000, 1) if vol else 0,
                         "market": "tse"}
        if out:
            return out
    return None


def fetch_daytrade_shares(ymd):
    """個股當日沖銷成交股數 {code: shares}。上市 TWTB4U（可指定日期）；
    上櫃端點盡力嘗試、失敗不影響上市。用於算當沖率=當沖股數/總成交股數。"""
    out = {}
    data = _get_json("https://www.twse.com.tw/rwd/zh/afterTrading/TWTB4U",
                     params={"date": ymd, "selectType": "All",
                             "response": "json"})
    if data and data.get("stat") == "OK":
        tables = data.get("tables") or [
            {"fields": data.get("fields"), "data": data.get("data")}]
        for t in tables:
            f = t.get("fields") or []
            i_code = next((i for i, x in enumerate(f) if "證券代號" in x), None)
            i_sh = next((i for i, x in enumerate(f)
                         if "沖銷" in x and "成交股數" in x
                         and "買進" not in x and "賣出" not in x), None)
            if i_code is None or i_sh is None:
                continue
            for row in t.get("data") or []:
                try:
                    code = str(row[i_code]).strip()
                    v = _num(row[i_sh])
                except Exception:                            # noqa: BLE001
                    continue
                if code and v:
                    out[code] = v
            if out:
                break
    else:
        log.warning("上市當沖統計（TWTB4U）取得失敗")

    # 上櫃（端點格式历经改版，盡力嘗試）
    roc = f"{int(ymd[:4]) - 1911}/{ymd[4:6]}/{ymd[6:]}"
    for url, params in (
        ("https://www.tpex.org.tw/www/zh-tw/intraday/dayTrading",
         {"date": roc, "type": "Daily", "response": "json"}),
        ("https://www.tpex.org.tw/web/stock/trading/intraday_trading/"
         "intraday_trading_list_print.php",
         {"l": "zh-tw", "d": roc, "o": "json"}),
    ):
        try:
            data = _get_json(url, params=params)
            rows = []
            if isinstance(data, dict):
                rows = (data.get("aaData") or data.get("tables", [{}])[0]
                        .get("data") or [])
            got = 0
            for row in rows:
                # 慣例：0=代號 1=名稱 2=當沖成交股數
                try:
                    code = str(row[0]).strip()
                    v = _num(row[2])
                except Exception:                            # noqa: BLE001
                    continue
                if code and code[0].isdigit() and v:
                    out[code] = v
                    got += 1
            if got:
                break
        except Exception:                                    # noqa: BLE001
            continue
    return out


def fetch_night_futures():
    """前一夜盤台指期（TX 近月）漲跌 %。TAIFEX OpenAPI，失敗回傳 None。"""
    for url in ("https://openapi.taifex.com.tw/v1/AfterHoursDailyMarketReportFut",
                "https://openapi.taifex.com.tw/v1/DailyMarketReportFutAh"):
        data = _get_json(url)
        if not isinstance(data, list) or not data:
            continue
        best = None
        for row in data:
            if str(row.get("Contract", "")).strip() != "TX":
                continue
            month = str(row.get("ContractMonth(Week)")
                        or row.get("ContractMonth") or "").strip()
            if not month or "/" in month:          # 排除價差組合
                continue
            if best is None or month < best[0]:
                best = (month, row)
        if not best:
            continue
        row = best[1]
        # 欄位名稱防禦性解析：優先百分比欄，否則用 漲跌/(收盤-漲跌) 推算
        for key in ("%Change", "ChangePercent", "Change%"):
            v = _num(str(row.get(key, "")).replace("%", ""))
            if v is not None:
                return round(v, 2)
        last = _num(row.get("Last") or row.get("Close"))
        chg = _num(row.get("Change"))
        if last is not None and chg is not None and last - chg:
            return round(chg / (last - chg) * 100, 2)
    return None


def fetch_index_gap():
    """加權指數今日開盤對昨收的跳空 %（夜盤情緒代理）。失敗回傳 None。"""
    data = _get_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII",
        params={"range": "5d", "interval": "1d"})
    try:
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        opens, closes = q["open"], q["close"]
        idx = [i for i, o in enumerate(opens) if o is not None]
        if len(idx) < 2:
            return None
        i = idx[-1]
        prev_close = next((closes[j] for j in reversed(idx[:-1])
                           if closes[j] is not None), None)
        if not prev_close:
            return None
        return round((opens[i] - prev_close) / prev_close * 100, 2)
    except Exception:                                        # noqa: BLE001
        return None


def fetch_inst_totals(ymd=None):
    """全市場三大法人買賣金額（上市，億元）。回傳
    {"foreign_yi": 外資買賣差額, "total_yi": 合計買賣差額}，失敗回傳 None。"""
    d = ymd or date.today().strftime("%Y%m%d")
    data = _get_json("https://www.twse.com.tw/rwd/zh/fund/BFI82U",
                     params={"dayDate": d, "type": "day", "response": "json"})
    if not data or data.get("stat") != "OK":
        return None
    foreign = total = 0.0
    for row in data.get("data", []):
        if len(row) < 4:
            continue
        name, diff = str(row[0]), _num(row[3])
        if diff is None:
            continue
        if "外資" in name:
            foreign += diff
        if "合計" in name:
            total = diff
    return {"foreign_yi": round(foreign / 1e8, 1),
            "total_yi": round(total / 1e8, 1)}


# ---------------------------------------------------------------------------
# 2. 三大法人買賣超 (T86)：外資、投信、自營商，單位股數
# ---------------------------------------------------------------------------

def fetch_t86(ymd):
    """ymd: 'YYYYMMDD'。回傳 {代號: {foreign_net, trust_net, dealer_net,
    total_net, foreign_buy, foreign_sell, trust_buy, trust_sell}}，單位：股。"""
    cache = _cache_load(f"t86_{ymd}.json")
    if cache is not None:
        return cache

    # 網路/限流失敗（回 None）重試；查無資料（stat!=OK，如假日）不重試。
    # 若某天因暫時失敗被誤判為假日，fetch_t86_latest_two 會摸到更舊的日期，
    # 造成舊資料覆蓋新結果（2026-08-11 曾發生），故這裡務必分辨兩種情況。
    data = None
    for attempt in range(3):
        data = _get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                         params={"date": ymd, "selectType": "ALLBUT0999",
                                 "response": "json"})
        if data is not None:
            break
        time.sleep(6)
    if not data or data.get("stat") != "OK":
        return {}

    fields = data.get("fields", [])

    def col(*keywords):
        for i, f in enumerate(fields):
            if all(k in f for k in keywords):
                return i
        return None

    idx = {
        "code": col("證券代號"),
        "f_buy": col("外陸資買進", "不含"),
        "f_sell": col("外陸資賣出", "不含"),
        "f_net": col("外陸資買賣超", "不含"),
        "t_buy": col("投信買進"),
        "t_sell": col("投信賣出"),
        "t_net": col("投信買賣超"),
        "d_net": col("自營商買賣超股數"),
        "total": col("三大法人買賣超"),
    }
    out = {}
    for row in data.get("data", []):
        try:
            code = row[idx["code"]].strip()
        except Exception:                                    # noqa: BLE001
            continue

        def v(key):
            i = idx[key]
            return _num(row[i]) if i is not None and i < len(row) else None

        out[code] = {
            "foreign_buy": v("f_buy") or 0,
            "foreign_sell": v("f_sell") or 0,
            "foreign_net": v("f_net") or 0,
            "trust_buy": v("t_buy") or 0,
            "trust_sell": v("t_sell") or 0,
            "trust_net": v("t_net") or 0,
            "dealer_net": v("d_net") or 0,
            "total_net": v("total") or 0,
        }
    if out:
        _cache_save(f"t86_{ymd}.json", out)
    return out


def fetch_tpex_inst(ymd):
    """上櫃三大法人買賣超（櫃買中心）。回傳格式與 fetch_t86 相同，單位：股。"""
    cache = _cache_load(f"tpex_inst_{ymd}.json")
    if cache is not None:
        return cache

    roc = f"{int(ymd[:4]) - 1911}/{ymd[4:6]}/{ymd[6:]}"
    headers = {"Referer": "https://www.tpex.org.tw/"}
    data = None
    try:
        r = _session().get("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                           params={"type": "Daily", "sect": "EW", "date": roc,
                                   "response": "json"},
                           headers=headers, timeout=CONFIG["request_timeout"])
        r.raise_for_status()
        data = r.json()
    except Exception as e:                                   # noqa: BLE001
        log.warning("TPEx 法人資料(新版API)失敗: %s，改用舊版端點", e)

    fields, rows = [], []
    for t in (data or {}).get("tables", []):
        if t.get("data"):
            fields, rows = t.get("fields", []), t["data"]
            break

    if not rows:
        # 備用：舊版 API（固定欄位順序的 aaData，se=EW 版面共 24 欄）
        try:
            r = _session().get(
                "https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
                "3itrade_hedge_result.php",
                params={"l": "zh-tw", "se": "EW", "t": "D", "d": roc,
                        "o": "json"},
                headers=headers, timeout=CONFIG["request_timeout"])
            r.raise_for_status()
            rows = r.json().get("aaData", [])
            fields = []                       # 舊版無欄位名，用固定索引
        except Exception as e:                               # noqa: BLE001
            log.warning("TPEx 法人資料(舊版API)也失敗: %s", e)
            return {}

    def col(*keywords):
        for i, f in enumerate(fields):
            if all(k in f for k in keywords):
                return i
        return None

    if fields:
        idx = {
            "code": col("代號"),
            "f_buy": col("不含", "買進"),
            "f_sell": col("不含", "賣出"),
            "f_net": col("不含", "買賣超"),
            "t_buy": col("投信", "買進"),
            "t_sell": col("投信", "賣出"),
            "t_net": col("投信", "買賣超"),
            "total": col("三大法人", "買賣超"),
        }
    else:
        # 舊版固定欄位：0代號 1名稱 2-4外資(不含自營) 5-7外資自營
        # 8-10外資合計 11-13投信 14-22自營商 23三大法人合計
        idx = {"code": 0, "f_buy": 2, "f_sell": 3, "f_net": 4,
               "t_buy": 11, "t_sell": 12, "t_net": 13, "total": 23}
        rows = [r for r in rows if len(r) >= 24]
    if idx["code"] is None or idx["total"] is None:
        log.warning("TPEx 法人資料欄位解析失敗: %s", fields)
        return {}

    out = {}
    for row in rows:
        try:
            code = str(row[idx["code"]]).strip()
        except Exception:                                    # noqa: BLE001
            continue
        if not code or not code[0].isdigit():
            continue

        def v(key):
            i = idx[key]
            return _num(row[i]) if i is not None and i < len(row) else None

        foreign, trust = v("f_net") or 0, v("t_net") or 0
        total = v("total") or 0
        out[code] = {
            "foreign_buy": v("f_buy") or 0,
            "foreign_sell": v("f_sell") or 0,
            "foreign_net": foreign,
            "trust_buy": v("t_buy") or 0,
            "trust_sell": v("t_sell") or 0,
            "trust_net": trust,
            "dealer_net": total - foreign - trust,
            "total_net": total,
        }
    if out:
        _cache_save(f"tpex_inst_{ymd}.json", out)
    return out


def fetch_t86_latest_two(today=None):
    """抓最近兩個有法人資料的交易日（上市+上櫃合併），
    回傳 (今日dict, 前一日dict, 今日日期, 前日日期)。以上市 T86 判斷交易日。"""
    d = today or date.today()
    found = []
    probe = d
    for _ in range(10):                       # 往回最多找 10 天，跳過假日
        ymd = probe.strftime("%Y%m%d")
        data = fetch_t86(ymd)
        if data:
            merged = dict(data)
            merged.update(fetch_tpex_inst(ymd))   # 上櫃併入（代號不重疊）
            found.append((ymd, merged))
            if len(found) == 2:
                break
        probe -= timedelta(days=1)
        time.sleep(1)
    if len(found) < 2:
        return {}, {}, None, None
    return found[0][1], found[1][1], found[0][0], found[1][0]


# ---------------------------------------------------------------------------
# 3. 集保戶股權分散表（週資料）：第 15 級 = 持股 1,000,001 股以上（>1000 張大戶）
# ---------------------------------------------------------------------------

def fetch_tdcc_dispersion():
    """回傳 {代號: {date, big_holder_pct, big_holder_shares, big_holder_people}}。
    同時把本週快照存入歷史檔，供計算「大戶持股週變動」。"""
    cache = _cache_load("tdcc_latest.json", max_age_hours=12)
    if cache is not None:
        return cache

    try:
        r = _session().get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
                           timeout=120)
        r.raise_for_status()
        r.encoding = "utf-8"
        rows = list(csv.reader(io.StringIO(r.text)))
    except Exception as e:                                   # noqa: BLE001
        log.warning("TDCC 股權分散表抓取失敗: %s", e)
        return _cache_load("tdcc_latest.json") or {}

    out = {}
    for row in rows[1:]:
        # 欄位: 資料日期, 證券代號, 持股分級, 人數, 股數, 占集保庫存數比例%
        if len(row) < 6:
            continue
        ymd, code, level = row[0].strip(), row[1].strip(), row[2].strip()
        if level != "15":                     # 15 = 1,000,001 股以上
            continue
        out[code] = {
            "date": ymd,
            "big_holder_people": _num(row[3]) or 0,
            "big_holder_shares": _num(row[4]) or 0,
            "big_holder_pct": _num(row[5]) or 0,
        }

    if out:
        _cache_save("tdcc_latest.json", out)
        _update_tdcc_history(out)
    return out


def _update_tdcc_history(latest):
    """保留每週第 15 級快照（最多 8 週），供計算週變動。"""
    hist = _cache_load("tdcc_history.json") or {}
    any_date = next(iter(latest.values()))["date"]
    hist[any_date] = {c: v["big_holder_pct"] for c, v in latest.items()}
    for k in sorted(hist)[:-8]:
        del hist[k]
    _cache_save("tdcc_history.json", hist)


def tdcc_weekly_change(code):
    """回傳 (本週大戶持股%, 與上一週相比的變動百分點) — 需累積兩週以上資料。"""
    hist = _cache_load("tdcc_history.json") or {}
    dates = sorted(hist)
    if not dates:
        return None, None
    cur = hist[dates[-1]].get(code)
    prev = hist[dates[-2]].get(code) if len(dates) >= 2 else None
    if cur is None:
        return None, None
    return cur, (round(cur - prev, 2) if prev is not None else None)


# ---------------------------------------------------------------------------
# 4. 內部人持股轉讓申報（董監事、經理人、大股東）
# ---------------------------------------------------------------------------

def _norm_transfer_row(row):
    """把一筆持股轉讓申報（dict）正規化。依欄位名稱模糊比對，
    避免官方欄位名稱微調造成解析失敗。"""
    def field(*keywords):
        for k, v in row.items():
            if k and all(w in k for w in keywords):
                return v
        return None

    code = field("公司代號") or field("證券代號")
    if not code:
        return None
    return {
        "code": str(code).strip(),
        "name": (field("公司名稱") or "").strip(),
        "date": (field("申報日期") or field("日期") or "").strip(),
        "holder": (field("身分") or field("申報人") or "").strip(),
        "method": (field("轉讓方式") or field("預定轉讓方式") or "").strip(),
        "shares": _num(field("轉讓股數") or field("預定轉讓股數")) or 0,
    }


def fetch_share_transfers():
    """上市＋上櫃公司持股轉讓日報表。回傳 list of
    {code, name, date, method, shares}，method 例如「一般交易」「信託」「贈與」。"""
    cache = _cache_load("transfers.json", max_age_hours=12)
    if cache is not None:
        return cache

    out = []

    # 上市（證交所 OpenAPI）
    data = _get_json("https://openapi.twse.com.tw/v1/opendata/t187ap12_L")
    for row in data or []:
        t = _norm_transfer_row(row)
        if t:
            out.append(t)

    # 上櫃（MOPS 開放資料 CSV）
    try:
        r = _session().get("https://mopsfin.twse.com.tw/opendata/t187ap12_O.csv",
                           timeout=60)
        r.raise_for_status()
        r.encoding = "utf-8-sig"
        for row in csv.DictReader(io.StringIO(r.text)):
            t = _norm_transfer_row(row)
            if t:
                out.append(t)
    except Exception as e:                                   # noqa: BLE001
        log.warning("上櫃持股轉讓資料抓取失敗: %s", e)

    if out:
        _cache_save("transfers.json", out)
    return out


def _roc_to_date(s):
    """民國日期 '1130805' 或 '113/08/05' → date。西元 '20240805' 也支援。"""
    s = str(s).replace("/", "").replace("-", "").strip()
    if not s.isdigit():
        return None
    try:
        if len(s) == 7:                       # 民國 YYYMMDD
            return date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
        if len(s) == 8:                       # 西元 YYYYMMDD
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return None


def transfer_general_lots_all(lookback_days=None):
    """全部個股近 N 日「一般交易」轉讓申報合計張數 {code: lots}。
    供前端自選/漲停卡計算條件③（清單內沒有的代號=無申報=0 張）。"""
    lookback = lookback_days or CONFIG["transfer_lookback_days"]
    cutoff = date.today() - timedelta(days=lookback)
    out = {}
    for t in fetch_share_transfers():
        if "一般交易" not in t["method"]:
            continue
        d = _roc_to_date(t["date"])
        if d and d < cutoff:
            continue
        out[t["code"]] = out.get(t["code"], 0) + t["shares"]
    return {c: round(v / 1000, 1) for c, v in out.items()}


def transfer_general_lots(code, lookback_days=None):
    """近 N 日內該股「一般交易」方式的轉讓申報合計張數。"""
    lookback = lookback_days or CONFIG["transfer_lookback_days"]
    cutoff = date.today() - timedelta(days=lookback)
    total_shares = 0
    for t in fetch_share_transfers():
        if t["code"] != code or "一般交易" not in t["method"]:
            continue
        d = _roc_to_date(t["date"])
        if d and d < cutoff:
            continue
        total_shares += t["shares"]
    return total_shares / 1000                # 張


# ---------------------------------------------------------------------------
# 5. MOPS 個別公司財報：合約負債、股本（資產負債表）、EPS（綜合損益表）
# ---------------------------------------------------------------------------

MOPS_URL = "https://mopsov.twse.com.tw/mops/web/{page}"


def _mops_post(page, co_id, year, season):
    """POST 公開資訊觀測站個別財報頁，回傳 HTML 文字。year 為民國年。"""
    try:
        r = _session().post(
            MOPS_URL.format(page=page),
            data={"encodeURIComponent": "1", "step": "1", "firstin": "1",
                  "off": "1", "queryName": "co_id", "inpuType": "co_id",
                  "TYPEK": "all", "isnew": "false", "co_id": co_id,
                  "year": str(year), "season": f"{season:02d}"},
            timeout=CONFIG["request_timeout"])
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception as e:                                   # noqa: BLE001
        log.warning("MOPS %s %s 失敗: %s", page, co_id, e)
        return ""


def _html_row_value(html, label):
    """從 MOPS 報表 HTML 中找「會計項目 label」該列的第一個數字。
    支援小數（EPS 等）與會計格式負數 (1,234)。"""
    text = re.sub(r"<[^>]+>", "|", html)
    text = re.sub(r"[ \t\r\n　]+", "", text)
    m = re.search(re.escape(label) + r"\|+(\(?-?[\d,]+(?:\.\d+)?\)?)", text)
    if not m:
        return None
    s = m.group(1)
    neg = s.startswith("(") and s.endswith(")")
    v = _num(s.strip("()"))
    return -v if neg and v is not None else v


def _recent_seasons(n=4):
    """由今天往回推 n 個可能已公布的季度，回傳 [(民國年, 季)]。"""
    today = date.today()
    y, q = today.year, (today.month - 1) // 3   # 上一季
    out = []
    for _ in range(n):
        if q == 0:
            y, q = y - 1, 4
        out.append((y - 1911, q))
        q -= 1
    return out


def fetch_financials(co_id):
    """回傳 {contract_liab_k, capital_k, eps, period}（金額單位：仟元）。
    只對通過前段篩選的少數個股呼叫，並加上延遲避免 MOPS 封鎖。"""
    cache = _cache_load(f"fin_{co_id}.json", max_age_hours=24 * 7)
    if cache is not None:
        return cache

    result = {"contract_liab_k": None, "capital_k": None,
              "eps": None, "period": None}
    for year, season in _recent_seasons():
        time.sleep(CONFIG["mops_delay_sec"])
        bs = _mops_post("ajax_t164sb03", co_id, year, season)   # 資產負債表
        if "查無" in bs or not bs:
            continue
        result["contract_liab_k"] = (
            _html_row_value(bs, "合約負債－流動")
            or _html_row_value(bs, "合約負債—流動")
            or _html_row_value(bs, "合約負債"))
        result["capital_k"] = (_html_row_value(bs, "股本合計")
                               or _html_row_value(bs, "普通股股本"))

        time.sleep(CONFIG["mops_delay_sec"])
        pl = _mops_post("ajax_t164sb04", co_id, year, season)   # 綜合損益表
        result["eps"] = (_html_row_value(pl, "基本每股盈餘")
                         or _html_row_value(pl, "基本每股盈餘（元）"))
        result["period"] = f"{year + 1911}Q{season}"
        break

    if result["period"]:
        _cache_save(f"fin_{co_id}.json", result)
    return result


# ---------------------------------------------------------------------------
# 6. 主力買賣超（券商分點）— 官方無彙總開放資料，以富邦證券分點頁為輔助來源，
#    失敗時以「三大法人合計買賣超」作為替代指標。
# ---------------------------------------------------------------------------

def fetch_main_force_net(code):
    """回傳 (主力買賣超張數 or None, 來源說明)。"""
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{code}.djhtm"
    try:
        r = _session().get(url, timeout=CONFIG["request_timeout"])
        r.raise_for_status()
        m = re.search(r"買賣超第[一1]名.*?(-?[\d,]+)", r.text) or \
            re.search(r"主力買賣超[^\d\-]*(-?[\d,]+)", r.text)
        if m:
            return _num(m.group(1)), "券商分點(富邦)"
    except Exception as e:                                   # noqa: BLE001
        log.debug("主力分點 %s 抓取失敗: %s", code, e)
    return None, "無資料(以法人合計替代)"


# ---------------------------------------------------------------------------
# 7. 盤中即時行情（累積成交量）
# ---------------------------------------------------------------------------

def fetch_intraday_volumes(codes, market_map=None):
    """回傳 {代號: {volume_lots, price, time}}，volume 為當日累積成交張數。
    market_map: {代號: 'tse'|'otc'}；未知市場的代號會同時以兩種前綴查詢，
    行情站只回應存在的那個。"""
    if not codes:
        return {}
    market_map = market_map or {}
    queries = []
    for c in codes:
        m = market_map.get(c)
        for prefix in ([m] if m in ("tse", "otc") else ["tse", "otc"]):
            queries.append(f"{prefix}_{c}.tw")

    out = {}
    s = _session()
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=15)
    except Exception:                                        # noqa: BLE001
        pass
    # 每次最多查 20 檔
    for i in range(0, len(queries), 20):
        ex_ch = "|".join(queries[i:i + 20])
        try:
            r = s.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                      params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                      timeout=CONFIG["request_timeout"])
            r.raise_for_status()
            for item in r.json().get("msgArray", []):
                code = item.get("c")
                if not code:
                    continue
                out[code] = {
                    "volume_lots": _num(item.get("v")) or 0,
                    "price": _num(item.get("z")) or _num(item.get("y")),
                    "time": item.get("t", ""),
                }
        except Exception as e:                               # noqa: BLE001
            log.warning("即時行情抓取失敗: %s", e)
        time.sleep(1)
    return out


def fetch_intraday_quotes_full(codes, market_map=None):
    """回傳 {代號: {price, prev_close, change_pct, volume_lots,
    ask_lots, bid_lots, name, time}}。ask/bid_lots 為揭示五檔委賣/委買總量（張），
    供委買賣失衡（流動性急凍）判斷；盤前或收盤後掛單欄位可能為空 → None。"""
    if not codes:
        return {}
    market_map = market_map or {}
    queries = []
    for c in codes:
        m = market_map.get(c)
        for prefix in ([m] if m in ("tse", "otc") else ["tse", "otc"]):
            queries.append(f"{prefix}_{c}.tw")

    def _five_sum(s):
        vals = [_num(x) for x in str(s or "").split("_") if x.strip()]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    out = {}
    s = _session()
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=15)
    except Exception:                                        # noqa: BLE001
        pass
    for i in range(0, len(queries), 20):
        ex_ch = "|".join(queries[i:i + 20])
        try:
            r = s.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                      params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                      timeout=CONFIG["request_timeout"])
            r.raise_for_status()
            for item in r.json().get("msgArray", []):
                code = item.get("c")
                if not code:
                    continue
                price = _num(item.get("z")) or _num(item.get("y"))
                prev = _num(item.get("y"))
                out[code] = {
                    "price": price,
                    "prev_close": prev,
                    "open": _num(item.get("o")),
                    "change_pct": (round((price - prev) / prev * 100, 2)
                                   if price and prev else None),
                    "volume_lots": _num(item.get("v")) or 0,
                    "ask_lots": _five_sum(item.get("f")),
                    "bid_lots": _five_sum(item.get("g")),
                    "name": item.get("n", ""),
                    "time": item.get("t", ""),
                }
        except Exception as e:                               # noqa: BLE001
            log.warning("即時完整行情抓取失敗: %s", e)
        time.sleep(1)
    return out


def load_holdings():
    """讀取持股清單檔（一行一代號，# 註解），供風險燈號與量能監控使用。
    檔案不存在回傳空 list。"""
    path = CONFIG.get("holdings_file", "watchlist.txt")
    codes = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                code = line.split("#")[0].strip()
                if code and code not in codes:
                    codes.append(code)
    except FileNotFoundError:
        pass
    return codes


# ---------------------------------------------------------------------------
# 國際市場風險指標（Yahoo Finance 公開 chart API）
# ---------------------------------------------------------------------------

def _yahoo_quote(symbol):
    """回傳 {price, prev_close, change_pct}，失敗回傳 None。"""
    data = _get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "1d", "interval": "15m"})
    try:
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if price is None or not prev:
            return None
        return {"price": price, "prev_close": prev,
                "change_pct": round((price - prev) / prev * 100, 2)}
    except Exception:                                        # noqa: BLE001
        return None


def fetch_market_indicators():
    """抓熔斷前市場訊號用的指標。台指期夜盤無免費即時源，
    以 EWT（美股掛牌台灣ETF，反映夜間台股情緒）與 ES=F 作代理。
    回傳 {key: {price, prev_close, change_pct}}，抓不到的 key 不出現。"""
    symbols = {
        "vix": "%5EVIX",       # 恐慌指數
        "es": "ES%3DF",        # S&P 500 期貨（近24小時交易）
        "twii": "%5ETWII",     # 加權指數（盤中現貨急殺）
        "ewt": "EWT",          # 台灣ETF（夜間情緒代理）
        "n225": "%5EN225",     # 日經
        "kospi": "%5EKS11",    # 韓股
    }
    out = {}
    for key, sym in symbols.items():
        q = _yahoo_quote(sym)
        if q:
            out[key] = q
        else:
            log.warning("市場指標 %s(%s) 抓取失敗", key, sym)
        time.sleep(0.5)
    return out
