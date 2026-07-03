"""L-Q 분기 전수 적재 (명세서 §2) — 정기공시 마감 다음 주 실행.

⚠️ 시총은 저장하지 않는다(mktcap=None). 시총 결합은 트리거 A(일일)에서 수행.

사용: python run_quarterly.py <year> <reprt_code>
  reprt_code: 11011(사업) 11012(반기) 11013(1분기) 11014(3분기)
  예) python run_quarterly.py 2025 11011

- 체크포인트: checkpoints/quarterly_{year}_{reprt}.json (중단 지점 재개)
- 산출: financials/{year}_{reprt}.parquet (정규화 SSOT, ticker 단위 1행)
- 다년 백필: 과거 5개년 사업보고서가 없으면 함께 적재
"""
from __future__ import annotations

import sys
import time
import traceback

import pandas as pd

from dhandho import dart, notify, storage

FIN_COLUMNS = (["ticker", "corp_code", "revenue", "operating_income", "gross_profit",
                "net_income", "net_income_controlling", "total_assets",
                "total_liabilities", "total_equity", "equity_controlling",
                "current_assets", "current_liabilities", "cash_and_equivalents",
                "short_term_investments", "total_borrowings", "ppe", "cfo", "capex",
                "interest_expense", "depreciation", "fs_div", "flags"]
               # 분기/반기 누적치 — TTM(직전 12개월) 계산용 (dart.FLOW_KEYS)
               + [f"{k}_cum" for k in dart.FLOW_KEYS])


def _row(ticker: str, corp_code: str, fin: dict) -> dict:
    row = {c: fin.get(c) for c in FIN_COLUMNS}
    row["ticker"] = ticker
    row["corp_code"] = corp_code
    row["flags"] = ";".join(fin.get("flags", []))
    return row


def collect(year: int, reprt_code: str) -> None:
    ckpt_path = f"checkpoints/quarterly_{year}_{reprt_code}.json"
    ckpt = storage.load_json(ckpt_path) or {"done": [], "rows": []}
    done = set(ckpt["done"])

    corp_codes = dart.get_corp_codes()
    print(f"[quarterly] {len(corp_codes)} listed corps, {len(done)} already done")

    processed = 0
    for ticker, corp in sorted(corp_codes.items()):
        if ticker in done:
            continue
        try:
            rows = dart.get_financials(corp, year, reprt_code)
            if rows:
                fin = dart.normalize_financials(rows)
                ckpt["rows"].append(_row(ticker, corp, fin))
        except RuntimeError as e:
            if "daily limit" in str(e):
                # 한도 초과 → 체크포인트 저장 후 종료(다음 실행에서 재개)
                storage.save_json(ckpt, ckpt_path)
                notify.notify_failure("run_quarterly", f"DART 한도 초과 — {processed}건 후 중단, 체크포인트 저장됨")
                return
            print(f"[quarterly] {ticker} failed: {e}")
        done.add(ticker)
        ckpt["done"] = sorted(done)
        processed += 1
        if processed % 200 == 0:
            storage.save_json(ckpt, ckpt_path)
            print(f"[quarterly] progress {processed}")
        time.sleep(0.1)   # 호출 한도 보호

    df = pd.DataFrame(ckpt["rows"], columns=FIN_COLUMNS)
    storage.upload_parquet(df, f"financials/{year}_{reprt_code}.parquet")
    storage.save_json({"done": sorted(done), "rows": []}, ckpt_path)   # 완료 표시(행 비움)
    print(f"[quarterly] uploaded financials/{year}_{reprt_code}.parquet ({len(df)} rows)")


def main() -> int:
    year = int(sys.argv[1])
    reprt = sys.argv[2] if len(sys.argv) > 2 else dart.REPRT_ANNUAL
    try:
        collect(year, reprt)
        if reprt == dart.REPRT_ANNUAL:
            # 다년 지표용 과거 사업보고서 백필 (없는 연도만)
            for y in range(year - 5, year):
                if not storage.exists(f"financials/{y}_{dart.REPRT_ANNUAL}.parquet"):
                    print(f"[quarterly] backfilling {y} annual")
                    collect(y, dart.REPRT_ANNUAL)
        else:
            # 분기/반기: TTM 계산에 직전 연간 + 전년 동기 보고서가 필요
            for y2, r2 in ((year - 1, dart.REPRT_ANNUAL), (year - 1, reprt)):
                if not storage.exists(f"financials/{y2}_{r2}.parquet"):
                    print(f"[quarterly] backfilling {y2}_{r2} for TTM")
                    collect(y2, r2)
        return 0
    except Exception:
        notify.notify_failure("run_quarterly", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
