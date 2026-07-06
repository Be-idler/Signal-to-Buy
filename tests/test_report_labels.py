"""한국어 라벨·플래그 변환 테스트 (가시성 개선)."""
from dhandho import report_labels as rl


def test_grade_word():
    assert rl.grade_word(4.5) == "우수"
    assert rl.grade_word(3.2) == "양호"
    assert rl.grade_word(2.6) == "보통"
    assert rl.grade_word(2.4) == "미흡"
    assert rl.grade_word(None) == "미상"


def test_flow_basis_flags_grouped_korean():
    flags = ["revenue_flow_basis_mismatch", "operating_income_flow_basis_mismatch",
             "net_income_flow_basis_mismatch"]
    out = rl.translate_flags(flags)
    assert len(out) == 1                                  # 하나로 그룹화
    line = out[0]
    assert "매출" in line and "영업이익" in line and "순이익" in line
    assert "12개월" in line and "전년 동기" in line
    assert "flow_basis_mismatch" not in line              # 코드 노출 안 함


def test_insufficient_flags_translated():
    out = rl.translate_flags(["D2_insufficient", "B4_insufficient"])
    assert len(out) == 1
    assert "급락 원인" in out[0] and "해자" in out[0]


def test_ttm_fallback_flag():
    out = rl.translate_flags(["revenue_ttm_fallback_annual"])
    assert "연간값으로 근사" in out[0] and "매출" in out[0]


def test_known_flag_mapped():
    out = rl.translate_flags(["C1_pbr_psr_fallback"])
    assert out == ["적자로 인해 저평가 평가를 PBR·PSR 기준으로 대체했습니다."]


def test_unknown_flag_kept_readably():
    out = rl.translate_flags(["some_new_flag"])
    assert out == ["(기타: some_new_flag)"]


def test_section_and_verdict_labels():
    assert rl.SECTION_KR["A"] == "하방보호(안전마진)"
    assert rl.VERDICT_KR["PASS"] == "보류"
    assert rl.SUBSCORE_KR["D2"] == "급락 원인"
