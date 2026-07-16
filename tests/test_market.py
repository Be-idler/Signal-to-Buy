"""시장 요인 분해 테스트 — 지수 동반 하락 vs 개별 요인."""
from dhandho import market


def _rows(pairs):
    """[(ticker, mktcap, close)] → 스냅샷 행 리스트."""
    return [{"ticker": t, "mktcap": mc, "close": c} for t, mc, c in pairs]


def test_market_level_and_universe():
    rows = _rows([("A", 100.0, 10.0), ("B", 200.0, 20.0), ("C", None, 5.0)])
    assert market.market_level(rows) == 300.0            # None 시총 제외
    assert market.market_level(rows, universe={"A"}) == 100.0


def test_drawdown():
    assert market.drawdown([100, 120, 90]) == 90 / 120 - 1
    assert market.drawdown([100]) is None


def test_beta_recovers_slope():
    # 종목 수익률 = 2 × 시장 수익률 → 베타 ≈ 2
    mkt = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.01, 0.02, -0.02, 0.01, -0.01]
    stock = [2 * x for x in mkt]
    b = market.beta(stock, mkt)
    assert b is not None and abs(b - 2.0) < 1e-6


def test_beta_insufficient_data_none():
    assert market.beta([0.01, 0.02], [0.01, 0.02]) is None


def test_two_point_change_common_universe():
    start = _rows([("A", 100.0, 10.0), ("B", 100.0, 10.0)])
    end = _rows([("A", 80.0, 8.0), ("B", 90.0, 9.0), ("C", 999.0, 1.0)])  # C 신규 무시
    chg = market.two_point_change(start, end)
    assert abs(chg - (170.0 / 200.0 - 1.0)) < 1e-9      # -15%


# ─────────────────────────── assess_decline (핵심)

def test_market_driven_decline():
    # 종목 -30%, 지수 -20%, β=1.2 → 기대 -24% → 기여 0.8 → 시장 요인
    r = market.assess_decline(-0.30, -0.20, 1.2)
    assert r["verdict"] == "market"
    assert "지수 동반" in r["note"] and "시장 요인" in r["note"]


def test_idiosyncratic_decline():
    # 종목 -30%, 지수 -3% → 기여 0.1 → 개별 요인
    r = market.assess_decline(-0.30, -0.03, 1.0)
    assert r["verdict"] == "idiosyncratic"
    assert "초과 하락" in r["note"]


def test_mixed_decline():
    r = market.assess_decline(-0.30, -0.12, 1.0)         # 기여 0.4
    assert r["verdict"] == "mixed"


def test_market_flat_means_idiosyncratic():
    r = market.assess_decline(-0.30, 0.005, 1.0)
    assert r["verdict"] == "idiosyncratic"
    assert "거의 하락하지 않음" in r["note"]


def test_small_decline_not_reported():
    assert market.assess_decline(-0.02, -0.15, 1.0)["note"] is None   # 급락 아님


def test_missing_market_change_silent():
    assert market.assess_decline(-0.30, None, 1.0)["note"] is None


def test_beta_none_uses_unit_assumption():
    r = market.assess_decline(-0.25, -0.20, None)        # β≈1 → 기여 0.8
    assert r["verdict"] == "market"
    assert "β≈1 가정" in r["note"]


def test_build_series_immune_to_share_count_changes():
    # A가 증자로 시총 2배(가격 불변) → 시총합 방식은 지수 급등으로 오인,
    # 가격수익률 체인링크는 지수 불변이어야 한다
    snaps = {
        "20260601": _rows([("A", 1000.0, 100.0), ("B", 500.0, 50.0)]),
        "20260602": _rows([("A", 2000.0, 100.0), ("B", 500.0, 50.0)]),  # A 증자
        "20260603": _rows([("A", 2000.0, 100.0), ("B", 500.0, 50.0)]),
    }
    _, levels = market.build_series(snaps)
    assert levels[0] == 100.0
    assert abs(levels[1] - 100.0) < 1e-9        # 가격 불변 → 지수 불변
    assert abs(levels[2] - 100.0) < 1e-9


def test_build_series_ipo_delisting_do_not_distort():
    # 신규상장 C(대형)와 B 상폐가 껴도 공통 종목 수익률만 반영
    snaps = {
        "20260601": _rows([("A", 1000.0, 100.0), ("B", 500.0, 50.0)]),
        "20260602": _rows([("A", 900.0, 90.0), ("C", 9000.0, 10.0)]),   # B 상폐·C 신규
        "20260603": _rows([("A", 810.0, 81.0), ("C", 9000.0, 10.0)]),
    }
    _, levels = market.build_series(snaps)
    assert abs(levels[1] - 90.0) < 1e-9         # 공통(A)만: -10%
    # 공통(A·C) 시총가중: (900×-10% + 9000×0%) / 9900 ≈ -0.91%
    assert abs(levels[2] - 90.0 * (1 - 90.0 / 9900.0)) < 1e-9


def test_end_to_end_series_from_snapshots():
    # 3거래일 스냅샷: 시장·종목 모두 하락, 종목이 시장과 동조
    snaps = {
        "20260601": _rows([("A", 100.0, 100.0), ("MKT", 1000.0, 100.0)]),
        "20260602": _rows([("A", 90.0, 90.0), ("MKT", 900.0, 90.0)]),
        "20260603": _rows([("A", 80.0, 80.0), ("MKT", 800.0, 80.0)]),
    }
    dates, levels = market.build_series(snaps)
    assert dates == ["20260601", "20260602", "20260603"]
    mkt_dd = market.drawdown(levels)
    srets = market.returns(market.stock_level_series(snaps, "A", dates))
    mrets = market.returns(levels)
    # 완전 동조 → 베타≈1, 시장 요인 판정
    r = market.assess_decline(market.drawdown([100.0, 90.0, 80.0]), mkt_dd,
                              market.beta(srets, mrets) or 1.0)
    assert r["verdict"] == "market"
