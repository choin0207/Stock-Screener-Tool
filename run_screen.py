# -*- coding: utf-8 -*-
"""CLI：執行一次每日篩選並寫入 docs/data/screen_results.json。
供 GitHub Actions（雲端模式）或 crontab 呼叫。"""

import logging
import sys

from screener import screener

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    out = screener.run_daily_screen()
    print(out["message"])
    # 抓不到法人資料視為失敗，讓 Actions 顯示紅燈以便察覺
    sys.exit(0 if out.get("trade_date") else 1)
