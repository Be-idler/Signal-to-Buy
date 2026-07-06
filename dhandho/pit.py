"""Point-in-Time 데이터 로더 (애드온2 §3) — 기준일 D에 알 수 있었던 정보만 사용.

룩어헤드 금지 근사 규칙: SSOT에 접수일(rcept_dt)이 없으므로 **법정 제출기한이
D 이전에 지난 보고서만** 사용한다(기한 내 제출 의무 → 접수일 ≤ 기한 ≤ D 보장).
기한 전에 조기 제출된 보고서를 놓칠 수 있으나 룩어헤드는 발생하지 않는다(보수적).
리포트에 "재무 as-of"를 명기한다.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from dhandho import krx, metrics, storage

REPRT_ANNUAL, REPRT_HALF, REPRT_Q1, REPRT_Q3 = "11011", "11012", "11013", "11014"
REPRT_NAME = {REPRT_ANNUAL: "사업보고서", REPRT_HALF: "반기보고서",
              REPRT_Q1: "1분기보고서", REPRT_Q3: "3분기보고서"}


def statutory_deadline(year: int, reprt: str) -> dt.date:
    """12월 결산 기준 법정 제출기한 (v2 명세서 §2 수집 캘린더)."""
    return {
        REPRT_ANNUAL: dt.date(year + 1, 3, 31),   # 연도 + 90일
        REPRT_Q1: dt.date(year, 5, 15),           # 분기 + 45일
        REPRT_HALF: dt.date(year, 8, 14),
        REPRT_Q3: dt.date(year, 11, 14),
    }[reprt]


def available_reports(d: dt.date, lookback_years: int = 8) -> list[tuple[int, str]]:
    """기준일 d에 확실히 공시되어 있던 보고서 목록 — 기한 최신순."""
    out: list[tuple[int, str, dt.date]] = []
    for year in range(d.year - lookback_years, d.year + 1):
        for reprt in (REPRT_ANNUAL, REPRT_Q1, REPRT_HALF, REPRT_Q3):
            deadline = statutory_deadline(year, reprt)
            if deadline <= d:
                out.append((year, reprt, deadline))
    out.sort(key=lambda x: x[2], reverse=True)
    return [(y, r) for y, r, _ in out]


def _df_to_fins(df) -> dict[str, dict]:
    fins: dict[str, dict] = {}
    if df is None:
        return fins
    for _, row in df.iterrows():
        d = row.to_dict()
        raw = d.get("flags")
        d["flags"] = raw.split(";") if isinstance(raw, str) and raw else []
        d = {k: (None if (not isinstance(v, list) and pd.isna(v)) else v)
             for k, v in d.items()}
        fins[d["ticker"]] = d
    return fins


def resolve_basis_date(date_str: str | None,
                       today: dt.date | None = None) -> tuple[str, bool]:
    """기준일 확정: None→최근 거래일, 휴장일→직전 거래일 보정.

    반환: (YYYYMMDD, corrected)
    """
    today = today or dt.date.today()
    if date_str is None:
        return krx.recent_trading_days(today, 1)[-1], False
    d = dt.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    trd = krx.recent_trading_days(d, 1)[-1]
    return trd, trd != date_str


def load_prices(basis: str) -> dict[str, dict]:
    """기준일 스냅샷: Drive 캐시 우선, 없으면 KRX 조회 후 적재 (§3)."""
    df = storage.read_parquet(f"prices/eod_{basis}.parquet")
    if df is not None:
        return {r["ticker"]: r for r in df.to_dict("records")}
    snapshot = krx.get_market_snapshot(basis)
    if snapshot:
        storage.upload_parquet(pd.DataFrame(snapshot), f"prices/eod_{basis}.parquet")
    return {r["ticker"]: r for r in snapshot}


def load_financials_asof(
    basis: str, require_ticker: str | None = None,
) -> tuple[dict[str, dict], dict[str, list[dict]], str]:
    """기준일 시점 재무 SSOT (+TTM 변환) 로드.

    반환: (fin_by_ticker, history_by_ticker, as_of_label)

    require_ticker 지정 시(단일 종목 질의): 최신 보고서 파일에 해당 종목이 아직
    적재되지 않았거나(지연 공시·부분 적재) 없으면, 그 종목을 포함하는 가장 최신
    보고서까지 거슬러 내려간다 — 최신 파일 하나에 종목이 없다고 질의를 통째로
    실패시키지 않기 위함(룩어헤드는 여전히 발생하지 않음). 대체 시 as_of에 명기.
    """
    d = dt.date(int(basis[:4]), int(basis[4:6]), int(basis[6:8]))
    latest_df, latest_year, latest_reprt = None, None, None
    skipped_newer: str | None = None          # 종목이 없어 건너뛴 최신 보고서
    for year, reprt in available_reports(d):
        path = f"financials/{year}_{reprt}.parquet"
        if not storage.exists(path):
            continue
        df = storage.read_parquet(path)
        if require_ticker is not None and require_ticker not in set(df["ticker"]):
            if skipped_newer is None:
                skipped_newer = f"{year} {REPRT_NAME[reprt]}"
            continue
        latest_df, latest_year, latest_reprt = df, year, reprt
        break
    if latest_df is None:
        raise RuntimeError(f"기준일 {basis} 이전에 공시된 재무 SSOT가 저장소에 없습니다")

    fins = _df_to_fins(latest_df)
    as_of = (f"{latest_year} {REPRT_NAME[latest_reprt]}"
             f"(법정기한 {statutory_deadline(latest_year, latest_reprt).isoformat()} 기준 추정)")
    if skipped_newer is not None:
        as_of += f" · {skipped_newer}에는 미반영(직전 보고서 기준)"

    if latest_reprt != REPRT_ANNUAL:
        prior_annual = _df_to_fins(storage.read_parquet(
            f"financials/{latest_year - 1}_{REPRT_ANNUAL}.parquet"))
        prior_same = _df_to_fins(storage.read_parquet(
            f"financials/{latest_year - 1}_{latest_reprt}.parquet"))
        fins = {t: metrics.build_ttm(f, prior_annual.get(t), prior_same.get(t))
                for t, f in fins.items()}
        as_of += " · 손익 TTM 변환"

    history: dict[str, list[dict]] = {t: [] for t in fins}
    annual_years = sorted({y for y, r in available_reports(d) if r == REPRT_ANNUAL})
    for y in annual_years[-6:]:
        if (latest_year, latest_reprt) == (y, REPRT_ANNUAL):
            continue
        df = storage.read_parquet(f"financials/{y}_{REPRT_ANNUAL}.parquet")
        for t, fin in _df_to_fins(df).items():
            if t in history:
                history[t].append(fin)
    return fins, history, as_of
