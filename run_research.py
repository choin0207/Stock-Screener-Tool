# -*- coding: utf-8 -*-
"""CLI：一年歷史回測研究（買進指標與跌前訊號評估）。

抓過去約一年的全市場行情與法人資料（逐日快取於 .cache/research/，
重跑只補新交易日），評估候選指標並寫入 docs/data/research.json，
供網頁「指標回測研究」卡片顯示。由 GitHub Actions（research.yml）
每週自動執行，或修改 research-request.txt 推送觸發。"""

import logging
import sys

from screener import research

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if __name__ == "__main__":
    try:
        result = research.run()
    except RuntimeError as e:
        print(f"回測中止：{e}")
        sys.exit(1)
    u, b = result["universe"], result["baseline"]
    adopted_buy = [f["label"] for f in result["buy_factors"] if f["adopted"]]
    adopted_drop = [f["label"] for f in result["drop_factors"] if f["adopted"]]
    print(f"回測完成：{result['period']['trading_days']} 個交易日、"
          f"{u['stocks']} 檔、{u['eval_points']:,} 個樣本點")
    print(f"基準：D+20 平均 {b['fwd20']}%、勝率 {b['win20']}%、"
          f"5日內跌逾5%機率 {b['drop_rate']}%")
    print("建議採用買進指標：" + ("、".join(adopted_buy) or "無"))
    print("有效跌前訊號：" + ("、".join(adopted_drop) or "無"))
