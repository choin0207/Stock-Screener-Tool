# -*- coding: utf-8 -*-
"""選股成效報告產生器：把實盤追蹤（performance.json）與回測（backtest.json）
整理成一頁繁中報告 docs/report.html，供分享與產品化評估。

由 report.yml 排程（每年 11/10，即首次累積三個月時）或 report-request.txt
觸發產生；也可隨時手動觸發看階段性數據。純模板渲染、不依賴 AI。
"""

import json
import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:                                          # pragma: no cover
    ZoneInfo = None

from .config import CONFIG
from . import backtest as bt

MIN_SAMPLES = 30       # 定案樣本低於此數 → 報告標註「初步參考」


def _load(name):
    try:
        with open(os.path.join(CONFIG["data_dir"], name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return None


def _fmt(v, d=1, plus=False):
    if v is None:
        return "—"
    s = f"{v:,.{d}f}"
    return ("+" + s) if plus and v > 0 else s


def _twii_return(d_from, d_to):
    """加權指數同期報酬 %（抓不到回傳 None）。"""
    try:
        px = bt._yahoo_daily("%5ETWII", "5y")
        ds = sorted(d for d in px if d_from <= d <= d_to)
        if len(ds) < 2:
            return None
        a, b = px[ds[0]][0], px[ds[-1]][0]
        return round((b - a) / a * 100, 2)
    except Exception:                                        # noqa: BLE001
        return None


def _ret_distribution(recs):
    """定案報酬分布：<-10 / -10~-5 / -5~0 / 0~5 / 5~10 / >10 (%)"""
    buckets = [("<-10%", 0), ("-10~-5%", 0), ("-5~0%", 0),
               ("0~5%", 0), ("5~10%", 0), (">10%", 0)]
    edges = [-10, -5, 0, 5, 10]
    out = [list(b) for b in buckets]
    vals = []
    for r in recs:
        v = r.get("ret_d20") if r.get("ret_d20") is not None else r.get("ret_pct")
        if v is None:
            continue
        vals.append(v)
        idx = sum(1 for e in edges if v >= e)
        out[idx][1] += 1
    return out, vals


def build_report():
    perf = _load("performance.json") or {}
    back = _load("backtest.json") or {}
    now = (datetime.now(ZoneInfo(CONFIG["timezone"]))
           if ZoneInfo else datetime.now())

    recs = perf.get("records", [])
    done = [r for r in recs if r.get("done")]
    s = perf.get("summary") or {}
    ds = s.get("done") or {}
    drop = (s.get("drop_stats") or {})
    dist, vals = _ret_distribution(done)

    d_from = min((r["entry_date"] for r in recs), default=None)
    d_to = perf.get("trade_date")
    twii = _twii_return(d_from, d_to) if d_from and d_to else None

    o = back.get("outcome") or {}
    ws = (back.get("week_stats") or {}).get("overall") or {}
    weights = back.get("weights") or {}
    wlabels = {"inst_ratio": "①外投放大", "net_ratio": "②法人放大",
               "liab_ratio": "④合約負債", "eps": "⑤EPS"}

    warn_rows = ""
    facs = (drop.get("factors") or {})
    for f in sorted(facs.values(), key=lambda x: -(x.get("hit") or 0)):
        if not f.get("warned"):
            continue
        acc = (f"{f['warn_then_drop']}/{f['warn_settled']}"
               if f.get("warn_settled") else "—")
        warn_rows += (f"<tr><td>{f['label']}（{f['cond']}）</td>"
                      f"<td>{f['warned']}</td><td>{acc}</td>"
                      f"<td>{f['hit']}/{drop.get('drops_total', 0)}</td></tr>")

    dist_rows = "".join(
        f"<tr><td>{lab}</td><td>{n}</td>"
        f"<td>{_fmt(n / len(vals) * 100 if vals else None, 0)}%</td></tr>"
        for lab, n in dist)

    caveat = ("" if ds.get("n", 0) >= MIN_SAMPLES else
              '<div class="warn">⚠️ 定案樣本尚少（未滿 30 筆），'
              "以下實盤統計僅供初步參考，請待樣本累積後再下結論。</div>")

    def date_h(x):
        return f"{x[:4]}/{x[4:6]}/{x[6:]}" if x else "—"

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>選股成效報告｜台股選股雷達</title>
<style>
 body {{ margin:0; padding:16px; background:#f5f6fa; color:#1a2233;
        font:15px/1.7 "Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif; }}
 @media (prefers-color-scheme: dark) {{ body {{ background:#101420; color:#e8ebf2; }}
   .card {{ background:#1a2030 !important; border-color:#2b3245 !important; }}
   th,td {{ border-color:#2b3245 !important; }} }}
 .card {{ background:#fff; border:1px solid #e3e6ee; border-radius:12px;
         padding:16px 18px; margin:0 auto 14px; max-width:760px; }}
 h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:0 0 10px; }}
 .sub {{ color:#66708a; font-size:12.5px; }}
 table {{ border-collapse:collapse; width:100%; font-size:13.5px; }}
 th,td {{ padding:6px 8px; border-bottom:1px solid #e3e6ee; text-align:right; }}
 th:first-child,td:first-child {{ text-align:left; }}
 th {{ color:#66708a; font-weight:600; }}
 .big {{ font-size:22px; font-weight:700; }}
 .up {{ color:#c92a3d; }} .down {{ color:#1a9955; }}
 .warn {{ background:#fff4e5; border:1px solid #f0b45c; border-radius:10px;
         padding:10px 12px; margin:10px 0; font-size:14px; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
         gap:10px; }}
 .kpi {{ border:1px solid #e3e6ee; border-radius:10px; padding:10px 12px; }}
 .kpi span {{ display:block; color:#66708a; font-size:12px; }}
</style></head><body>
<div class="card">
 <h1>📊 選股成效報告</h1>
 <div class="sub">台股選股雷達｜報告期間 {date_h(d_from)} – {date_h(d_to)}
  ｜產生於 {now.strftime("%Y/%m/%d %H:%M")}｜僅供研究參考，不構成投資建議</div>
 {caveat}
</div>

<div class="card"><h2>一、實盤篩選成效（🏆/🟡 入選股，入選日收盤進場、追蹤20交易日）</h2>
 <div class="grid">
  <div class="kpi"><span>累計訊號</span><b class="big">{s.get('total', 0)}</b> 筆</div>
  <div class="kpi"><span>已定案</span><b class="big">{ds.get('n', 0)}</b> 筆</div>
  <div class="kpi"><span>定案勝率</span><b class="big">{_fmt(ds.get('win_rate'), 1)}%</b></div>
  <div class="kpi"><span>平均報酬</span><b class="big {('up' if (ds.get('avg_ret') or 0) > 0 else 'down')}">{_fmt(ds.get('avg_ret'), 2, plus=True)}%</b></div>
  <div class="kpi"><span>同期加權指數</span><b class="big">{_fmt(twii, 2, plus=True)}%</b></div>
 </div>
 <p class="sub">分組：🏆全符合 {json.dumps((s.get('by_tier') or {}).get('0'), ensure_ascii=False)}；
 🟡①②+④ {json.dumps((s.get('by_tier') or {}).get('1'), ensure_ascii=False)}</p>
 <h2 style="margin-top:14px">定案報酬分布（D+20）</h2>
 <table><tr><th>區間</th><th>筆數</th><th>占比</th></tr>{dist_rows}</table>
</div>

<div class="card"><h2>二、跌前警訊命中率（實盤累積）</h2>
 <p class="sub">下跌事件＝跌破入選價 5%；「準確率」＝發警後該筆最終下跌的比例、
 「覆蓋」＝下跌事件中事先有此警訊的比例</p>
 <table><tr><th>警訊因素</th><th>發警檔數</th><th>準確率</th><th>覆蓋</th></tr>
 {warn_rows or '<tr><td colspan="4">尚無警訊紀錄</td></tr>'}</table>
</div>

<div class="card"><h2>三、歷史回測對照（{(back.get('period') or {}).get('from', '—')}
 – {(back.get('period') or {}).get('to', '—')}）</h2>
 <div class="grid">
  <div class="kpi"><span>①②訊號數</span><b class="big">{o.get('n', '—')}</b></div>
  <div class="kpi"><span>20日勝率</span><b class="big">{_fmt(o.get('win_rate_20d'), 1)}%</b></div>
  <div class="kpi"><span>平均20日報酬</span><b class="big">{_fmt(o.get('avg_ret20'), 2, plus=True)}%</b></div>
  <div class="kpi"><span>一週漲≥2%機率</span><b class="big">{_fmt(ws.get('p2'), 0)}%</b></div>
 </div>
 <p class="sub">評分權重：{ '、'.join(f"{wlabels.get(k, k)} {v * 100:.0f}%"
                                    for k, v in weights.items()) or '尚未產生'}</p>
</div>

<div class="card"><h2>四、方法與限制</h2>
 <p class="sub">・訊號定義：①外資+投信買超較前日放大 ②三大法人買賣超放大
 ③內部人轉讓有限 ④合約負債>資本額50% ⑤EPS>1.5。<br>
 ・實盤為系統每日自動篩選的真實紀錄（非事後回填）；進場價=入選日收盤，
 未計交易成本與滑價。<br>
 ・回測樣本受財報抓取上限影響，④⑤因子覆蓋以近期訊號為主。<br>
 ・樣本仍在累積中，統計會隨時間更新；本報告不構成投資建議。</p>
 <p class="sub"><a href="./index.html">← 回選股雷達</a></p>
</div>
</body></html>"""

    path = os.path.join("docs", "report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return {"path": path, "done_n": ds.get("n", 0),
            "win_rate": ds.get("win_rate"), "avg_ret": ds.get("avg_ret"),
            "twii": twii}
