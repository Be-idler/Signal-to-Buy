"""L-Q 분기 전수 적재 (명세서 §2) — 정기공시 마감 다음 주 실행.

⚠️ 시총은 저장하지 않는다(mktcap=None). 시총 결합은 트리거 A(일일)에서 수행.

사용: python run_quarterly.py <year> <reprt_code>
  reprt_code: 11011(사업) 11012(반기) 11013(1분기) 11014(3분기)
  예) python run_quarterly.py 2025 11011

- 체크포인트: checkpoints/quarterly_{year}_{reprt}.json (중단 지점 재개)
- 산출: financials/{year}_{reprt}.parquet (정규화 SSOT, ticker 단위 1행)
- 다년 백필: 과거 5개년 사업보고서가 없으면 함께 적재
- 병렬 수집: DART 호출 WORKERS(4)개 동시 실행 (한도 내 정중한 속도)
- 시간 예산: GitHub Actions 잡 한도(6h)에 걸리기 전 MAX_RUNTIME_MIN(320분)에
  스스로 체크포인트 저장 후 종료하고 `.continue_needed` 마커를 남긴다 —
  워크플로가 이를 감지해 같은 인자로 새 런을 자동 재발행(self-continuation).
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import time
import traceback

import pandas as pd

from dhandho import dart, notify, storage

WORKERS = int(os.environ.get("QUARTERLY_WORKERS", "4"))
MAX_RUNTIME_MIN = float(os.environ.get("MAX_RUNTIME_MIN", "320"))
CONTINUE_MARKER = ".continue_needed"
_START = time.monotonic()


def _time_left() -> bool:
    return (time.monotonic() - _START) < MAX_RUNTIME_MIN * 60

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


def _fetch_one(ticker: str, corp: str, year: int, reprt_code: str):
    """단일 종목 수집 (워커 스레드). 반환: (ticker, corp, fin|None, err|None)."""
    try:
        rows = dart.get_financials(corp, year, reprt_code)
        fin = dart.normalize_financials(rows) if rows else None
        return ticker, corp, fin, None
    except Exception as e:                       # noqa: BLE001 — 개별 실패는 격리
        return ticker, corp, None, e


def collect(year: int, reprt_code: str) -> bool:
    """전 종목 수집. 반환: True=완료, False=시간 예산 소진으로 일시 중단."""
    ckpt_path = f"checkpoints/quarterly_{year}_{reprt_code}.json"
    ckpt = storage.load_json(ckpt_path) or {"done": [], "rows": []}
    done = set(ckpt["done"])

    corp_codes = dart.get_corp_codes()
    todo = [(t, c) for t, c in sorted(corp_codes.items()) if t not in done]
    print(f"[quarterly] {year}_{reprt_code}: {len(corp_codes)} corps, "
          f"{len(done)} done, {len(todo)} todo")

    CHUNK = 200
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i in range(0, len(todo), CHUNK):
            if not _time_left():
                storage.save_json(ckpt, ckpt_path)
                print(f"[quarterly] time budget ({MAX_RUNTIME_MIN}min) reached — "
                      f"checkpoint saved, will self-continue")
                return False
            chunk = todo[i:i + CHUNK]
            results = ex.map(lambda tc: _fetch_one(tc[0], tc[1], year, reprt_code),
                             chunk)
            daily_limit = False
            for ticker, corp, fin, err in results:
                if err is not None:
                    if "daily limit" in str(err):
                        daily_limit = True
                        continue             # done 처리하지 않음 → 재개 시 재시도
                    print(f"[quarterly] {ticker} failed: {err}")
                elif fin is not None:
                    ckpt["rows"].append(_row(ticker, corp, fin))
                done.add(ticker)
            ckpt["done"] = sorted(done)
            storage.save_json(ckpt, ckpt_path)
            print(f"[quarterly] progress {len(done)}/{len(corp_codes)}")
            if daily_limit:
                notify.notify_failure(
                    "run_quarterly",
                    f"DART 일일 한도 초과({year}_{reprt_code}) — 체크포인트 저장됨. "
                    f"내일 같은 인자로 재실행하면 이어서 진행")
                sys.exit(0)                  # 자동 재발행하면 무한루프 → 수동/익일 재개

    df = pd.DataFrame(ckpt["rows"], columns=FIN_COLUMNS)
    storage.upload_parquet(df, f"financials/{year}_{reprt_code}.parquet")
    storage.save_json({"done": sorted(done), "rows": []}, ckpt_path)   # 완료 표시(행 비움)
    print(f"[quarterly] uploaded financials/{year}_{reprt_code}.parquet ({len(df)} rows)")
    return True


def _pause_for_continuation() -> int:
    """시간 예산 소진 — 마커를 남기고 정상 종료(워크플로가 새 런 재발행)."""
    with open(CONTINUE_MARKER, "w") as fh:
        fh.write("time budget reached")
    print("[quarterly] paused — workflow will re-dispatch to continue")
    return 0


def main() -> int:
    year = int(sys.argv[1])
    reprt = sys.argv[2] if len(sys.argv) > 2 else dart.REPRT_ANNUAL
    try:
        if not collect(year, reprt):
            return _pause_for_continuation()
        if reprt == dart.REPRT_ANNUAL:
            # 다년 지표용 과거 사업보고서 백필 (없는 연도만)
            for y in range(year - 5, year):
                if not storage.exists(f"financials/{y}_{dart.REPRT_ANNUAL}.parquet"):
                    print(f"[quarterly] backfilling {y} annual")
                    if not collect(y, dart.REPRT_ANNUAL):
                        return _pause_for_continuation()
        else:
            # 분기/반기: TTM 계산에 직전 연간 + 전년 동기 보고서가 필요
            for y2, r2 in ((year - 1, dart.REPRT_ANNUAL), (year - 1, reprt)):
                if not storage.exists(f"financials/{y2}_{r2}.parquet"):
                    print(f"[quarterly] backfilling {y2}_{r2} for TTM")
                    if not collect(y2, r2):
                        return _pause_for_continuation()
        notify.send_bot1(f"✅ 분기 적재 완료: {year}_{reprt} (+백필)")
        return 0
    except SystemExit:
        raise
    except Exception:
        notify.notify_failure("run_quarterly", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
