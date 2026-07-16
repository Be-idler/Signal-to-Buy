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
    assert good["peg"] == pytest.approx(0.4)
    assert good["peg_adj"] == pytest.approx(2.5)       # (20+0)/8 — 배당 0 보수 가정
    assert good["subscores"]["LB"]["score"] == 5.0     # 린치지수 ≥2.0 + 성장 스위트스폿
    assert "dividend_yield_unavailable" in good["flags"]
    deficit = score_lynch({"per": None, "eps_cagr_5y": -0.1})
    assert deficit["subscores"]["LB"]["score"] == 2.5  # PEG 무의미 → 보수 캡
    assert "peg_unavailable" in deficit["flags"]
    assert deficit["grade"] in ("보류", "제외")


def test_lynch_dividend_adjusted_peg_lifts_score():
    base = {"per": 20.0, "eps_cagr_5y": 0.12}
    no_div = score_lynch(base)                          # 린치지수 12/20 = 0.6 → B1 2점
    with_div = score_lynch(base, dividend_yield=0.05)   # (12+5)/20 = 0.85 → 여전히 2점대
    rich_div = score_lynch(base, dividend_yield=0.09)   # (12+9)/20 = 1.05 → B1 3점
    assert with_div["peg_adj"] > no_div["peg_adj"]
    assert rich_div["subscores"]["LB"]["score"] > no_div["subscores"]["LB"]["score"]


def test_lynch_overheated_growth_penalized():
    r = score_lynch({"per": 10.0, "eps_cagr_5y": 0.45})  # 30% 초과 성장 — 지속 불가능
    assert "growth_unsustainable_penalty" in r["flags"]


def test_lynch_slow_grower_capped_at_watch():
    # 저성장·배당형은 점수와 무관하게 상한 = 관심종목 편입
    r = score_lynch({"per": 4.0, "eps_cagr_5y": 0.05, "net_cash": 100.0,
                     "fcf": 50.0, "fcf_negative_years": 0, "cfo_to_ni": 1.3})
    assert r["category"] == "저성장·배당"
    assert r["grade"] not in ("적극 검토", "분할 검토")
    assert "slow_grower_watch_cap" in r["flags"]


def test_lynch_cyclical_per_reverse_logic():
    # 저PER + 이익이 다년 최고치 = 피크 신호 → LE 저점
    m = {"per": 5.0, "eps_cagr_5y": None, "op_margin_cv": 0.9,
         "op_income_history": [50.0, 80.0, 120.0, 200.0, 300.0]}
    r = score_lynch(m)
    assert r["category"] == "경기순환"
    assert r["subscores"]["LE"]["score"] == 1.5
    assert "cyclical_per_reverse_logic" in r["flags"]


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
    assert "산출 안 함" in tp["targets"]["3년"]           # 단기는 전부 부정
    assert "분할매수 밴드" in tp["entry"]


def test_ltgg_five_year_target_is_default():
    # 매출 5년 CAGR 25% → 5년 목표가 = 100 × 1.25^5 ≈ 305원, 약 3.1배
    m = {"revenue_cagr_5y": 0.25}
    tp = target_price.compute("ltgg", m, close=100.0, shares=10.0)
    assert "305원" in tp["targets"]["5년"]
    assert "3.1배" in tp["targets"]["5년"]


def test_ltgg_five_year_growth_clipped():
    # 비현실적 고성장(80%)도 40%로 클립 → 100 × 1.4^5 ≈ 538원
    m = {"revenue_cagr_5y": 0.80}
    tp = target_price.compute("ltgg", m, close=100.0, shares=10.0)
    assert "538원" in tp["targets"]["5년"] and "40%" in tp["targets"]["5년"]


def test_ltgg_five_year_target_needs_growth():
    tp = target_price.compute("ltgg", {}, close=100.0, shares=10.0)   # 성장률 미확보
    assert "산출 불가" in tp["targets"]["5년"]


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
    assert "핸드오프" not in text                        # handoff 미지정 시 블록 생략


# ───────────────────────────── 클로드 채팅 핸드오프 (§7 확장)

