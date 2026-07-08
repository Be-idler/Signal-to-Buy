"""L-Q 분기 전수 적재 (명세서 §2) — 정기공시 마감 다음 주 실행.

⚠️ 시총은 저장하지 않는다(mktcap=None). 시총 결합은 트리거 A(일일)에서 수행.

사용:
  python run_quarterly.py <year> <reprt_code>            # 전수 적재
  python run_quarterly.py <year> <reprt_code> --recollect  # 누락 종목만 재수집
  python run_quarterly.py <year> <reprt_code> --repair <종목코드...>  # 지정 종목 보강
  reprt_code: 11011(사업) 11012(반기) 11013(1분기) 11014(3분기)
  예) python run_quarterly.py 2025 11011
     python run_quarterly.py 2025 11011 --recollect

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
CORP_CODES_CACHE = "meta/corp_codes.json"
# 커버리지 경보: DART 유니버스(corp_codes)에는 비상장·펀드·무자료 법인이 많아
# 정상 적재도 유니버스 대비 ~68%뿐이다(실측 2026 Q1: 2,702/3,976). 따라서
# 유니버스 비율이 아니라 '적재 행수 절대 하한'으로 조용한 빈/부분적재를 잡는다.
COVERAGE_MIN_ROWS = int(os.environ.get("COVERAGE_MIN_ROWS", "1500"))
_START = time.monotonic()


def _time_left() -> bool:
    return (time.monotonic() - _START) < MAX_RUNTIME_MIN * 60


def _corp_codes() -> dict[str, str]:
    """DART 상장사 매핑 — Drive 캐시 폴백.

    corpCode.xml(전 상장사 zip)은 수 MB라 GitHub 러너에서 간헐적으로 수십
    초~수 분씩 걸려 하드 타임아웃에 걸릴 수 있다. 정상 조회 시 Drive에
    캐시하고, 라이브 조회가 실패하면 마지막 캐시로 폴백해 적재·재수집이
    이 한 번의 다운로드 실패로 통째로 중단되지 않도록 한다.
    """
    try:
        codes = dart.get_corp_codes()
        try:
            storage.save_json(codes, CORP_CODES_CACHE)
        except Exception as e:                       # noqa: BLE001 — 캐시 저장 실패는 비치명
            print(f"[quarterly] corp_codes 캐시 저장 실패(무시): {e}")
        return codes
    except Exception as e:                           # noqa: BLE001
        cached = storage.load_json(CORP_CODES_CACHE)
        if cached:
            print(f"[quarterly] corp_codes 라이브 조회 실패 → Drive 캐시 사용 "
                  f"({len(cached)}종목): {e}")
            return cached
        raise

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


def collect(year: int, reprt_code: str) -> int | None:
    """전 종목 수집. 반환: 적재 종목 수(완료), None=시간 예산 소진으로 일시 중단."""
    ckpt_path = f"checkpoints/quarterly_{year}_{reprt_code}.json"
    ckpt = storage.load_json(ckpt_path) or {"done": [], "rows": []}
    done = set(ckpt["done"])

    corp_codes = _corp_codes()
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
                return None
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

    # 커버리지 경보 — 적재 행수가 하한 미만이면 조용한 빈/부분적재(네트워크·한도) 의심
    if len(df) < COVERAGE_MIN_ROWS:
        notify.send_bot1(notify.header_system(
            f"적재 커버리지 경고: {year}_{reprt_code} {len(df)}행 "
            f"< 하한 {COVERAGE_MIN_ROWS}행 (유니버스 {len(corp_codes)}) — "
            f"빈/부분적재 의심, --recollect 실행 권장"))
    return len(df)


def repair_missing(year: int, reprt_code: str, tickers: list[str]) -> None:
    """이미 업로드된 보고서 SSOT에서 특정 종목만 빠졌을 때 안전하게 보강한다.

    collect()는 완료 후 체크포인트 rows를 비우므로, 그 상태에서 일부 종목만
    다시 collect()하면 업로드 시 전체 파일이 새로 수집한 소수 행으로
    덮어써져 기존 데이터가 유실된다. repair_missing은 기존 parquet 전체를
    읽어 대상 종목의 행만 교체/추가한 뒤 병합본을 재업로드해 이를 피한다.
    """
    path = f"financials/{year}_{reprt_code}.parquet"
    df = storage.read_parquet(path)
    if df is None:
        raise RuntimeError(f"[repair] {path} 없음 — 먼저 전체 적재가 필요합니다")

    corp_codes = _corp_codes()
    new_rows = []
    for t in tickers:
        corp = corp_codes.get(t)
        if corp is None:
            print(f"[repair] {t}: corp_code 없음 — 스킵")
            continue
        rows = dart.get_financials(corp, year, reprt_code)
        if not rows:
            print(f"[repair] {t}: DART 응답에 재무 행 없음 — 스킵(미공시 가능)")
            continue
        fin = dart.normalize_financials(rows)
        new_rows.append(_row(t, corp, fin))
        print(f"[repair] {t}: 수집 완료")

    if not new_rows:
        print("[repair] 추가/갱신할 데이터 없음")
        return

    new_df = pd.DataFrame(new_rows, columns=FIN_COLUMNS)
    fixed = {r["ticker"] for r in new_rows}
    merged = pd.concat([df[~df["ticker"].isin(fixed)], new_df], ignore_index=True)
    storage.upload_parquet(merged, path)
    print(f"[repair] {path} 갱신 완료 (총 {len(merged)}행, 신규/갱신 {len(new_rows)}건)")


def recollect_missing(year: int, reprt_code: str) -> None:
    """이미 업로드된 보고서 SSOT에서 누락된 종목을 자동 탐지해 재수집한다.

    전수 적재는 개별 종목이 일시 오류를 만나면 그 보고서에서 영구 제외되는
    구조라(재개 시 done 처리됨), 네트워크가 불안정하면 소수 종목이 조용히
    빠질 수 있다. 이 모드는 상장 유니버스(corp_codes)와 적재된 parquet을
    비교해 빠진 종목만 다시 수집한다.

    - 안전성: 새로 받은 종목의 행만 교체/추가하고 기존 데이터는 보존한다.
    - 무자료 캐시: DART가 '자료 없음'을 확정한 종목(비상장 전환·미제출 등)은
      recollect_empty_{year}_{reprt}.json에 기록해, 반복 실행 시 재조회를
      건너뛴다(누락≠무자료 구분). 일시 오류로 실패한 종목은 캐시에 넣지
      않으므로 다음 실행에서 다시 시도된다.
    - 재개: 매 청크마다 병합·업로드하고 누락 집합을 현재 parquet 기준으로
      다시 계산하므로, 시간 예산·일일 한도로 중단돼도 재실행하면 남은
      누락분만 이어서 채운다.
    """
    path = f"financials/{year}_{reprt_code}.parquet"
    empty_path = f"checkpoints/recollect_empty_{year}_{reprt_code}.json"
    df = storage.read_parquet(path)
    if df is None:
        raise RuntimeError(f"[recollect] {path} 없음 — 먼저 전체 적재가 필요합니다")

    corp_codes = _corp_codes()
    present = set(df["ticker"].astype(str))
    empty = set(storage.load_json(empty_path) or [])
    missing = [(t, corp_codes[t]) for t in sorted(corp_codes)
               if t not in present and t not in empty]
    print(f"[recollect] {year}_{reprt_code}: 유니버스 {len(corp_codes)}, "
          f"적재 {len(present)}, 무자료 캐시 {len(empty)}, 재수집 대상 {len(missing)}")
    if not missing:
        notify.send_bot1(notify.header_system(
            f"누락 재수집: {year}_{reprt_code} — 재수집 대상 없음 (총 {len(present)}종목)"))
        return

    added = 0
    CHUNK = 200
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i in range(0, len(missing), CHUNK):
            if not _time_left():
                print("[recollect] 시간 예산 소진 — 재실행 시 남은 누락분 이어서 재수집")
                break
            chunk = missing[i:i + CHUNK]
            results = ex.map(lambda tc: _fetch_one(tc[0], tc[1], year, reprt_code),
                             chunk)
            new_rows, new_empty, daily_limit = [], [], False
            for ticker, corp, fin, err in results:
                if err is not None:
                    if "daily limit" in str(err):
                        daily_limit = True
                    else:
                        print(f"[recollect] {ticker} failed: {err}")
                    continue                 # 일시 오류 → 캐시 안 함(다음 실행 재시도)
                if fin is not None:
                    new_rows.append(_row(ticker, corp, fin))
                else:
                    new_empty.append(ticker)   # DART 무자료 확정 → 캐시
            if new_rows:
                fixed = {r["ticker"] for r in new_rows}
                df = pd.concat(
                    [df[~df["ticker"].isin(fixed)],
                     pd.DataFrame(new_rows, columns=FIN_COLUMNS)], ignore_index=True)
                storage.upload_parquet(df, path)
                added += len(new_rows)
            if new_empty:
                empty |= set(new_empty)
                storage.save_json(sorted(empty), empty_path)
            print(f"[recollect] progress {min(i + CHUNK, len(missing))}/{len(missing)} "
                  f"(추가 누적 {added}, 무자료 캐시 {len(empty)})")
            if daily_limit:
                notify.notify_failure(
                    "run_quarterly",
                    f"DART 일일 한도 초과(recollect {year}_{reprt_code}) — 여기까지 "
                    f"저장됨. 내일 재실행하면 남은 누락분 이어서 재수집")
                sys.exit(0)

    notify.send_bot1(notify.header_system(
        f"누락 재수집 완료: {year}_{reprt_code} — {added}종목 추가, 총 {len(df)}종목 적재"))


def _pause_for_continuation() -> int:
    """시간 예산 소진 — 마커를 남기고 정상 종료(워크플로가 새 런 재발행)."""
    with open(CONTINUE_MARKER, "w") as fh:
        fh.write("time budget reached")
    print("[quarterly] paused — workflow will re-dispatch to continue")
    return 0


def main() -> int:
    year = int(sys.argv[1])
    reprt = sys.argv[2] if len(sys.argv) > 2 else dart.REPRT_ANNUAL
    mode = sys.argv[3] if len(sys.argv) > 3 else ""
    if mode == "--repair":
        repair_missing(year, reprt, sys.argv[4:])
        return 0
    if mode == "--recollect":
        recollect_missing(year, reprt)
        return 0
    try:
        counts: dict[str, int] = {}
        n = collect(year, reprt)
        if n is None:
            return _pause_for_continuation()
        counts[f"{year}_{reprt}"] = n
        if reprt == dart.REPRT_ANNUAL:
            # 다년 지표용 과거 사업보고서 백필 (없는 연도만)
            for y in range(year - 5, year):
                if not storage.exists(f"financials/{y}_{dart.REPRT_ANNUAL}.parquet"):
                    print(f"[quarterly] backfilling {y} annual")
                    n = collect(y, dart.REPRT_ANNUAL)
                    if n is None:
                        return _pause_for_continuation()
                    counts[f"{y}_{dart.REPRT_ANNUAL}"] = n
        else:
            # 분기/반기: TTM 계산에 직전 연간 + 전년 동기 보고서가 필요
            for y2, r2 in ((year - 1, dart.REPRT_ANNUAL), (year - 1, reprt)):
                if not storage.exists(f"financials/{y2}_{r2}.parquet"):
                    print(f"[quarterly] backfilling {y2}_{r2} for TTM")
                    n = collect(y2, r2)
                    if n is None:
                        return _pause_for_continuation()
                    counts[f"{y2}_{r2}"] = n
        summary = ", ".join(f"{k} {v:,}종목" for k, v in counts.items())
        notify.send_bot1(notify.header_system(f"분기 적재 완료: {summary}"))
        return 0
    except SystemExit:
        raise
    except Exception:
        notify.notify_failure("run_quarterly", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
