# -*- coding: utf-8 -*-
"""CLI：全市場財報掃描（上市+上櫃全部 4 碼個股）。

逐檔抓取 MOPS 個別財報（合約負債、股本、EPS），存入
docs/data/financials.json，供每日篩選的「全市場合約負債達標股」分組使用。
財報為季資料，每週掃描一次即可；已有近期資料的個股會跳過，
單次執行超過時間上限會先存檔、下次續掃。
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta

from screener.config import CONFIG
from screener import datasources as ds

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("finscan")

MAX_HOURS = 5          # 單次執行時間上限（Actions 單一 job 上限 6 小時）


def out_path():
    os.makedirs(CONFIG["data_dir"], exist_ok=True)
    return os.path.join(CONFIG["data_dir"], "financials.json")


def save(fin_all):
    with open(out_path(), "w", encoding="utf-8") as f:
        json.dump(fin_all, f, ensure_ascii=False)


def main():
    quotes = ds.fetch_daily_quotes()
    codes = sorted(c for c in quotes if len(c) == 4 and c.isdigit())
    if not codes:
        log.error("無法取得個股清單")
        return

    try:
        with open(out_path(), encoding="utf-8") as f:
            fin_all = json.load(f)
    except Exception:                                        # noqa: BLE001
        fin_all = {}

    cutoff = (datetime.now() -
              timedelta(days=CONFIG["finscan_max_age_days"])
              ).isoformat(timespec="seconds")
    todo = [c for c in codes
            if not fin_all.get(c, {}).get("period")
            or fin_all[c].get("fetched_at", "") < cutoff]
    log.info("財報掃描：全市場 %d 檔，待更新 %d 檔", len(codes), len(todo))

    start, done = time.time(), 0
    for code in todo:
        if time.time() - start > MAX_HOURS * 3600:
            log.warning("達單次時間上限，先存檔（其餘 %d 檔下次續掃）",
                        len(todo) - done)
            break
        fin = ds.fetch_financials(code)
        fin["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        fin_all[code] = fin
        done += 1
        if done % 25 == 0:
            save(fin_all)
            log.info("進度 %d/%d（最新: %s %s）",
                     done, len(todo), code, fin.get("period"))

    save(fin_all)
    with_data = sum(1 for v in fin_all.values() if v.get("period"))
    print(f"本次掃描 {done} 檔；資料庫共 {len(fin_all)} 檔"
          f"（有財報資料 {with_data} 檔）")


if __name__ == "__main__":
    main()
