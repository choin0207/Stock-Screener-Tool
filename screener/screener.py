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
        out = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "trade_date": None, "results": [], "message": msg}
        save_results(out)
        return out

    quotes = ds.fetch_daily_quotes()
    ds.fetch_tdcc_dispersion()          # 更新大戶資料快取與週歷史

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

    # ---- 條件 3：一般交易持股轉讓 ----
    survivors = []
    transfer_lots = {}
    for code in candidates:
        lots = ds.transfer_general_lots(code)
        transfer_lots[code] = lots
        if lots <= CONFIG["transfer_max_lots"]:
            survivors.append(code)
    log.info("條件3 通過：%d 檔", len(survivors))

    # ---- 條件 4 + 5：個別公司財報（僅查存活個股）----
    results = []
    for code in survivors:
        fin = ds.fetch_financials(code)
        contract = fin.get("contract_liab_k")
        capital = fin.get("capital_k")
        eps = fin.get("eps")

        cond4 = (contract is not None and capital is not None and
                 contract > capital *
                 CONFIG["contract_liability_capital_ratio"])
        cond5 = (eps is not None and
                 eps > CONFIG["profitability_threshold"])
        if not (cond4 and cond5):
            continue

        cur = t86_today[code]
        prev = t86_prev[code]
        q = quotes.get(code, {})
        big_pct, big_chg = ds.tdcc_weekly_change(code)
        mf_net, mf_src = ds.fetch_main_force_net(code)

        results.append({
            "code": code,
            "name": q.get("name", ""),
            "close": q.get("close"),
            "change": q.get("change"),
            "volume_lots": q.get("volume_lots"),
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
            "transfer_general_lots": round(transfer_lots[code], 1),
            # 財報
            "contract_liab_k": contract,
            "capital_k": capital,
            "eps": eps,
            "fin_period": fin.get("period"),
        })

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": d_today,
        "prev_trade_date": d_prev,
        "results": results,
        "message": f"篩選完成：{len(results)} 檔符合全部條件"
                   f"（法人條件通過 {len(candidates)} 檔）",
    }
    save_results(out)
    log.info(out["message"])
    return out


def watchlist():
    """盤中監控清單 = 最新篩選結果 + 設定檔中額外指定的股票。"""
    codes = [r["code"] for r in load_results().get("results", [])]
    for c in CONFIG["monitor_extra_symbols"]:
        if c not in codes:
            codes.append(c)
    return codes
