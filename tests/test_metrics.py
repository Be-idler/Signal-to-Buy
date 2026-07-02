import math

from dhandho.metrics import compute_derived


def _fin(**over):
    base = dict(
        revenue=1000.0, operating_income=100.0, gross_profit=300.0,
        net_income=80.0, net_income_controlling=75.0,
        total_assets=2000.0, total_liabilities=800.0, total_equity=1200.0,
        equity_controlling=1100.0, current_assets=900.0, current_liabilities=400.0,
        cash_and_equivalents=300.0, short_term_investments=50.0,
        total_borrowings=200.0, ppe=600.0, cfo=150.0, capex=50.0,
        interest_expense=10.0, flags=[],
    )
    base.update(over)
    return base


def test_financial_only_metrics():
    m = compute_derived(_fin())
    assert m["op_margin"] == 0.1
    assert m["fcf"] == 100.0
    assert m["net_cash"] == 150.0            # (300+50) - 200
    assert m["ncav"] == 100.0                # 900 - 800
    assert math.isclose(m["debt_ratio"], 800 / 1200)
    assert m["interest_coverage"] == 10.0
    assert m["roic"] is not None
    # 시총 미주입 → 시총 결합 지표 None + 플래그
    assert m["per"] is None and m["ev_ebit"] is None
    assert "mktcap_missing" in m["flags"]


def test_mktcap_injection_completes_valuation():
    m = compute_derived(_fin(), mktcap=1000.0)
    assert math.isclose(m["per"], 1000 / 75)
    assert math.isclose(m["pbr"], 1000 / 1100)
    assert math.isclose(m["ev"], 1000 + 200 - 350)
    assert math.isclose(m["ev_ebit"], 850 / 100)
    assert math.isclose(m["net_cash_to_mktcap"], 0.15)
    assert math.isclose(m["ncav_to_mktcap"], 0.10)
    assert "mktcap_missing" not in m["flags"]


def test_deficit_company_per_ev_ebit_none():
    m = compute_derived(_fin(operating_income=-50.0, net_income=-30.0,
                             net_income_controlling=-30.0), mktcap=1000.0)
    assert m["per"] is None                  # 적자 → PER 무효
    assert m["ev_ebit"] is None              # EBIT<0 → EV/EBIT 무효
    assert m["pbr"] is not None              # PBR·PSR 폴백 가능
    assert m["psr"] is not None


def test_no_debt_infinite_coverage():
    m = compute_derived(_fin(total_borrowings=0.0))
    assert m["interest_coverage"] == float("inf")


def test_borrowings_missing_coverage_none():
    m = compute_derived(_fin(total_borrowings=None))
    assert m["interest_coverage"] is None
    assert m["net_cash"] is None


def test_capital_impairment_passthrough():
    m = compute_derived(_fin(total_equity=-100.0, equity_controlling=None))
    assert m["total_equity"] == -100.0


def test_history_cagr_and_fcf_years():
    hist = [_fin(revenue=600.0, operating_income=60.0, cfo=-10.0, capex=20.0,
                 total_equity=800.0),
            _fin(revenue=700.0, operating_income=70.0, total_equity=900.0),
            _fin(revenue=800.0, operating_income=80.0, total_equity=1000.0),
            _fin(revenue=900.0, operating_income=90.0, total_equity=1100.0)]
    m = compute_derived(_fin(), history=hist)
    assert math.isclose(m["revenue_cagr_5y"], (1000 / 600) ** 0.25 - 1)
    assert m["fcf_negative_years"] == 1      # 최근 5년 중 첫해 FCF<0
    assert m["op_income_slope"] > 0
    assert m["roiic"] is not None


def test_history_missing_flag():
    m = compute_derived(_fin())
    assert m["revenue_cagr_5y"] is None
    assert "history_missing" in m["flags"]


def test_drawdown():
    m = compute_derived(_fin(), closes=[100.0, 120.0, 90.0, 78.0])
    assert math.isclose(m["drawdown_52w"], 78 / 120 - 1)
