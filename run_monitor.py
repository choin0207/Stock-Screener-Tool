# -*- coding: utf-8 -*-
"""CLI：執行一次盤中量能檢查（狀態存於 docs/data/monitor_state.json，
每次執行會與上一次的累積量相減）。供 GitHub Actions 每 10 分鐘呼叫。"""

import logging

from screener import monitor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    alerts = monitor.check_volume_spike()
    if alerts:
        for a in alerts:
            print("ALERT:", a["message"])
    else:
        print("無新警示（或非交易時段）")
