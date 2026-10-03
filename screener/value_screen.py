# -*- coding: utf-8 -*-
"""高殖利率存股篩選（使用者 2026-10-03 需求）：

Ⓐ 高殖利率：殖利率 ≥ value_yield_min％（官方本益比殖利率表）
Ⓑ 高抵扣稅額：2018 稅改後舊制「可扣抵稅額比率」已廢除，現制為
   股利 8.5% 可抵減稅額（每戶上限 8 萬）→ 以現金股利 ≥ value_div_min 元
   判定，並顯示 每股可抵減稅額 = 股利 × 8.5%
Ⓒ K 值低檔：KD(9) 的 K ≤ value_kd_max（超賣區，反彈機會）
Ⓓ 合約負債 > 股本（資本額）× value_cl_ratio

候選 = Ⓐ∩Ⓓ（先用零成本資料縮小範圍），再對候選逐檔抓 Yahoo 日K算 KD；
產出 docs/data/value_screen.json，前端選股分頁「💰 高殖利率存股篩選」卡。"""

import json
import logging
import os
import time

from . import datasources as ds
from . import technical
from .config import CONFIG

log = logging.getLogger("screener.value")

TAX_CREDIT_RATE = 0.085                # 股利可抵減稅額率（現行稅制 8.5%）


def _data_path(name):
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], name)


def _load_json(name):
    try:
        with open(_data_path(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return None


def run(trade_date=""):
    """執行篩選並寫 value_screen.json。回傳 (候選數, 全符合數)。"""
    yields = ds.fetch_yield_all()
    fins = _load_json("financials.json") or {}
    snap = _load_json("market_snapshot.json") or {}
    stocks = snap.get("stocks", {})

    y_min = CONFIG["value_yield_min"]
    div_min = CONFIG["value_div_min"]
    kd_max = CONFIG["value_kd_max"]
    cl_ratio = CONFIG["value_cl_ratio"]

    cands = []
    for code, yv in yields.items():
        if len(code) != 4 or not code.isdigit() or code.startswith("00"):
            continue
        if yv["yield_pct"] < y_min:                  # Ⓐ 高殖利率（入場券）
            continue
        fin = fins.get(code) or {}
        liab, cap = fin.get("contract_liab_k"), fin.get("capital_k")
        if liab is None or cap is None or not cap:
            continue
        if liab <= cap * cl_ratio:                   # Ⓓ 合約負債>股本（入場券）
            continue
        cands.append((code, yv, fin))
    cands.sort(key=lambda t: -t[1]["yield_pct"])
    cands = cands[:CONFIG["value_kd_max_codes"]]

    rows = []
    for code, yv, fin in cands:
        s = stocks.get(code) or {}
        price = s.get("c")
        div = yv["div_ps"]
        if div is None and price and yv["yield_pct"]:
            div = round(price * yv["yield_pct"] / 100, 2)   # 上市：由殖利率回推
        k_val = d_val = None
        kk = technical.fetch_daily_k(code, yv["market"])
        if kk:
            closes, highs, lows, _ = kk
            ks, dvs = technical._kd(closes, highs, lows)
            k_val, d_val = round(ks[-1], 1), round(dvs[-1], 1)
        time.sleep(technical.DELAY_SEC)
        tc = round(div * TAX_CREDIT_RATE, 3) if div is not None else None
        p = {
            "hy": True,                              # 候選即已過Ⓐ
            "tc": div is not None and div >= div_min,
            "kd": k_val is not None and k_val <= kd_max,
            "cl": True,                              # 候選即已過Ⓓ
        }
        rows.append({
            "code": code,
            "name": yv["name"] or s.get("n", ""),
            "market": yv["market"],
            "price": price,
            "yield_pct": yv["yield_pct"],
            "pe": yv["pe"],
            "div_est": div,
            "tax_credit": tc,                        # 每股可抵減稅額（8.5%）
            "k": k_val, "d": d_val,
            "liab_ratio": (round(fin["contract_liab_k"] / fin["capital_k"], 2)
                           if fin.get("capital_k") else None),
            "eps": fin.get("eps"), "period": fin.get("period"),
            "pass": p,
            "all": all(p.values()),
        })
    rows.sort(key=lambda r: (not r["all"], -(r["yield_pct"] or 0)))

    from . import performance
    out = {
        "generated_at": performance._now().isoformat(timespec="seconds"),
        "trade_date": trade_date or snap.get("trade_date", ""),
        "params": {"yield_min": y_min, "div_min": div_min,
                   "kd_max": kd_max, "cl_ratio": cl_ratio},
        "rows": rows,
    }
    with open(_data_path("value_screen.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    n_all = sum(1 for r in rows if r["all"])
    log.info("高殖利率存股篩選：候選 %d 檔，全符合 %d 檔", len(rows), n_all)
    return len(rows), n_all
