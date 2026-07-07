import math

from dhandho import frameworks
from dhandho.frameworks import (rank_magic_formula, rank_universe, score_buffett,
                                score_dhandho, score_dhandho_quant, score_ltgg,
                                score_outsiders)


def _metrics(**over):
    """정량 지표가 모두 우량한 합성 종목."""
    base = dict(
        flags=[], mktcap=1.0e11,             # 1,000억 — 마법공식 최소 시총 통과
        net_cash_to_mktcap=0.50, ncav_to_mktcap=1.20,
        interest_coverage=float("inf"), debt_ratio=0.30, fcf_negative_years=0,
        revenue_cagr_5y=0.08, revenue_cagr_3y=0.08, op_income_slope=0.05,
        revenue_slope=0.05, total_equity=1200.0,
        op_income_history=[80.0, 90.0, 100.0, 110.0, 130.0],
        revenue_history=[600.0, 700.0, 800.0, 900.0, 1000.0],
        drawdown_52w=-0.35, ev_ebit=5.0, per=6.0, pbr=0.8, psr=0.5,
        earnings_yield=0.15, roc_greenblatt=0.30, roic=0.18, roiic=0.20,
        op_margin_cv=0.10, gpa=0.40, gross_margin_slope=0.02,
        roe_mean=0.16, roe_stdev=0.02, fcf_margin=0.12, fcf_cagr_5y=0.12,
        operating_income=130.0, revenue=1000.0, net_income=100.0,
    )
    base.update(over)
    return base


# ─────────────────────────────────── §13.1 A 섹션 매핑

def test_a_section_perfect_downside():
    q = score_dhandho_quant(_metrics())
    subs = q["A"]["subscores"]
    assert subs["A1"]["score"] == 5.0        # net_cash/시총 0.5 ≥ 0.40
    assert subs["A2"]["score"] == 5.0        # NCAV/시총 1.2 ≥ 1.0
    assert subs["A3"]["score"] == 5.0        # 무차입(inf) + 부채비율<0.5 보정 후 클립
    assert subs["A4"]["score"] == 5.0        # FCF 음수 0년
    assert q["A_quant"] == 5.0


def test_a1_boundary_mapping():
    for value, expected in [(0.41, 5), (0.30, 4), (0.15, 3), (0.05, 2), (-0.1, 1)]:
        q = score_dhandho_quant(_metrics(net_cash_to_mktcap=value))
        assert q["A"]["subscores"]["A1"]["score"] == expected, value


def test_a3_debt_ratio_penalty():
    q = score_dhandho_quant(_metrics(interest_coverage=4.0, debt_ratio=2.5))
    assert q["A"]["subscores"]["A3"]["score"] == 3.0   # 4점 − 1.0 (부채비율>2)


def test_none_input_capped_at_2_5_with_flag():
    q = score_dhandho_quant(_metrics(net_cash_to_mktcap=None))
    assert q["A"]["subscores"]["A1"]["score"] == 2.5
    assert "A1_insufficient" in q["A"]["flags"]


# ─────────────────────────────────── §13.3/13.4 D·게이트

def test_d_quant_caps_qualitative_at_2_5():
    q = score_dhandho_quant(_metrics())
    subs = q["D"]["subscores"]
    assert subs["D2"]["score"] == 2.5        # 정성 미확보 → 보수적 캡
    assert subs["D3"]["score"] == 2.5
    assert subs["D1"]["score"] == 5.0        # 매출·이익 모두 성장
    # D_quant = .3*5 + .3*2.5 + .2*2.5 + .2*2.5 = 3.25
    assert math.isclose(q["D_quant"], 3.25)


def test_d1_structural_decline():
    q = score_dhandho_quant(_metrics(revenue_cagr_5y=-0.10, op_income_slope=-0.10))
    assert q["D"]["subscores"]["D1"]["score"] == 1.0


def test_d4_full_impairment():
    q = score_dhandho_quant(_metrics(total_equity=-10.0))
    assert q["D"]["subscores"]["D4"]["score"] == 1.0


def test_d4_audit_going_concern():
    q = score_dhandho_quant(_metrics(), audit={"opinion": "적정",
                                               "going_concern_doubt": True})
    assert q["D"]["subscores"]["D4"]["score"] == 1.0


# ─────────────────────────────────── C 섹션 (peers·폴백)

def _peers():
    return {"ev_ebit": [4, 6, 8, 10, 12, 15], "per": [5, 8, 10, 12, 15, 20],
            "pbr": [0.5, 0.8, 1.0, 1.5, 2.0, 3.0], "psr": [0.3, 0.5, 1, 2, 3, 4]}


