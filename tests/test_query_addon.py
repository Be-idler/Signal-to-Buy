"""애드온2 테스트: 파서·PIT 룩어헤드 금지·비대칭·린치·애크먼·목표가·포맷."""
import datetime as dt

import pytest

from dhandho import asymmetry, pit, query_parser, report_format, target_price
from dhandho.frameworks_ackman import score_ackman
from dhandho.frameworks_lynch import classify, score_lynch

TODAY = dt.date(2026, 7, 4)
UNIVERSE = {"005930": "삼성전자", "000660": "SK하이닉스",
            "005935": "삼성전자우", "126340": "비나텍"}


# ───────────────────────────── query_parser (§1)

def test_parse_code_scheme_date():
    r = query_parser.parse("005930 버핏 20260630", UNIVERSE, TODAY)
    assert r == {"ticker": "005930", "name": "삼성전자", "scheme": "buffett",
                 "scheme_label": "버핏멍거", "date": "20260630"}


def test_parse_name_and_hyphen_date():
    r = query_parser.parse("삼성전자 단도 2026-06-30", UNIVERSE, TODAY)
    assert r["ticker"] == "005930" and r["scheme"] == "dhandho"
    assert r["date"] == "20260630"


def test_parse_date_omitted():
    r = query_parser.parse("비나텍 린치", UNIVERSE, TODAY)
    assert r["ticker"] == "126340" and r["scheme"] == "lynch" and r["date"] is None


def test_scheme_aliases():
    for alias, key in [("파브라이", "dhandho"), ("bg", "ltgg"), ("워런버핏", "buffett"),
                       ("손다이크", "outsiders"), ("peg", "lynch"), ("액티비스트", "ackman")]:
        assert query_parser.parse(f"005930 {alias}", UNIVERSE, TODAY)["scheme"] == key


def test_future_date_rejected():
    with pytest.raises(query_parser.ParseError, match="미래"):
        query_parser.parse("005930 단도 2099-01-01", UNIVERSE, TODAY)


def test_ambiguous_name_asks_back():
    with pytest.raises(query_parser.AmbiguousError):
        query_parser.parse("삼성 버핏", UNIVERSE, TODAY)   # 삼성전자·삼성전자우 부분일치


def test_unknown_scheme_and_usage():
    with pytest.raises(query_parser.ParseError, match="스킴"):
        query_parser.parse("005930 모멘텀", UNIVERSE, TODAY)
    with pytest.raises(query_parser.ParseError):
        query_parser.parse("삼성전자", UNIVERSE, TODAY)


# ───────────────────────────── pit (§3) — 룩어헤드 금지

def test_lookahead_annual_not_available_before_deadline():
    # 2026-03-01: 2025 사업보고서(기한 2026-03-31) 아직 미확정 → 최신은 2025 3분기
    avail = pit.available_reports(dt.date(2026, 3, 1))
    assert (2025, "11011") not in avail
    assert avail[0] == (2025, "11014")


def test_lookahead_annual_available_after_deadline():
    avail = pit.available_reports(dt.date(2026, 4, 1))
    assert avail[0] == (2025, "11011")


def test_lookahead_q1_boundary():
    # 5/15 기한 당일까지는 미확정, 이후 확정
    assert (2026, "11013") not in pit.available_reports(dt.date(2026, 5, 14))
    assert (2026, "11013") in pit.available_reports(dt.date(2026, 5, 15))


# ───────────────────────────── asymmetry (애드온1 §1.4)

def test_asymmetry_net_cash_above_mktcap():
    m = {"mktcap": 100.0, "net_cash": 150.0, "ncav": 120.0, "operating_income": 20.0}
    a = asymmetry.compute(m)
    assert a["negative_risk"] is True                      # 바닥이 현재가 위
    assert "강한 비대칭" in asymmetry.verdict(a["ratio"], True)


def test_asymmetry_full_impairment():
    m = {"mktcap": 100.0, "net_cash": -50.0, "ncav": -80.0, "operating_income": -10.0}
    a = asymmetry.compute(m)
    assert "bottom_negative" in a["flags"] and "upside_unavailable" in a["flags"]
    assert a["ratio"] is None


def test_asymmetry_deficit_high_ncav():
    m = {"mktcap": 100.0, "net_cash": 30.0, "ncav": 90.0, "operating_income": -5.0}
    a = asymmetry.compute(m)
    assert a["bottom_mktcap"] == 90.0 and "NCAV" in a["bottom_basis"]
    assert a["ratio"] is None                              # 업사이드 산출 불가(적자)


