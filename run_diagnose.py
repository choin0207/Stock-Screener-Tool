# -*- coding: utf-8 -*-
"""CLI：個股五條件診斷。用法：python run_diagnose.py 3379
逐條檢查該股是否符合①~⑤，附實際數據，結果寫入 docs/data/diagnose.json
（網頁的「個股條件診斷」卡片會顯示）。供 GitHub Actions 的 Diagnose Stock
workflow 呼叫。"""

import json
import logging
import os
import sys
from datetime import datetime

from screener.config import CONFIG
from screener import datasources as ds

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def lots(shares):
    return f"{shares / 1000:+,.0f} 張"


def main(code):
    checks = []

    def add(label, ok, detail):
        checks.append({"label": label, "ok": ok, "detail": detail})

    quotes, _ = ds.fetch_daily_quotes()
    q = quotes.get(code, {})
    t_today, t_prev, d1, d0 = ds.fetch_t86_latest_two()
    cur, prev = t_today.get(code), t_prev.get(code)

    if cur is None:
        add("法人資料", None,
            "最新交易日的三大法人資料查無此代號（當日無法人進出紀錄，"
            "或上櫃資料來源暫時失效）")
    else:
        f_net, t_net = cur["foreign_net"], cur["trust_net"]
        inst_today = f_net + t_net
        inst_prev = (prev["foreign_net"] + prev["trust_net"]) if prev else 0
        ratio = CONFIG["inst_surge_ratio"]
        c1 = (f_net > 0 and t_net > 0 and inst_today > 0 and
              inst_today > max(inst_prev, 0) * ratio)
        add(f"① 外資+投信大量買超（合計>前日×{ratio:g}，兩者皆買超）", c1,
            f"外資 {lots(f_net)}、投信 {lots(t_net)}，"
            f"合計 {lots(inst_today)} vs 前日合計 {lots(inst_prev)}")

        nb = CONFIG["net_buy_ratio"]
        prev_total = prev["total_net"] if prev else 0
        c2 = bool(prev and prev_total > 0 and
                  cur["total_net"] > prev_total * nb)
        add(f"② 三大法人買賣超>前日{nb:g}倍（前日需為買超）", c2,
            f"今日 {lots(cur['total_net'])} vs 前日 {lots(prev_total)}")

    t_lots = ds.transfer_general_lots(code)
    add(f"③ 近{CONFIG['transfer_lookback_days']}日一般交易轉讓"
        f"≤{CONFIG['transfer_max_lots']}張",
        t_lots <= CONFIG["transfer_max_lots"], f"申報合計 {t_lots:,.0f} 張")

    fin = ds.fetch_financials(code)
    contract, capital, eps = (fin.get("contract_liab_k"),
                              fin.get("capital_k"), fin.get("eps"))
    r = CONFIG["contract_liability_capital_ratio"]
    if contract is not None and capital is not None:
        c4 = contract > capital * r
        detail4 = (f"合約負債 {contract / 1e5:,.2f} 億 vs 資本額×{r:g} = "
                   f"{capital * r / 1e5:,.2f} 億（{fin.get('period')}）")
    else:
        c4, detail4 = False, "查無財報資料（合約負債或股本）"
    add(f"④ 合約負債>資本額×{r:g}", c4, detail4)

    th = CONFIG["profitability_threshold"]
    c5 = eps is not None and eps > th
    add(f"⑤ EPS>{th:g}元", c5,
        f"EPS {eps} 元（{fin.get('period')}）" if eps is not None
        else "查無 EPS 資料")

    out = {
        "code": code,
        "name": q.get("name", ""),
        "market": q.get("market"),
        "close": q.get("close"),
        "trade_date": d1,
        "prev_trade_date": d0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    with open(os.path.join(CONFIG["data_dir"], "diagnose.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"=== {code} {out['name']} 條件診斷（交易日 {d1}）===")
    for c in checks:
        mark = "✓" if c["ok"] else ("？" if c["ok"] is None else "✗")
        print(f"[{mark}] {c['label']}｜{c['detail']}")


if __name__ == "__main__":
    main(sys.argv[1].strip() if len(sys.argv) > 1 else "3379")