def test_report_renders_handoff_block():
    req = {"ticker": "005930", "name": "삼성전자", "scheme": "buffett",
           "scheme_label": "버핏멍거", "date": None}
    ctx = {"basis": "20260708", "close": 61000.0, "mktcap": 3.6e14,
           "entry": "x", "targets": {}, "assumptions": [],
           "handoff": ["스킴=buffett(버핏멍거) · 종목=삼성전자(005930) · 기준일=20260708",
                       "정량점수: BA=3.50"]}
    text = report_format.build(req, ctx)
    assert "■ 클로드 심층분석 핸드오프" in text
    assert "스킴=buffett" in text
    # 핸드오프는 고지문(disclaimer)보다 앞에 위치
    assert text.index("핸드오프") < text.index(report_format.DISCLAIMER)


def test_handoff_lines_contents():
    import run_query
    req = {"ticker": "006050", "name": "국영지앤엠", "scheme": "buffett",
           "scheme_label": "버핏멍거", "date": None}
    lines = run_query._handoff_lines(
        req, "20260708",
        scores={"BA": 3.5, "BB": 3.0, "BC": 3.5, "BD": 4.2, "BE": 2.5},
        total=3.28, grade="보류",
        gates={"정직성(G1)": True, "해자(B≥3.5)": False},
        extras=["캡 이전 정량값: BB=3.4 BC=3.8"],
        flags=["BB_type_capped", "BC_quant_only"])
    joined = "\n".join(lines)
    assert "스킴=buffett(버핏멍거) · 종목=국영지앤엠(006050) · 기준일=20260708" in joined
    assert "BB=3.00" in joined and "종합 3.28" in joined and "등급 '보류'" in joined
    assert "정직성(G1)=통과" in joined and "해자(B≥3.5)=미달" in joined
    assert "캡 이전 정량값: BB=3.4 BC=3.8" in joined
    assert "재채점 대상(정성): " in joined and "moat" in joined
    assert "BB_type_capped;BC_quant_only" in joined
    assert "prompts/buffett.md" in joined


def test_buffett_exposes_quant_precap():
    from dhandho.frameworks import score_buffett
    m = {"roe_mean": 0.16, "roe_stdev": 0.02, "gpa": 0.30,
         "gross_margin_slope": 0.005, "roiic": 0.20, "op_margin_cv": 0.1,
         "op_income_history": [10, 12, 14, 15, 16, 17]}
    r = score_buffett(m)
    pre = r["quant_precap"]
    assert pre["BB"] is not None and pre["BB"] > 3.0    # 캡 이전 정량값 > 캡(3.0)
    assert r["subscores"]["BB"]["score"] == 3.0         # 표기값은 캡 적용
    assert pre["BC"] is not None


# ──────────────────────────── 질의응답 LLM 정성 그라운딩 (애드온)

def test_llm_score_single_parses_rubric_json(monkeypatch):
    from dhandho import llm

    class _Blk:
        type = "text"
        text = ('{"B4":{"score":4.0,"grounded":true,"reason":"브랜드 해자",'
                '"basis":[{"tier":1,"source":"사업보고서","date":"20260320"}]},'
                '"drop_reason":"일회성 비용","selection_reason":"저평가"}')

    class _Resp:
        content = [_Blk()]

    class _Msgs:
        def create(self, **kw):
            assert "종목 005930" in kw["messages"][0]["content"]
            return _Resp()

    class _Client:
        messages = _Msgs()

    monkeypatch.setattr(llm, "_client", lambda: _Client())
    qual = llm.score_single("005930", "추출 자료")
    assert qual["B4"]["score"] == 4.0 and qual["B4"]["basis"]
    assert qual["drop_reason"] == "일회성 비용"


def test_dhandho_caveat_reflects_grounding():
    import run_query
    s = run_query._dhandho_caveat(["B4", "D2"], None)
    assert "B4·D2" in s and "D3·F1·F3" in s          # 반영/보수처리 구분 표기
    s2 = run_query._dhandho_caveat([], "api timeout")
    assert "api timeout" in s2 and "보수적으로" in s2


def test_josa_selects_by_batchim():
    import run_query
    assert run_query._josa("안전마진(하방보호)") == "가"   # '호' 받침 없음
    assert run_query._josa("저평가 매력") == "이"           # '력' 받침 있음


