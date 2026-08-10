# -*- coding: utf-8 -*-
"""CLI：產生選股成效報告 docs/report.html。供 report.yml 呼叫。"""

import logging

from screener import report

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    out = report.build_report()
    print(f"報告已產生 {out['path']}：定案 {out['done_n']} 筆、"
          f"勝率 {out['win_rate']}%、平均報酬 {out['avg_ret']}%、"
          f"同期大盤 {out['twii']}%")
