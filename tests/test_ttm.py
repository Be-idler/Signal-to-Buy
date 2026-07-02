"""TTM 변환 테스트 — 보고서별 손익 기준 불일치(연간 vs 기간) 보정."""
from dhandho.metrics import build_ttm, compute_derived


def _bs(**over):
    base = dict(total_assets=2000., total_liabilities=800., total_equity=1200.,
                current_assets=900., current_liabilities=400.,
                cash_and_equivalents=300., short_term_investments=50.,
                total_borrowings=200., ppe=600., flags=[])
    base.update(over)
    return base


def test_ttm_formula():
    # 직전 연간 매출 1000, 당기(반기) 누적 600, 전년 동기 누적 450 → TTM = 1150
    interim = _bs(revenue=600., revenue_cum=600., operating_income=60.,
                  operating_income_cum=60.)
    annual = {"revenue": 1000., "operating_income": 100.}
    prior_same = {"revenue": 450., "revenue_cum": 450., "operating_income": 45.,
                  "operating_income_cum": 45.}
    out = build_ttm(interim, annual, prior_same)
    assert out["revenue"] == 1150.
    assert out["operating_income"] == 115.
    assert out["flow_basis"] == "TTM"


def test_bs_items_kept_from_interim():
    interim = _bs(revenue=300., cash_and_equivalents=777.)
    out = build_ttm(interim, {"revenue": 1000.}, {"revenue": 250.})
    assert out["cash_and_equivalents"] == 777.       # 시점 잔액은 최신 보고서 그대로
    assert out["revenue"] == 1050.


def test_missing_prior_same_falls_back_to_annual():
    interim = _bs(revenue=300.)
    out = build_ttm(interim, {"revenue": 1000.}, None)
    assert out["revenue"] == 1000.                   # 3개월치 그대로 쓰지 않는다
    assert "revenue_ttm_fallback_annual" in out["flags"]


def test_missing_everything_flow_becomes_none():
    interim = _bs(revenue=300.)
    out = build_ttm(interim, None, None)
    assert out["revenue"] is None                    # 기간 불일치 값 사용 금지
    assert "revenue_flow_basis_mismatch" in out["flags"]


def test_q1_distortion_prevented_end_to_end():
    """1분기 3개월 이익에 연간 시총을 붙이면 PER 4배 왜곡 — TTM으로 방지."""
    q1 = _bs(revenue=250., net_income=25., net_income_controlling=25.,
             operating_income=30., cfo=40., capex=10.)
    annual = {"revenue": 1000., "net_income": 100., "net_income_controlling": 100.,
              "operating_income": 120., "cfo": 160., "capex": 40.,
              "interest_expense": 8., "gross_profit": 300., "depreciation": 20.}
    prior_q1 = {"revenue": 240., "net_income": 24., "net_income_controlling": 24.,
                "operating_income": 28., "cfo": 38., "capex": 9.,
                "interest_expense": 2., "gross_profit": 70., "depreciation": 5.}
    ttm = build_ttm(q1, annual, prior_q1)
    m = compute_derived(ttm, mktcap=1000.)
    # TTM 순이익 = 100 + 25 − 24 = 101 → PER ≈ 9.9 (3개월치였다면 PER 40)
    assert 9.5 < m["per"] < 10.5
