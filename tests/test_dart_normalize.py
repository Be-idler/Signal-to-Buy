from dhandho.dart import normalize_financials


def _row(acc_id="", acc_nm="", amount="0", sj="BS"):
    return {"account_id": acc_id, "account_nm": acc_nm,
            "thstrm_amount": amount, "sj_div": sj}


def test_standard_tags_mapped():
    rows = [
        _row("ifrs-full_Revenue", "매출액", "1,000", "IS"),
        _row("dart_OperatingIncomeLoss", "영업이익", "100", "IS"),
        _row("ifrs-full_Assets", "자산총계", "5,000"),
        _row("ifrs-full_Liabilities", "부채총계", "2,000"),
        _row("ifrs-full_CurrentAssets", "유동자산", "3,000"),
        _row("ifrs-full_CashAndCashEquivalents", "현금및현금성자산", "500"),
    ]
    fin = normalize_financials(rows)
    assert fin["revenue"] == 1000
    assert fin["operating_income"] == 100
    assert fin["total_assets"] == 5000
    assert fin["current_assets"] == 3000
    assert fin["cash_and_equivalents"] == 500


def test_alternative_operating_income_tag():
    rows = [_row("ifrs-full_ProfitLossFromOperatingActivities", "영업이익", "77", "IS")]
    assert normalize_financials(rows)["operating_income"] == 77


def test_name_fallback_when_nonstandard_tag():
    rows = [_row("-표준계정코드 미사용-", "영업이익", "55", "IS")]
    assert normalize_financials(rows)["operating_income"] == 55


def test_borrowings_keyword_sum():
    rows = [
        _row("", "단기차입금", "100"),
        _row("", "장기차입금", "200"),
        _row("", "사채", "300"),
        _row("", "리스부채", "50"),
        _row("", "매입채무", "999"),           # 비차입 — 미포함
    ]
    fin = normalize_financials(rows)
    assert fin["total_borrowings"] == 650


def test_borrowings_not_found_flagged():
    fin = normalize_financials([_row("ifrs-full_Assets", "자산총계", "10")])
    assert fin["total_borrowings"] is None
    assert "borrowings_not_found" in fin["flags"]


def test_operating_income_missing_flagged():
    fin = normalize_financials([_row("ifrs-full_Assets", "자산총계", "10")])
    assert "operating_income_missing" in fin["flags"]


def test_dash_amount_ignored():
    fin = normalize_financials([_row("ifrs-full_Revenue", "매출액", "-", "IS")])
    assert "revenue" not in fin


# ─────────────────────────── 정기보고서 본문 추출 (정성 그라운딩 원문)

def test_extract_business_sections_picks_body_over_toc():
    from dhandho import dart
    # 목차(짧은 매치)와 본문(긴 매치)이 공존 — 본문을 골라야 한다
    text = ("목차\n사업의 내용\n재무에 관한 사항\n감사인의 감사의견\n"
            "II. 사업의 내용\n당사는 리튬일차전지를 주력으로 하며 군수·스마트그리드향 "
            "매출 비중이 높다. 경쟁사는 소수이며 인증 장벽이 존재한다. " * 30
            + "\nIII. 재무에 관한 사항\n(재무제표...)")
    out = dart.extract_business_sections(text)
    assert "[사업의 내용]" in out
    assert "리튬일차전지" in out                      # 본문이 선택됨
    assert "재무제표" not in out                      # 다음 섹션에서 절단


def test_extract_business_sections_fallback_when_missing():
    from dhandho import dart
    text = "아무 구조 없는 문서 본문입니다. " * 50
    out = dart.extract_business_sections(text)
    assert out.startswith("아무 구조 없는")            # 앞부분 폴백


def test_llm_doc_text_includes_periodic_body():
    from dhandho import llm
    docs = {"periodic": {"report_nm": "사업보고서 (2025.12)", "rcept_dt": "20260320",
                         "rcept_no": "20260320000123",
                         "text": "[사업의 내용]\n당사는 전지 제조업을 영위한다."},
            "disclosures": [{"rcept_dt": "20260701", "report_nm": "단일판매ㆍ공급계약체결",
                             "rcept_no": "20260701000001"}],
            "disclosure_texts": [{"report_nm": "단일판매ㆍ공급계약체결",
                                  "rcept_dt": "20260701", "rcept_no": "20260701000001",
                                  "text": "계약금액 120억원, 매출액 대비 15%"}],
            "executives": []}
    out = llm._doc_text(docs)
    assert "정기보고서 원문 발췌" in out and "전지 제조업" in out
    assert "수시공시 본문" in out and "계약금액 120억원" in out
