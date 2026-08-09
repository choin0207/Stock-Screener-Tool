# -*- coding: utf-8 -*-
"""CLI：執行一年歷史回測訓練，寫入 docs/data/backtest.json。
供 GitHub Actions（backtest.yml）呼叫；第一次跑約 1 小時（含限速）。"""

import logging
import sys

from screener import backtest

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    try:
        out = backtest.run_backtest()
    except Exception as e:                                   # noqa: BLE001
        logging.exception("回測失敗: %s", e)
        sys.exit(1)
    print(f"回測完成：{out['outcome']['n']} 筆訊號，"
          f"20日勝率 {out['outcome']['win_rate_20d']}%，"
          f"權重 {out['weights']}")
    sys.exit(0)
