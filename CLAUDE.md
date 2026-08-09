# CLAUDE.md — 專案工作記憶

> 本檔為 AI 助理的專案脈絡檔。Claude Code / Cowork 開啟本專案時會自動讀取，
> 讓新對話延續先前的開發脈絡。修改專案時請同步更新本檔的「目前狀態」。

## 專案是什麼

台股每日五條件篩選 + 盤中量能監控的跨平台 PWA，完全免伺服器：

- **正式網頁**：https://choin0207.github.io/Stock-Screener-Tool/ （GitHub Pages，來源 main 分支 /docs）
- **GitHub repo**：https://github.com/choin0207/Stock-Screener-Tool
- 運算全部跑在 GitHub Actions；結果 commit 回 `docs/data/` 更新網頁
- 使用者主要用手機開網頁（已加入主畫面當 App），**不開 GitHub**；
  維運操作透過修改觸發檔推送（見下）

## 五條件與分級（門檻都在 `screener/config.py`）

1. ① 外資+投信皆買超，合計 ≥ 前日 × 2（inst_surge_ratio）
2. ② 三大法人買賣超 > 前日 × 3（net_buy_ratio，原10倍，使用者要求放寬）
3. ③ 近30日內部人「一般交易」轉讓 ≤ 100 張
4. ④ 合約負債 > 資本額 × 0.5（contract_liability_capital_ratio，原1.0，已放寬）
5. ⑤ EPS > 1.5 元

結果分四組：🏆全符合(5/5)、🟡①②+④、🔵全市場④達標（**不論法人動向**，
來自每週財報掃描資料庫）、⚪①②但④未達標。盤中監控只跟 🏆🟡 組。

## 自動排程（.github/workflows/）

| workflow | 排程（UTC） | 推送觸發檔 | 作用 |
|---|---|---|---|
| daily-screen.yml | 平日 07:30、09:00（=台北15:30/17:00） | `screen-request.txt` | 每日篩選＋產出全市場快照 |
| intraday-monitor.yml | 平日 01:00–05:50 每10分 | — | 量能爆量警示（10分鐘段量>當日均段量10倍） |
| financial-scan.yml | 週六 22:00（週五UTC） | `finscan-request.txt` | 全市場財報掃描→financials.json |
| diagnose.yml | 手動/推送 | `diagnose-request.txt`（**只讀第一行**的代號） | 個股五條件診斷→網頁診斷卡 |
| backtest.yml | 手動/推送 | `backtest-request.txt` | 一年回測訓練→backtest.json（首跑約1hr，.cache 走 actions/cache） |

**遠端觸發方式**：`date > <觸發檔> && git commit && git push`。
commit 步驟已含衝突重試（`git pull --rebase -X theirs` ×3）。

## 資料來源與已知地雷

- 上市法人 T86：`twse.com.tw/rwd/zh/fund/T86`；**台北深夜(約00-06時)維護會整批失敗**，
  篩選會 exit 1 顯示紅燈，白天重跑即可
- 上櫃法人：TPEx 新版 `www/zh-tw/insti/dailyTrade` **常回 520**，程式會 fallback
  舊版 `3itrade_hedge_result.php`（固定24欄索引）；兩者都掛過一次
- MOPS 個別財報：POST `mopsov.twse.com.tw/mops/web/ajax_t164sb03`(資產負債表)/
  `ajax_t164sb04`(損益表)，有限速(mops_delay_sec=3)；HTML 解析在
  `_html_row_value()`，**已修過小數截斷與 (123) 會計負數 bug**
- 集保大戶(TDCC 1-5)：週資料，需累積兩週才有「週變動」；歷史存 `.cache/tdcc_history.json`
- ETF 排除：候選股只留 4 碼數字代號（006205 曾造成 MOPS 逾時）
- `.cache/` 不入版控；`docs/data/` 由 Actions commit

## 前端（docs/）

- 純靜態 PWA；SW 快取策略：**頁面/config.js 網路優先**（曾因 cache-first 讓使用者
  看到舊版介面，SHELL 版本 shell-v4）
- 自選名單：localStorage（key `mywatch`），每台裝置獨立；資料來自
  `data/market_snapshot.json`（每日篩選產出，欄位縮寫見 `_save_market_snapshot`）
  + `data/financials.json`；前端 `TH` 常數需與 config.py 門檻同步
- 登入（選用，預設關）：`docs/config.js` 的 AUTH_URL 填 Apps Script 網址即啟用；
  帳密表在使用者私人 Google Sheet；教學在 `SETUP_AUTH.md`，程式在
  `google-apps-script/Code.gs`。使用者說「先不要用帳密」

## 合約負債篩選卡與績效追蹤（2026-08-09 新增）

- 前端「📦 合約負債（在手訂單）篩選」卡片：**純前端**讀 financials.json +
  market_snapshot.json，門檻選單（>0 / ≥資本額50%(④) / ≥100%）＋代號名稱搜尋，
  依「合約負債/資本額」排序，預設顯示30筆可展開；不增加任何後端請求
- `screener/performance.py`：每日篩選成功後（run_daily_screen 尾端 try/except 呼叫）
  記錄 🏆/🟡 入選股（入選日收盤=進場價），之後每個交易日用**當日已抓的行情快照**
  補收盤（零額外 API），追蹤 20 個交易日≈一個月 → `docs/data/performance.json`；
  算 D+5/D+10/D+20 報酬、期間最高最低、定案訊號勝率與平均報酬（總計+分組）。
  同日兩次排程以 (code, entry_date) 去重；逾 45 日曆天不足20筆自動定案（防停牌卡住）；
  檔案最多留 400 筆。前端「📈 篩選成效追蹤」卡片顯示，作為選股是否值得買進的證據