def test_c1_uses_peer_percentile():
    q = score_dhandho_quant(_metrics(), peers=_peers())
    assert q["C"]["subscores"]["C1"]["score"] > 3.5    # 저평가(싼 쪽) → 고득점


def test_c1_deficit_falls_back_to_pbr_psr():
    m = _metrics(ev_ebit=None, per=None)               # 적자 → 멀티플 무효
    q = score_dhandho_quant(m, peers=_peers())
    assert "C1_pbr_psr_fallback" in q["C"]["flags"]
    assert q["C"]["subscores"]["C1"]["score"] > 2.5


def test_c4_sweet_spot_drawdown():
    q = score_dhandho_quant(_metrics(drawdown_52w=-0.35))
    assert q["C"]["subscores"]["C4"]["score"] == 4.5
    q2 = score_dhandho_quant(_metrics(drawdown_52w=-0.05))
    assert q2["C"]["subscores"]["C4"]["score"] == 2.0


# ─────────────────────────────────── 단도 최종 (qual 반영)

def test_full_dhandho_with_qual():
    # v1 그라운딩 항목: B4·D2·D3·F1·F3
    qual = {k: {"score": 5.0, "basis": [{"tier": 1, "source": "rcept", "date": "20260101"}]}
            for k in ("B4", "D2", "D3", "F1", "F3")}
    r = score_dhandho(_metrics(), qual=qual, peers=_peers(),
                      audit={"opinion": "적정"},
                      disclosures=[{"report_nm": "주요사항보고서(자기주식취득결정)"}],
                      shareholder={"dividend_paid": True, "retired_any": True,
                                   "unretired_treasury": False},
                      insider=[{"change": 1000.0}])
    assert r["sections"]["D"]["subscores"]["D2"]["score"] == 5.0
    assert r["sections"]["A"]["total"] == 5.0
    assert r["sections"]["E"]["subscores"]["E1"]["score"] == 4.5   # 배당+소각
    assert r["sections"]["F"]["subscores"]["F2"]["score"] == 4.5   # 내부자 순매수
    assert r["total"] > 4.0
    assert "E1_heuristic" in r["flags"]                # E1 세부 매핑은 v1 코드 대조 전 근사


def test_qual_without_basis_capped():
    qual = {"D2": {"score": 5.0, "basis": []}}         # 근거 없음(grounded=false) → 2.5 상한
    r = score_dhandho(_metrics(), qual=qual)
    assert r["sections"]["D"]["subscores"]["D2"]["score"] == 2.5


def test_e_section_hoarded_treasury_penalized():
    r = score_dhandho(_metrics(),
                      shareholder={"dividend_paid": True, "retired_any": False,
                                   "unretired_treasury": True})
    assert r["sections"]["E"]["subscores"]["E1"]["score"] == 3.0   # 3.5 − 0.5


def test_f2_insider_net_selling():
    r = score_dhandho(_metrics(), insider=[{"change": -500.0}, {"change": 100.0}])
    assert r["sections"]["F"]["subscores"]["F2"]["score"] == 1.5


# ─────────────────────────────────── LTGG / 아웃사이더 / 버핏

def test_ltgg_gates_and_caveat():
    r = score_ltgg(_metrics(revenue_cagr_5y=0.25, roiic=0.20))
    assert r["gates"]["L1"] and r["gates"]["L2"]
    assert "framework_target_mismatch_caveat" in r["flags"]


def test_ltgg_capital_impairment_excluded():
    r = score_ltgg(_metrics(total_equity=-5.0))
    assert r["grade"] == "제외"
    assert "capital_impairment_excluded" in r["flags"]


def test_outsiders_buyback_retirement_matters():
    retired = score_outsiders(_metrics(), buyback={"bought": True, "retired_ratio": 0.9})
    hoarded = score_outsiders(_metrics(), buyback={"bought": True, "retired_ratio": 0.0})
    # 소각 반영: B4(소각) 차이가 OB(주당 가치) 섹션에 반영 (의무이행=중립 3.0, 미소각=2.0)
    assert retired["subscores"]["OB"]["score"] > hoarded["subscores"]["OB"]["score"]
    assert retired["total"] > hoarded["total"]


def test_outsiders_dual_scoring_and_spread():
    # 소각 이력 우수 + 저가 매입 → E(코리아 디스카운트 해소도) 양호 케이스
    r = score_outsiders(_metrics(cfo_to_ni=1.1),
                        buyback={"bought": True, "retired_ratio": 0.9,
                                 "pre_mandate_retired": True})
    assert "total_reform" in r and "spread" in r and r["spread_note"]
    # 미적용 = E 가중치를 A/B/C/D에 재배분 — 두 총점 모두 산출됨
    assert isinstance(r["total_reform"], float) and isinstance(r["total"], float)
    assert r["grade_reform"] in ("적극 검토", "분할 검토", "관심종목 편입", "보류", "제외")


