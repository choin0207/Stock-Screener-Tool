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


def _third_day_inst(prev_ymd):
    """抓「前日再往前」一個交易日的法人買賣超（上市+上櫃，單位：股）。
    回傳 (ymd, dict)；找不到回傳 (None, {})。"""
    from datetime import datetime, timedelta
    try:
        d = datetime.strptime(str(prev_ymd), "%Y%m%d").date()
    except Exception:                                        # noqa: BLE001
        return None, {}
    probe = d - timedelta(days=1)
    for _ in range(6):                        # 跳過假日最多找 6 天
        ymd = probe.strftime("%Y%m%d")
        data = ds.fetch_t86(ymd)
        if data:
            merged = dict(data)
            merged.update(ds.fetch_tpex_inst(ymd))
            return ymd, merged
        probe -= timedelta(days=1)
        time.sleep(1)
    return None, {}


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
    cl_min = CONFIG["value_cl_min"]              # Ⓓ 倍數區間（使用者定 3~5 倍）
    cl_max = CONFIG["value_cl_max"]
    target = CONFIG.get("value_target_count", 10)

    cands = []
    for code, yv in yields.items():
        if len(code) != 4 or not code.isdigit() or code.startswith("00"):
            continue
        if y_min > 0 and yv["yield_pct"] < y_min:    # Ⓐ 殖利率（0=不設限）
            continue
        fin = fins.get(code) or {}
        liab, cap = fin.get("contract_liab_k"), fin.get("capital_k")
        if liab is None or cap is None or not cap:
            continue
        if not (cap * cl_min < liab <= cap * cl_max):   # Ⓓ 倍數區間（入場券）
            continue
        price = (stocks.get(code) or {}).get("c")
        div = yv["div_ps"]
        if div is None and price and yv["yield_pct"]:
            div = round(price * yv["yield_pct"] / 100, 2)   # 上市：由殖利率回推
        if div is None or div < div_min:             # Ⓑ 現金股利（入場券）
            continue
        cands.append((code, yv, fin, div))
    # 以合約負債倍數由高到低排序（Ⓓ 自動門檻取倍數最高者，KD 運算額度留給它們）
    cands.sort(key=lambda t: -(t[2]["contract_liab_k"] / t[2]["capital_k"]))
    cands = cands[:CONFIG["value_kd_max_codes"]]

    # 近三日法人：今日/前日取自快照(f/f0,t/t0,x/x0，張)，第三日另抓一次 T86+TPEx
    d_t, d_p = snap.get("trade_date", ""), snap.get("prev_trade_date", "")
    d_pp, t86_pp = (_third_day_inst(d_p) if cands and d_p else (None, {}))

    rows = []
    for code, yv, fin, div in cands:
        s = stocks.get(code) or {}
        price = s.get("c")
        k_val = d_val = None
        tech = None
        kk = technical.fetch_daily_k(code, yv["market"])
        if kk:
            closes, highs, lows, _ = kk
            ks, dvs = technical._kd(closes, highs, lows)
            k_val, d_val = round(ks[-1], 1), round(dvs[-1], 1)
            try:                              # 技術面摘要（同日K零額外請求）
                tech = technical.analyze(*kk)
            except Exception:                                # noqa: BLE001
                pass
        time.sleep(technical.DELAY_SEC)
        pp = t86_pp.get(code) or {}
        inst = {
            "d": [d_t, d_p, d_pp],            # 新→舊
            "f": [s.get("f"), s.get("f0"),
                  round((pp.get("foreign_net") or 0) / 1000, 1)
                  if d_pp else None],
            "t": [s.get("t"), s.get("t0"),
                  round((pp.get("trust_net") or 0) / 1000, 1)
                  if d_pp else None],
            "x": [s.get("x"), s.get("x0"),
                  round((pp.get("total_net") or 0) / 1000, 1)
                  if d_pp else None],
        }
        tc = round(div * TAX_CREDIT_RATE, 3) if div is not None else None
        p = {
            "hy": True,                              # Ⓐ（y_min=0 時不設限）
            "tc": True,                              # Ⓑ 候選已過（股利入場券）
            "kd": k_val is not None and k_val <= kd_max,
            "cl": True,                              # Ⓓ 候選已過（倍數區間）
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
            "inst": inst,
            "tech": tech,
            "pass": p,
            "all": all(p.values()),
        })
    rows.sort(key=lambda r: (not r["all"], -(r["yield_pct"] or 0)))

    from . import performance
    out = {
        "generated_at": performance._now().isoformat(timespec="seconds"),
        "trade_date": trade_date or snap.get("trade_date", ""),
        "params": {"yield_min": y_min, "div_min": div_min,
                   "kd_max": kd_max, "cl_min": cl_min, "cl_max": cl_max,
                   "cl_target": target},
        "rows": rows,
    }
    with open(_data_path("value_screen.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    try:                      # 成效追蹤：記錄+追蹤半年股價（條件×獲利機率分析）
        from . import value_perf
        value_perf.update(rows, stocks, out["trade_date"])
    except Exception:                                        # noqa: BLE001
        log.exception("選股成效追蹤更新失敗（不影響篩選）")
    n_all = sum(1 for r in rows if r["all"])
    log.info("高殖利率存股篩選：候選 %d 檔，全符合 %d 檔", len(rows), n_all)
    return len(rows), n_all
