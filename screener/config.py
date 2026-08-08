# -*- coding: utf-8 -*-
"""篩選條件與系統設定。所有門檻值都可以在這裡調整。"""

CONFIG = {
    # ---- 每日篩選條件 ----
    # 條件1：外資「與」投信當日皆為買超，且合計買超 >= 前一日合計買超 * 此倍數
    "inst_surge_ratio": 2.0,

    # 條件2：三大法人買賣超 > 前一日買賣超的倍數（前一日需為買超）
    "net_buy_ratio": 10.0,

    # 條件3：近 N 日內「一般交易」持股轉讓申報合計不得超過的張數
    "transfer_lookback_days": 30,
    "transfer_max_lots": 100,          # 100 張 = 100,000 股

    # 條件4：合約負債 > 實收資本額（股本），單位一致後比較
    "contract_liability_vs_capital": True,

    # 條件5：獲利能力門檻（預設用「基本每股盈餘 EPS」> 1.5 元）
    "profitability_metric": "eps",     # eps
    "profitability_threshold": 1.5,

    # ---- 盤中量能監控 ----
    "monitor_interval_min": 10,        # 每 10 分鐘檢查一次
    "volume_spike_ratio": 10.0,        # 最近一段量 > 當日先前每段平均量的倍數
    "monitor_extra_symbols": [],       # 除了篩選結果外，額外要監控的股票代號
    "market_open": "09:00",
    "market_close": "13:30",

    # ---- 每日排程 ----
    "daily_screen_time": "15:30",      # 台北時間，盤後資料公布後執行

    # ---- 其他 ----
    "timezone": "Asia/Taipei",
    # 結果與警示輸出到 docs/data/：Flask 模式直接當靜態檔提供，
    # GitHub Pages 模式由 Actions 產生後 commit，前端讀同一路徑。
    "data_dir": "docs/data",
    "cache_dir": ".cache",
    "request_timeout": 30,
    "mops_delay_sec": 3,               # MOPS 每次查詢間隔，避免被封鎖
}
