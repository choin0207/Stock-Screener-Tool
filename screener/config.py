# -*- coding: utf-8 -*-
"""篩選條件與系統設定。所有門檻值都可以在這裡調整。"""

CONFIG = {
    # ---- 每日篩選條件 ----
    # 條件1：外資「與」投信當日皆為買超，且合計買超 >= 前一日合計買超 * 此倍數
    "inst_surge_ratio": 2.0,

    # 條件2：三大法人買賣超 > 前一日買賣超的倍數（前一日需為買超）
    "net_buy_ratio": 3.0,

    # 條件3：近 N 日內「一般交易」持股轉讓申報合計不得超過的張數
    "transfer_lookback_days": 30,
    "transfer_max_lots": 100,          # 100 張 = 100,000 股

    # 條件4：合約負債 > 實收資本額（股本）× 此比例（0.5 = 資本額的一半即可）
    "contract_liability_capital_ratio": 0.5,

    # 條件5：獲利能力門檻（預設用「基本每股盈餘 EPS」> 1.5 元）
    "profitability_metric": "eps",     # eps
    "profitability_threshold": 1.5,

    # 通過條件①②的候選股最多評估幾檔財報（保護 MOPS 不被大量請求）
    "max_financial_queries": 40,

    # 全市場財報掃描（run_finscan.py）：資料超過此天數視為過期需重抓
    "finscan_max_age_days": 60,

    # ---- 盤中量能監控 ----
    "monitor_interval_min": 10,        # 每 10 分鐘檢查一次
    "volume_spike_ratio": 10.0,        # 最近一段量 > 當日先前每段平均量的倍數
    "monitor_extra_symbols": [],       # 除了篩選結果外，額外要監控的股票代號
    "market_open": "09:00",
    "market_close": "13:30",

    # ---- 持股風險紅黃綠燈（自選/持股監控，run_monitor.py 內執行）----
    # 持股清單檔：一行一個代號，# 開頭為註解；清單內股票每 10 分鐘評估燈號
    "holdings_file": "watchlist.txt",
    # 市場層級訊號門檻 [黃燈, 紅燈]
    "risk_vix_level": [22.0, 30.0],            # VIX 絕對水位
    "risk_vix_spike_pct": [15.0, 30.0],        # VIX 對前收漲幅 %
    "risk_es_drop_pct": [-1.5, -3.0],          # S&P500 期貨對前收跌幅 %
    "risk_twii_drop_pct": [-2.0, -3.5],        # 加權指數盤中對前收跌幅 %
    "risk_ewt_drop_pct": [-2.0, -3.5],         # EWT(台灣ETF,夜間代理)跌幅 %
    "risk_asia_drop_pct": [-2.0, -3.5],        # 日經/KOSPI 跌幅 %
    # 個股層級訊號門檻
    "risk_stock_drop_pct": [-3.0, -4.5],       # 個股對昨收跌幅 % [黃, 紅]
    "risk_imbalance_ratio": [8.0, 15.0],       # 委賣5檔量/委買5檔量 [黃, 紅]
    "risk_imbalance_min_ask_lots": 300,        # 失衡判斷的最低委賣掛單量（張）
    "risk_dump_vol_ratio": 5.0,                # 急跌時最近段量>平均段量此倍數→紅燈
    "risk_market_overlay_drop_pct": -1.0,      # 市場紅燈時個股跌逾此%才跟著升紅

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
