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
