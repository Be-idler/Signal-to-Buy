"""load_financials_asof 단일종목 폴백 테스트.

최신 보고서 파일에 질의 종목이 아직 없으면(지연 공시·부분 적재),
그 종목을 포함하는 가장 최신 보고서까지 거슬러 내려간다.
룩어헤드는 발생하지 않으며(과거 보고서만), 대체 사실을 as_of에 명기한다.
"""
import datetime as dt

import pandas as pd

from dhandho import pit


def _df(*tickers):
    """최소 재무 SSOT 프레임 — ticker와 결산 항목 일부."""
    rows = [dict(ticker=t, corp_code=f"c{t}", name=t,
                 total_assets=2000., total_liabilities=800., total_equity=1200.,
                 current_assets=900., current_liabilities=400.,
                 cash_and_equivalents=300., short_term_investments=0.,
                 total_borrowings=200., ppe=600., revenue=1000.,
                 operating_income=100., net_income=80.,
                 net_income_controlling=80., flags="")
            for t in tickers]
    return pd.DataFrame(rows)


def _install(monkeypatch, store: dict):
    # 존재하지 않는 과거 연간/동기 파일은 빈 프레임으로 관대하게 처리
    # (TTM·history 로딩 경로가 exists() 없이 read_parquet을 호출하므로).
    monkeypatch.setattr(pit.storage, "exists", lambda p: p in store)
    monkeypatch.setattr(pit.storage, "read_parquet",
                        lambda p: store.get(p, _df()))


def test_falls_back_to_older_report_containing_ticker(monkeypatch):
    basis = "20260703"                       # 최신 가용: 2026 1분기(11013)
    store = {
        "financials/2026_11013.parquet": _df("005930"),          # 포스코 없음
        "financials/2025_11011.parquet": _df("005930", "047050"),  # 연간엔 있음
    }
    _install(monkeypatch, store)
    fins, _hist, as_of = pit.load_financials_asof(basis, require_ticker="047050")
    assert "047050" in fins
    assert "2025 사업보고서" in as_of                    # 직전 연간 보고서 채택
    assert "미반영" in as_of                             # 대체 사실 명기


def test_no_fallback_when_ticker_in_latest(monkeypatch):
    basis = "20260703"
    store = {"financials/2026_11013.parquet": _df("047050", "005930"),
             "financials/2025_11011.parquet": _df("047050"),
             "financials/2024_11011.parquet": _df("047050")}
    _install(monkeypatch, store)
    fins, _hist, as_of = pit.load_financials_asof(basis, require_ticker="047050")
    assert "047050" in fins
    assert "1분기보고서" in as_of and "미반영" not in as_of


def test_universe_load_unaffected_without_require_ticker(monkeypatch):
    basis = "20260703"
    store = {"financials/2026_11013.parquet": _df("005930"),
             "financials/2025_11011.parquet": _df("005930", "047050")}
    _install(monkeypatch, store)
    fins, _hist, as_of = pit.load_financials_asof(basis)   # 트리거A/B 경로
    assert set(fins) == {"005930"}                          # 최신 보고서 그대로
    assert "미반영" not in as_of
