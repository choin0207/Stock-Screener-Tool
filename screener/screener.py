# -*- coding: utf-8 -*-
"""每日選股邏輯：五個條件全部通過才列入名單。

  1. 外資與投信與前一日相比大量購入（皆為買超，且合計買超 >= 前日 * inst_surge_ratio）
  2. 三大法人買賣超 > 前一日買賣超 * net_buy_ratio（預設 3 倍，前日需為買超）
  3. 近 N 日「一般交易」內部人持股轉讓合計 <= transfer_max_lots（預設 100 張）
  4. 合約負債 > 實收資本額（股本）× contract_liability_capital_ratio（預設 0.5）
  5. 獲利能力：基本每股盈餘 EPS > profitability_threshold（預設 1.5 元）

條件 1、2 用大盤整批資料先過濾，只對存活的少數個股查 MOPS 個別財報（條件 4、5），
把對公開資訊觀測站的請求量降到最低。
"""

import json
import logging
import os
from datetime import datetime

from .config import CONFIG
from . import datasources as ds
from . import performance
from . import backtest

log = logging.getLogger("screener.core")

RESULT_FILE = None


def _result_path():
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], "screen_results.json")


def load_results():
    try:
        with open(_result_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {"generated_at": None, "trade_date": None, "results": [],
                "message": "尚未執行篩選"}


def save_results(obj):
    with open(_result_path(), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def run_daily_screen():
    """執行每日篩選，回傳結果 dict 並寫入 data/screen_results.json。"""
    log.info("開始每日篩選 ...")
    t86_today, t86_prev, d_today, d_prev = ds.fetch_t86_latest_two()
    if not t86_today or not t86_prev:
        msg = "無法取得三大法人資料（T86），請確認網路或稍後再試"
        log.error(msg)
        out = {"generated_at": performance._now().isoformat(timespec="seconds"),
               "trade_date": None, "results": [], "message": msg}
        save_results(out)
        return out

    # 防止資料來源異常時「舊交易日覆蓋新結果」（T86 間歇失敗會往回摸到舊日期）
    existing = load_results()
    if existing.get("trade_date") and d_today < existing["trade_date"]:
        msg = (f"T86 資料異常：本次僅取得 {d_today}（舊於現有結果 "
               f"{existing['trade_date']}），不覆寫，請稍後重跑")
        log.error(msg)
        return {"generated_at": performance._now().isoformat(timespec="seconds"),
                "trade_date": None, "results": [], "message": msg}

    quotes = _safe_quotes(d_today)
    ds.fetch_tdcc_dispersion()          # 更新大戶資料快取與週歷史
    vh_map = _vol_hist_map(d_today)     # 近4日量能歷史（由前一份快照滾動）

    # 市場順風指標：夜盤台指（或開盤跳空代理）＋外資全市場買賣超金額
    market_ctx = _market_context(d_today)

    surge_ratio = CONFIG["inst_surge_ratio"]
    net_ratio = CONFIG["net_buy_ratio"]

    # ---- 條件 1 + 2：整批過濾 ----
    candidates = []
    for code, cur in t86_today.items():
        prev = t86_prev.get(code)
        if not prev:
            continue

        # 條件1：外資、投信今日皆買超，且合計買超放大
        inst_today = cur["foreign_net"] + cur["trust_net"]
        inst_prev = prev["foreign_net"] + prev["trust_net"]
        cond1 = (cur["foreign_net"] > 0 and cur["trust_net"] > 0 and
                 inst_today > max(inst_prev, 0) * surge_ratio and inst_today > 0)

        # 條件2：三大法人買賣超 > 前日 net_buy_ratio 倍（前日需為買超）
        cond2 = (prev["total_net"] > 0 and
                 cur["total_net"] > prev["total_net"] * net_ratio)

        if cond1 and cond2:
            candidates.append(code)

    log.info("條件1+2 通過：%d 檔 %s", len(candidates), candidates)

    # ---- 條件 3/4/5：逐檔評估，所有候選股都列出並依符合程度分級 ----
    # 排除 ETF／受益憑證等非個股（台股個股為 4 碼數字且不以 00 開頭；
    # ETF 如 006205 為 5-6 碼、0050/0056 為 00 開頭 4 碼）
    candidates = [c for c in candidates
                  if len(c) == 4 and c.isdigit() and not c.startswith("00")]
    # 依法人買超由大到小排序；設查詢上限保護 MOPS 不被大量請求
    candidates.sort(key=lambda c: -t86_today[c]["total_net"])
    max_q = CONFIG.get("max_financial_queries", 40)
    if len(candidates) > max_q:
        log.warning("候選 %d 檔超過財報查詢上限 %d，僅評估法人買超前 %d 檔",
                    len(candidates), max_q, max_q)
        candidates = candidates[:max_q]

    bulk_fin = load_financials()

    def build_entry(code, cur, prev, fin, fetch_main_force=True):
        """組出一檔股票的完整結果（含五條件旗標）。cur/prev 可為 None。"""
        z = {"foreign_buy": 0, "foreign_sell": 0, "foreign_net": 0,
             "trust_buy": 0, "trust_sell": 0, "trust_net": 0,
             "dealer_net": 0, "total_net": 0}
        cur, prev = cur or z, prev or z
        q = quotes.get(code, {})

        inst_today = cur["foreign_net"] + cur["trust_net"]
        inst_prev = prev["foreign_net"] + prev["trust_net"]
        c1 = (cur["foreign_net"] > 0 and cur["trust_net"] > 0 and
              inst_today > 0 and inst_today > max(inst_prev, 0) * surge_ratio)
        c2 = (prev["total_net"] > 0 and
              cur["total_net"] > prev["total_net"] * net_ratio)

        lots = ds.transfer_general_lots(code)
        c3 = lots <= CONFIG["transfer_max_lots"]

        contract = fin.get("contract_liab_k")
        capital = fin.get("capital_k")
        eps = fin.get("eps")
        c4 = (contract is not None and capital is not None and
              contract > capital * CONFIG["contract_liability_capital_ratio"])
        c5 = (eps is not None and eps > CONFIG["profitability_threshold"])

        conds = {"c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5}
        big_pct, big_chg = ds.tdcc_weekly_change(code)
        if fetch_main_force:
            mf_net, mf_src = ds.fetch_main_force_net(code)
        else:
            mf_net, mf_src = None, "以法人合計替代"

        return {
            "code": code,
            "name": q.get("name", ""),
            "market": q.get("market", "tse"),
            "conds": conds,
            "passed": sum(conds.values()),
            "close": q.get("close"),
            "change": q.get("change"),
            "volume_lots": q.get("volume_lots"),
            "vol_hist": vh_map.get(code, []),
            # 三大法人（張）
            "foreign_buy": round(cur["foreign_buy"] / 1000, 1),
            "foreign_sell": round(cur["foreign_sell"] / 1000, 1),
            "foreign_net": round(cur["foreign_net"] / 1000, 1),
            "foreign_net_prev": round(prev["foreign_net"] / 1000, 1),
            "trust_buy": round(cur["trust_buy"] / 1000, 1),
            "trust_sell": round(cur["trust_sell"] / 1000, 1),
            "trust_net": round(cur["trust_net"] / 1000, 1),
            "trust_net_prev": round(prev["trust_net"] / 1000, 1),
            "total_net": round(cur["total_net"] / 1000, 1),
            "total_net_prev": round(prev["total_net"] / 1000, 1),
            # 主力
            "main_force_net": (mf_net if mf_net is not None
                               else round(cur["total_net"] / 1000, 1)),
            "main_force_src": mf_src,
            # 大戶（>1000張）
            "big_holder_pct": big_pct,
            "big_holder_chg": big_chg,
            # 持股轉讓
            "transfer_general_lots": round(lots, 1),
            # 財報
            "contract_liab_k": contract,
            "capital_k": capital,
            "eps": eps,
            "fin_period": fin.get("period"),
        }

    # 法人動能候選股（通過①②門檻）
    results = []
    for code in candidates:
        fin = bulk_fin.get(code) or {}
        if not fin.get("period"):
            fin = ds.fetch_financials(code)
        results.append(build_entry(code, t86_today[code], t86_prev.get(code),
                                   fin, fetch_main_force=True))

    # 全市場合約負債達標股（④✓ 但法人未進場）——需先跑過 Financial Scan
    listed = {r["code"] for r in results}
    ratio = CONFIG["contract_liability_capital_ratio"]
    for code, fin in bulk_fin.items():
        if code in listed or len(code) != 4 or not code.isdigit() \
                or code.startswith("00"):
            continue
        contract, capital = fin.get("contract_liab_k"), fin.get("capital_k")
        if contract is None or capital is None or contract <= capital * ratio:
            continue
        if code not in quotes and code not in t86_today:
            continue                          # 已下市或無交易資料
        results.append(build_entry(code, t86_today.get(code),
                                   t86_prev.get(code), fin,
                                   fetch_main_force=False))

    # 回測權重評分（backtest.json 存在時），同分級內以評分排序
    weights = backtest.load_weights()
    for r in results:
        r["score"] = backtest.live_score(r, weights) if weights else None

    def tier(r):
        gate = r["conds"]["c1"] and r["conds"]["c2"]
        if r["passed"] == 5:
            return 0
        if gate and r["conds"]["c4"]:
            return 1
        if r["conds"]["c4"]:
            return 2
        return 3

    results.sort(key=lambda r: (tier(r), -r["passed"],
                                -(r["score"] or 0),
                                -(r["total_net"] or 0)))

    n = [sum(1 for r in results if tier(r) == t) for t in range(4)]
    msg = (f"符合全部條件 {n[0]} 檔；法人動能+合約負債達標 {n[1]} 檔；"
           f"全市場合約負債達標 {n[2]} 檔；其他法人動能股 {n[3]} 檔")
    if not bulk_fin:
        msg += "（全市場財報資料庫尚未建立，請先執行 Financial Scan）"
    out = {
        "generated_at": performance._now().isoformat(timespec="seconds"),
        "trade_date": d_today,
        "prev_trade_date": d_prev,
        "market": market_ctx,
        "results": results,
        "message": msg,
    }
    save_results(out)
    _save_market_snapshot(quotes, t86_today, t86_prev, d_today, d_prev, vh_map)
    try:                      # 內部人轉讓彙總（供前端自選/漲停卡算條件③）
        with open(os.path.join(CONFIG["data_dir"], "transfers.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"trade_date": d_today,
                       "lookback_days": CONFIG["transfer_lookback_days"],
                       "lots": ds.transfer_general_lots_all()},
                      f, ensure_ascii=False)
    except Exception:                                        # noqa: BLE001
        log.exception("轉讓彙總輸出失敗（不影響篩選結果）")
    try:                      # 篩選成效追蹤（🏆/🟡 入選股一個月股價驗證＋跌前警告）
        performance.update(results, quotes, d_today, t86=t86_today,
                           financials=bulk_fin,
                           transfer_fn=ds.transfer_general_lots)
    except Exception:                                        # noqa: BLE001
        log.exception("績效追蹤更新失敗（不影響篩選結果）")
    log.info(out["message"])
    return out


def _stale_quotes_from_prev(require_trade_date=None):
    """把前一份快照轉成 stale 標記的行情（僅名稱/參考價，績效不入帳）。
    require_trade_date 給定時，快照交易日不符則回傳 None。"""
    path = os.path.join(CONFIG["data_dir"], "market_snapshot.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:                                        # noqa: BLE001
        return None
    if require_trade_date and prev.get("trade_date") != require_trade_date:
        return None
    return {c: {"name": s.get("n", ""), "market": s.get("m", "tse"),
                "close": s.get("c"), "change": s.get("ch"),
                "volume_lots": s.get("v"), "stale": True}
            for c, s in prev.get("stocks", {}).items() if s.get("n")}


def _safe_quotes(d_today):
    """取行情並雙重防污染：
    1. 台北盤中（平日 08:30–14:35）執行時 API 回即時值 → 沿用前一份快照
       （2026-08-13 曾把盤中量誤存成收盤）
    2. 收盤後 API 延遲更新、資料日 ≠ 交易日 → 不採用，沿用前快照為參考價
       （2026-08-18 曾把 8/17 收盤誤標成 8/18；19:07 保險重跑會補正）"""
    now = performance._now()
    hm = now.strftime("%H:%M")
    if now.weekday() < 5 and "08:30" <= hm <= "14:35":
        quotes = _stale_quotes_from_prev(require_trade_date=d_today)
        if quotes:
            log.warning("盤中執行：沿用前一份快照收盤資料 %d 檔，"
                        "避免即時值誤存為收盤", len(quotes))
            return quotes
    fresh, qd = ds.fetch_daily_quotes()
    qd = qd or {}
    bad_tse = bool(qd.get("tse") and qd["tse"] != d_today)
    bad_otc = bool(qd.get("otc") and qd["otc"] != d_today)
    if fresh and bad_tse:
        # openapi 延遲 → 改用盤後正式行情表 MI_INDEX（可指定日期）
        mi = ds.fetch_twse_close_quotes(d_today)
        if mi:
            log.warning("openapi 上市資料日 %s ≠ %s，改用 MI_INDEX 盤後表 %d 檔",
                        qd.get("tse"), d_today, len(mi))
            fresh = {c: v for c, v in fresh.items() if v["market"] != "tse"}
            fresh.update(mi)
            bad_tse = False
    if fresh and (bad_tse or bad_otc):
        log.warning("行情資料日不符（tse=%s otc=%s ≠ %s）且無備援，"
                    "該市場價格標記 stale 待保險重跑補正",
                    qd.get("tse"), qd.get("otc"), d_today)
        for v in fresh.values():
            if (v["market"] == "tse" and bad_tse) or \
                    (v["market"] == "otc" and bad_otc):
                v["stale"] = True
    return _quotes_with_fallback(fresh)


def _quotes_with_fallback(quotes):
    """行情來源整批失敗的保底：缺行情的個股用上一份快照補
    名稱/市場/收盤（標記 stale，績效追蹤不會拿舊價當今日收盤）。"""
    path = os.path.join(CONFIG["data_dir"], "market_snapshot.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f).get("stocks", {})
    except Exception:                                        # noqa: BLE001
        return quotes
    added = 0
    for code, s in prev.items():
        if code in quotes or not s.get("n"):
            continue
        quotes[code] = {"name": s["n"], "market": s.get("m", "tse"),
                        "close": s.get("c"), "change": s.get("ch"),
                        "volume_lots": s.get("v"), "stale": True}
        added += 1
    if added:
        log.warning("行情缺 %d 檔，以上一份快照補名稱與參考價（stale）", added)
    return quotes


def _market_context(trade_ymd=None):
    """日級市場指標：夜盤台指漲跌（抓不到改用開盤跳空代理）、
    外資/三大法人全市場買賣超金額（億，用交易日查詢）。
    tailwind=夜盤大漲且外資買超。"""
    ctx = {"night_pct": None, "night_src": None,
           "foreign_yi": None, "total_yi": None, "tailwind": False}
    try:
        pct = ds.fetch_night_futures()
        if pct is not None:
            ctx.update({"night_pct": pct, "night_src": "台指期夜盤"})
        else:
            gap = ds.fetch_index_gap()
            if gap is not None:
                ctx.update({"night_pct": gap,
                            "night_src": "加權指數開盤跳空（夜盤代理）"})
    except Exception:                                        # noqa: BLE001
        log.exception("夜盤指標取得失敗")
    try:
        totals = ds.fetch_inst_totals(trade_ymd)
        if totals:
            ctx.update(totals)
    except Exception:                                        # noqa: BLE001
        log.exception("法人買賣金額取得失敗")
    ctx["tailwind"] = bool(
        ctx["night_pct"] is not None and
        ctx["night_pct"] >= CONFIG["night_surge_pct"] and
        (ctx["foreign_yi"] or 0) > 0)
    return ctx


def _vol_hist_map(d_today):
    """由前一份快照滾動出前 5 個交易日成交量 {code: [[ymd, vol], ...]近→遠}。
    帶日期並以同日期後寫覆蓋（收盤重跑的最終值為準），同日重跑不位移。"""
    path = os.path.join(CONFIG["data_dir"], "market_snapshot.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:                                        # noqa: BLE001
        return {}
    prev_d = prev.get("trade_date")
    out = {}
    for code, s in prev.get("stocks", {}).items():
        # 僅保留帶日期的新格式；舊純數字格式無法補日期，捨棄重建
        vh = [e for e in (s.get("vh") or [])
              if isinstance(e, list) and len(e) == 2
              and e[0] and e[1] is not None]
        if prev_d and prev_d != d_today and s.get("v") is not None:
            vh = [[prev_d, s["v"]]] + [e for e in vh if e[0] != prev_d]
        out[code] = vh[:5]
    return out


def _save_market_snapshot(quotes, t86_today, t86_prev, d_today, d_prev,
                          vh_map=None):
    """全市場每日快照（供網頁「自選名單」查任意個股）。欄位縮寫壓縮檔案大小：
    n=名稱 m=市場 c=收盤 ch=漲跌 v=量(張) f/f0=外資買賣超今/昨(張)
    t/t0=投信 x/x0=法人合計。"""
    path = os.path.join(CONFIG["data_dir"], "market_snapshot.json")
    # 行情來源偶發整批失敗（如假日維護）：沿用上一份快照的名稱/價格保底，
    # 避免出現「有法人數據但無名稱股價」的空白快照
    prev_snap = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev_snap = json.load(f).get("stocks", {})
    except Exception:                                        # noqa: BLE001
        pass

    snap = {}
    for code in set(quotes) | set(t86_today):
        q = quotes.get(code, {})
        cur = t86_today.get(code) or {}
        prev = t86_prev.get(code) or {}

        def k(d, key):
            return round((d.get(key) or 0) / 1000, 1)

        snap[code] = {
            "n": q.get("name", ""), "m": q.get("market", "tse"),
            "c": q.get("close"), "ch": q.get("change"),
            "v": q.get("volume_lots"),
            "f": k(cur, "foreign_net"), "f0": k(prev, "foreign_net"),
            "t": k(cur, "trust_net"), "t0": k(prev, "trust_net"),
            "x": k(cur, "total_net"), "x0": k(prev, "total_net"),
            "vh": (vh_map or {}).get(code, []),
        }
        old = prev_snap.get(code)
        if (not q or q.get("stale")) and old and old.get("n"):
            for key in ("n", "m", "c", "ch", "v"):
                snap[code][key] = old.get(key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"trade_date": d_today, "prev_trade_date": d_prev,
                   "stocks": snap}, f, ensure_ascii=False)


def load_financials():
    """全市場財報資料庫（run_finscan.py 產生）。{code: {contract_liab_k, ...}}"""
    try:
        with open(os.path.join(CONFIG["data_dir"], "financials.json"),
                  encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {}


def watchlist():
    """盤中監控清單 = 法人動能且合約負債達標(🏆/🟡組)的股票 + 額外指定。"""
    codes = [r["code"] for r in load_results().get("results", [])
             if r.get("conds", {}).get("c4", True)
             and r.get("conds", {}).get("c1", True)
             and r.get("conds", {}).get("c2", True)]
    for c in CONFIG["monitor_extra_symbols"] + ds.load_holdings():
        if c not in codes:
            codes.append(c)
    return codes