def test_dhandho_target_neutralized_when_deep_value_unfit():
    # 자산 바닥·업사이드가 현재가를 크게 밑돌면 매수영역 아님을 명시하고
    # 목표가를 '수렴 가능'으로 포장하지 않는다 (우량·고가 종목 오표기 방지)
    from dhandho import target_price
    asym = {"bottom_mktcap": 10_000.0, "upside_mktcap": 50_000.0,
            "bottom_basis": "NCAV", "upside_basis": "영업이익×8x"}
    r = target_price.compute("dhandho", {}, close=1000.0, shares=100.0,
                             asym=asym, catalyst_evidence=["자사주 공시"])
    assert "매수영역 아님" in r["entry"]
    assert "현재가 이하" in r["targets"]["6개월"] and "수렴 가능" not in r["targets"]["6개월"]
    assert "부적용" in r["targets"]["3년"]


# ──────────────────────────── TTM 자동 백필 · 뉴스 그라운딩 (애드온)

def test_news_rss_parsing():
    from dhandho import news
    raw = """<rss><channel>
    <item><title>삼성전자, 2분기 실적 반등 전망</title>
      <pubDate>Tue, 15 Jul 2026 08:00:00 GMT</pubDate>
      <source url="https://x">한국경제</source></item>
    <item><title><![CDATA[반도체 업황 회복세 &amp; 수출 증가]]></title>
      <pubDate>Mon, 14 Jul 2026 02:00:00 GMT</pubDate>
      <source url="https://y">연합뉴스</source></item>
    </channel></rss>"""
    items = news._parse_rss(raw)
    assert len(items) == 2
    assert items[0]["title"] == "삼성전자, 2분기 실적 반등 전망"
    assert items[0]["source"] == "한국경제"
    assert "&" in items[1]["title"]                   # CDATA·엔티티 해제


def test_llm_doc_text_renders_news_and_market_note():
    from dhandho import llm
    docs = {"news": [{"title": "급락 원인은 일회성 소송 충당금", "date": "0715",
                      "source": "매경"}],
            "market_note": "하락의 88%가 지수 동반 하락(β≈1)"}
    out = llm._doc_text(docs)
    assert "뉴스 헤드라인" in out and "일회성 소송 충당금" in out
    assert "tier 3" in out                            # 미디어 계층 명시
    assert "시장 요인 분해" in out and "88%" in out


def test_backfill_company_upserts_row(monkeypatch):
    import pandas as pd
    from dhandho import dart, pit, storage
    from run_quarterly import FIN_COLUMNS

    monkeypatch.setattr(dart, "get_financials", lambda c, y, r: [{"dummy": 1}])
    monkeypatch.setattr(dart, "normalize_financials",
                        lambda rows: {"revenue": 100.0, "total_assets": 500.0})
    existing = pd.DataFrame([{c: None for c in FIN_COLUMNS} | {"ticker": "000001"},
                             {c: None for c in FIN_COLUMNS} | {"ticker": "005930"}],
                            columns=FIN_COLUMNS)
    monkeypatch.setattr(storage, "exists", lambda p: True)
    monkeypatch.setattr(storage, "read_parquet", lambda p: existing.copy())
    uploaded = {}
    monkeypatch.setattr(storage, "upload_parquet",
                        lambda df, p: uploaded.update({"df": df, "path": p}))
    ok = pit.backfill_company("005930", "00126380", 2025, "11013")
    assert ok
    assert uploaded["path"] == "financials/2025_11013.parquet"
    df = uploaded["df"]
    assert len(df) == 2                               # 교체(업서트) — 중복 없음
    row = df[df["ticker"] == "005930"].iloc[0]
    assert row["revenue"] == 100.0 and row["corp_code"] == "00126380"


def test_backfill_company_rejects_empty(monkeypatch):
    from dhandho import dart, pit
    monkeypatch.setattr(dart, "get_financials", lambda c, y, r: [])
    monkeypatch.setattr(dart, "normalize_financials", lambda rows: {})
    assert pit.backfill_company("005930", "00126380", 2025, "11013") is False