- **跌前警告引擎**（同在 performance.py）：追蹤期間每天檢查五條件因素是否轉弱——
  外資/投信轉賣（單日賣超吃掉入選日買超、或連2日賣超）、法人連2日賣超、
  內部人轉讓入選後增逾100張或破門檻、新一季合約負債降逾20%或跌破④門檻
  （perf_contract_drop_pct）、新一季EPS低於⑤門檻且較入選時差。
  首次觸發附加 alerts.json（沿用量能警示卡+通知，同因素不重複發）；
  跌破入選價5%（perf_drop_alert_pct）記「下跌事件」並歸因哪些警訊先出現、
  提前幾個交易日，累積各因素命中率統計（summary.drop_stats）顯示在前端
  「下跌前兆統計」。法人流向存 rec.flows（來自當日已抓的 T86，零額外 API）
- 技術跌前訊號（2026-08-09 二補）：爆量下跌（量>5日均2倍且跌>2%）、
  跌破5日線（向下穿越才觸發）、連3黑，資料只用 record 內 prices/vols，零額外請求；
  FACTORS cond="技" 顯示為「技術訊號」
- 離線測試：scratchpad test_performance.py 38 項全過

## 一年回測訓練（2026-08-09 新增）

- `screener/backtest.py` + `run_backtest.py` + backtest.yml（backtest-request.txt 觸發）：
  ^TWII 取交易日 → 逐日抓一年 T86+TPEx（.cache 逐日快取，workflow 用 actions/cache
  保留）→ 重演①②訊號 → Yahoo 抓各訊號股 15 個月日K → MOPS 補「訊號當時已公布」
  季度的④⑤（backtest_max_mops_pairs=250 上限，近期訊號優先）
- 產出 docs/data/backtest.json：因子 IC＋高低分組勝率、跌前指標
  precision/recall/中位提前天數、**評分權重**（live 因子正 IC 正規化）
- `screener.py` 每日篩選讀 backtest.load_weights() → 每檔 `score` 0-100，
  同分級內按評分排序；前端「評分」chip＋「🧪 一年回測」卡片
- **③內部人與集保大戶無歷史資料，無法回測**；樣本僅數百筆，權重僅供參考
- 修正：4 碼 ETF（0050/0056 等 00 開頭）現於篩選與回測皆排除
- 離線測試：scratchpad test_backtest.py 21 項全過（純函式，不碰網路）

## 持股風險紅黃綠燈（2026-08-09 新增）

- 持股清單：repo 根目錄 `watchlist.txt`（一行一代號，# 註解）。前端 localStorage
  自選名單與此檔**互相獨立**——燈號監控只看 watchlist.txt，使用者說要加誰就編輯此檔
- `screener/risk.py`：市場訊號（VIX 水位/飆升、ES=F、^TWII、EWT、日經、KOSPI，
  皆 Yahoo chart API）+ 個股訊號（跌幅、委賣/委買五檔失衡、急跌爆量）→
  `docs/data/watch_alerts.json`；紅燈「轉紅」時追加 alerts.json（不重複轟炸）
- **台指期夜盤無免費即時源**：以 EWT（美股台灣ETF）+ ES=F 作夜間代理（標註假設）
- 執行順序陷阱：`run_monitor.py` 必須**先 risk.assess() 再 check_volume_spike()**，
  因後者會把 monitor_state 更新成本段累積量，反過來跑「最近段量」恆為 0
- 監控 workflow cron 提前到 UTC 0-5（台北 08:00 起），開盤前僅市場訊號
- 門檻都在 config.py `risk_*`；離線測試 13 項於 scratchpad test_risk.py 全過

## 目前狀態（2026-08-09 更新）

- GitHub 推送認證：使用者已在互動終端登入過一次，GCM 憑證存於 Windows
  認證管理員（git:https://github.com），助理可直接推送
- 本機 Windows Python 3.14 已補裝 requests / tzdata / pyyaml 供離線測試
- 自選名單「加入後自動比對條件」本來就是自動的（前端讀快照即時算①②④⑤），
  只差 market_snapshot.json 首份資料

- 財報掃描資料庫 `docs/data/financials.json`：已涵蓋 1,316 檔（全市場約1,800），
  已觸發續掃補剩餘
- 全市場快照 `market_snapshot.json`：**尚未產生**——首次觸發碰上深夜維護失敗，
  等下一次成功的 Daily Screen（週一15:30自動或手動觸發）後，自選名單卡片才有數據
- 個案：3379彬台=④達標但法人未進場（會落🔵組）、EPS -0.54；
  6152百一=合約負債4.71億僅佔資本額28%、④未達標（使用者關注中，放自選名單）；
  3443創意=最近一次唯一5/5全過
- 使用者曾詢問未做的事：多使用者自選名單同步（目前 localStorage 各裝置獨立；
  升級方案=登入後存 Google Sheet）

## 慣例

- 對使用者溝通用繁體中文；使用者用手機操作、不熟 GitHub，
  盡量由助理透過觸發檔代辦，避免要求使用者開 GitHub
- 測試：改動後跑離線測試（mock 資料來源）+ `python3 -m py_compile` +
  workflow YAML 驗證 + 前端 JS `new Function()` 語法檢查
- commit 訊息用繁中；資料 commit 加 `[skip ci]`

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