# ───────────────────────────── lynch (§4.1)

def test_lynch_categories():
    assert classify({"eps_cagr_5y": 0.25}) == "고성장"
    assert classify({"eps_cagr_5y": 0.15}) == "스톨워트"
    assert classify({"eps_cagr_5y": 0.05}) == "저성장·배당"
    assert classify({"ncav_to_mktcap": 1.2, "eps_cagr_5y": 0.15}) == "자산주"
    assert classify({"op_income_history": [-10.0, 5.0], "eps_cagr_5y": 0.3}) == "턴어라운드"


def test_lynch_peg_scoring_and_deficit_switch():
    good = score_lynch({"per": 8.0, "eps_cagr_5y": 0.20, "debt_ratio": 0.5})
    assert good["peg"] == pytest.approx(0.4) and good["score"] == 5.0
    deficit = score_lynch({"per": None, "eps_cagr_5y": -0.1})
    assert deficit["score"] == 2.5 and "peg_unavailable" in deficit["flags"]


# ───────────────────────────── ackman (§4.2)

def _quality_m():
    return {"fcf_margin": 0.15, "fcf_negative_years": 0, "gpa": 0.4,
            "debt_ratio": 0.3, "roic": 0.18, "per": 6.0}


def test_ackman_no_catalyst_is_conservative_value_trap():
    r = score_ackman(_quality_m(), disclosures=None)
    assert r["catalyst"] == 2.0
    assert "가치함정 위험(촉매 부재)" in r["labels"]
    assert "catalyst_no_evidence" in r["flags"]


def test_ackman_retirement_catalyst_scores_up():
    disc = [{"report_nm": "주요사항보고서(자기주식소각결정)", "rcept_dt": "20260601"}]
    r = score_ackman(_quality_m(), disc)
    assert r["catalyst"] >= 4.0 and r["catalyst_evidence"]


# ───────────────────────────── target_price (§6)

def test_dhandho_entry_band():
    m = {"mktcap": 1000.0, "net_cash": 400.0, "ncav": 600.0, "operating_income": 200.0}
    tp = target_price.compute("dhandho", m, close=10.0, shares=100.0)
    assert "~" in tp["entry"] and tp["assumptions"]        # 바닥 6원 → 6.9~7.8원 밴드
    assert "촉매 없음" in tp["targets"]["6개월"]


def test_dhandho_asymmetry_constraint_binds():
    # 업사이드가 얕으면 비대칭 2:1 제약이 밴드 상단을 지배
    m = {"mktcap": 1000.0, "net_cash": 400.0, "ncav": 600.0, "operating_income": 100.0}
    tp = target_price.compute("dhandho", m, close=10.0, shares=100.0)
    assert "비대칭 2:1 제약이 우선" in tp["entry"]


def test_ltgg_refuses_short_horizon():
    m = {"revenue_cagr_5y": 0.25}
    tp = target_price.compute("ltgg", m, close=100.0, shares=10.0)
    assert "산출하지 않음" in tp["targets"]["6개월"]
    assert "분할매수 밴드" in tp["entry"]


def test_ackman_refuses_without_catalyst():
    tp = target_price.compute("ackman", {"mktcap": 100.0, "operating_income": 10.0,
                                         "ncav": 50.0, "net_cash": 20.0},
                              close=10.0, shares=10.0, catalyst_evidence=[])
    assert "거부" in tp["entry"]


# ───────────────────────────── report_format (§5)

def test_report_header_and_disclaimer():
    req = {"ticker": "005930", "name": "삼성전자", "scheme": "dhandho",
           "scheme_label": "단도투자", "date": "20260630"}
    ctx = {"basis": "20260630", "close": 61000.0, "mktcap": 3.6e14,
           "fin_as_of": "2025 사업보고서", "entry": "테스트", "targets": {},
           "assumptions": [], "checklist": ["확인1"], "data_status": []}
    text = report_format.build(req, ctx)
    assert text.splitlines()[0] == "🔎 삼성전자 단도투자 방식 분석 (2026-06-30 기준)"
    assert report_format.DISCLAIMER in text
    assert "재무 기준: 2025 사업보고서" in text
    assert "시가총액 360조" in text                       # 3.6e14원 = 360조
    assert "확인1" in text
