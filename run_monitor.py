# -*- coding: utf-8 -*-
"""CLI：執行一次盤中監控（GitHub Actions 每 10 分鐘呼叫）。

順序固定：先跑持股風險紅黃綠燈（risk.assess 需要「上一段」的累積量狀態），
再跑量能監控（check_volume_spike 會把狀態更新成本段累積量）。"""

import logging

from screener import monitor, risk

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    result = risk.assess()
    if result:
        market = result["market"]["light"]
        reds = [c for c, s in result["stocks"].items() if s["light"] == "red"]
        print(f"風險評估：市場={market}，持股紅燈={reds or '無'}")
    else:
        print("風險評估：週末未執行")

    alerts = monitor.check_volume_spike()
    if alerts:
        for a in alerts:
            print("ALERT:", a["message"])
    else:
        print("無新量能警示（或非交易時段）")

    n = monitor.update_intraday_quotes()
    print(f"盤中價同步 {n} 檔" if n else "盤中價同步：非交易時段未執行")

    k = monitor.check_limitup_open_strength()
    print(f"漲停續強觀察 {k} 檔" if k else "漲停續強觀察：非觀察時段未執行")
