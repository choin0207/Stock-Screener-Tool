# Stock-Screener-Tool — Wiki Skeleton

## Repository summary
Taiwan-stock daily 5-criteria screener + intraday volume/risk monitor. Serverless
architecture: compute runs in GitHub Actions (or local Flask), results are committed
as JSON to `docs/data/`, and a static PWA (`docs/`) reads them. Two run modes:
Cloud (GitHub Pages + Actions) and Local (Flask + APScheduler).

Languages/stack: Python 3.12 backend (`screener/` package + 5 top-level entry scripts),
static HTML/JS/CSS PWA (`docs/`), Google Apps Script (optional login), GitHub Actions
workflows, `requests`/`flask`/`apscheduler`.

No committed test suite (verification via offline mocks + py_compile per CLAUDE.md).

## Planned structure

- /openwiki/quickstart.md — Entrypoint. High-level map, run modes, core concepts, task-routing table, backlog. (written LAST)

- /openwiki/architecture/
  - overview.md — System architecture: serverless data flow (Actions → JSON → PWA),
    two run modes, module dependency graph, data contract between backend and frontend.
    Mermaid: flowchart of data flow; module dependency graph.
  - data-contract.md — The JSON files in docs/data/ that couple backend↔frontend:
    screen_results.json, market_snapshot.json, financials.json, alerts.json,
    monitor_state.json, watch_alerts.json, diagnose.json. Field-by-field schemas,
    producers/consumers, abbreviation key. erDiagram/tables.

- /openwiki/screening/
  - overview.md — screener package overview: the 5 criteria, tiering (🏆🟡🔵⚪),
    two-phase filtering (bulk then per-stock), owning symbols. Mermaid flowchart of pipeline.
  - criteria.md — Detailed definition of each of 5 conditions with config keys, formulas,
    the c1..c5 flags, tiering function, watchlist selection. Evidence: screener.run_daily_screen, build_entry, tier.
  - daily-screen.md — run_daily_screen deep dive: sequence, candidate cap, market snapshot,
    full-market contract-liability group. Sequence diagram. Entry: run_screen.py.
  - financial-scan.md — run_finscan.py: full-market MOPS scan → financials.json, resume/age logic.
  - diagnose.md — run_diagnose.py: per-stock 5-criteria diagnosis → diagnose.json.

- /openwiki/data-sources/
  - overview.md — datasources.py overview: sources table, session/UA, caching layer,
    error-tolerance philosophy, unit conventions (shares vs lots vs 仟元).
  - institutional-trades.md — T86 (TWSE) + TPEx institutional net-buy, fetch_t86,
    fetch_tpex_inst (new/old API fallback), fetch_t86_latest_two (latest-two trading days).
  - quotes-and-intraday.md — fetch_daily_quotes (TSE+OTC close), fetch_intraday_volumes,
    fetch_intraday_quotes_full (ask/bid five-tick), MIS realtime endpoint quirks.
  - financials-mops.md — MOPS individual financials: _mops_post, _html_row_value parsing
    (decimal + accounting-negative bugs), _recent_seasons, fetch_financials, rate-limiting/cache.
  - holdings-transfers-misc.md — TDCC big-holder dispersion (weekly, level 15) + weekly change,
    insider share transfers (一般交易 lots), main-force (Fubon fallback), Yahoo market indicators,
    load_holdings, watchlist.txt.

- /openwiki/monitoring/
  - volume-monitor.md — monitor.py: 10-min volume-spike detection, cross-run state
    (monitor_state.json), market-hours gating, alert generation. Entry: run_monitor.py order.
  - risk-lights.md — risk.py: green/yellow/red pre-crash market + per-stock risk lights,
    thresholds, grading, market-overlay, watch_alerts.json + red-alert append. State diagram.

- /openwiki/frontend/
  - pwa-app.md — docs/index.html: dashboard structure, mode detection (server vs cloud),
    data loading loops, tiering/badges rendering, watchlist (localStorage), notifications,
    risk/diagnose cards, TH constants sync with config.py.
  - pwa-shell.md — manifest.json + sw.js caching strategy (network-first shell/config,
    always-network data), config.js AUTH_URL.

- /openwiki/operations/
  - local-server.md — app.py: Flask routes (/, /api/*), APScheduler jobs, threading lock
    for manual screen, static serving of docs/.
  - github-actions.md — The 4 data workflows (daily-screen, intraday-monitor, financial-scan,
    diagnose) + openwiki-update: schedules, triggers (request files), commit-retry pattern,
    concurrency groups. Remote-trigger recipe.
  - auth.md — Optional login: Code.gs (Apps Script + Google Sheet), config.js AUTH_URL,
    frontend session flow, SETUP_AUTH.md, security limitations.
  - configuration.md — screener/config.py: every CONFIG key grouped, defaults, tuning guidance,
    which keys front-end TH must mirror.

## Coverage checklist
- screener/config.py → operations/configuration.md
- screener/datasources.py → data-sources/* (split by domain)
- screener/screener.py → screening/*
- screener/monitor.py → monitoring/volume-monitor.md
- screener/risk.py → monitoring/risk-lights.md
- app.py → operations/local-server.md
- run_screen.py → screening/daily-screen.md
- run_monitor.py → monitoring/volume-monitor.md
- run_finscan.py → screening/financial-scan.md
- run_diagnose.py → screening/diagnose.md
- docs/index.html → frontend/pwa-app.md
- docs/sw.js, manifest.json, config.js → frontend/pwa-shell.md
- .github/workflows/* → operations/github-actions.md
- google-apps-script/Code.gs, SETUP_AUTH.md → operations/auth.md
- docs/data/*.json → architecture/data-contract.md
- watchlist.txt → data-sources/holdings-transfers-misc.md