def test_outsiders_gates_block_buy_without_management_evidence():
    # 경영진(OC) 근거 미확보 → 2.5 캡 → C게이트 미통과 → 상한 보류
    r = score_outsiders(_metrics(cfo_to_ni=1.3),
                        buyback={"bought": True, "retired_ratio": 0.9})
    assert not r["gates"]["경영진(C≥3.0)"]
    assert r["grade"] in ("보류", "제외")


def test_outsiders_data_gate_caps_at_watch():
    # 다년 FCF 미확보(G4) → 상한 관심종목 편입
    r = score_outsiders(_metrics(fcf_cagr_5y=None, cfo_to_ni=1.3, roiic=0.20),
                        insider=[{"change": 1000.0}],
                        buyback={"bought": True, "retired_ratio": 0.9})
    assert not r["gates"]["데이터(다년 FCF)"]
    assert r["grade"] in ("관심종목 편입", "보류", "제외")


def test_buffett_tunneling_hard_exclusion():
    qual = {"G1": {"tunneling_confirmed": True}}
    r = score_buffett(_metrics(), qual=qual)
    assert r["grade"] == "제외"
    assert "tunneling_hard_excluded" in r["flags"]


def test_buffett_moat_capped_without_qualitative_type():
    # 정량 지표 우수해도 해자 유형 미특정 → BB ≤ 3.0 (3점 초과 금지)
    r = score_buffett(_metrics())
    assert r["subscores"]["BB"]["score"] <= 3.0
    assert "BB_type_capped" in r["flags"]
    assert not r["gates"]["해자(B≥3.5)"]          # → 매수급 불가


def test_buffett_margin_of_safety_from_owner_earnings():
    # 오너어닝스 100, 시총 500 → IV = 100×1.05/0.075 = 1400 → 할인 64% → BE=5
    m = _metrics(owner_earnings=100.0, owner_earnings_ratio=1.0,
                 mktcap=500.0, eps_cagr_5y=0.10, ncav_to_mktcap=0.2)
    r = score_buffett(m)
    assert r["mos_discount"] is not None and r["mos_discount"] > 0.5
    assert r["subscores"]["BE"]["score"] == 5.0
    assert "BE_dcf_simplified" in r["flags"]


def test_buffett_no_discount_scores_low_mos():
    # 시총이 내재가치보다 큼 → 할인 없음 → BE=1, 안전마진 게이트 미통과
    m = _metrics(owner_earnings=50.0, owner_earnings_ratio=0.5,
                 mktcap=1.0e11, eps_cagr_5y=0.0, ncav_to_mktcap=0.2)
    r = score_buffett(m)
    assert r["subscores"]["BE"]["score"] == 1.0
    assert not r["gates"]["안전마진(E≥3.0)"]


def test_buffett_leverage_dependent_roe_discounted():
    m = _metrics(roe_mean=0.18, debt_ratio=2.0, owner_earnings_ratio=1.0,
                 net_debt_to_ebitda=3.0, interest_coverage=6.0)
    r = score_buffett(m)
    assert "BD_leveraged_roe" in r["flags"]


# ─────────────────────────────────── 마법공식

def test_magic_formula_ranking_and_exclusions():
    universe = {
        "GOOD": _metrics(earnings_yield=0.20, roc_greenblatt=0.40),
        "MID": _metrics(earnings_yield=0.10, roc_greenblatt=0.15),
        "BAD": _metrics(earnings_yield=0.02, roc_greenblatt=0.03),
        "DEFICIT": _metrics(operating_income=-10.0),
        "TINY": _metrics(mktcap=1.0),
        "BANK": _metrics(),
    }
    info = {"BANK": {"name": "OO은행"}}
    r = rank_magic_formula(universe, info)
    assert r["GOOD"]["rank"] == 1 and r["GOOD"]["score"] == 5.0
    assert r["BAD"]["rank"] == 3
    assert r["DEFICIT"]["excluded"] == "ebit_nonpositive"
    assert r["TINY"]["excluded"] == "mktcap_below_min"
    assert r["BANK"]["excluded"] == "sector_excluded"
    assert "basket_tool_not_single_buy" in r["GOOD"]["flags"]


def test_rank_universe_dispatch():
    universe = {"X": _metrics(), "Y": _metrics(roic=0.02, fcf_cagr_5y=-0.05)}
    ranked = rank_universe(universe, "buffett")
    assert ranked[0][0] == "X"
    ranked_mf = rank_universe(universe, "magic_formula")
    assert ranked_mf[0][1]["score"] is not None
    try:
        rank_universe(universe, "nope")
        assert False
    except ValueError:
        pass
