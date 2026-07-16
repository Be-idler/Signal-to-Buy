"""리포트 한국어 라벨·용어 사전 (가시성 개선).

점수 코드(A/B/C, C1…)·플래그 원문을 사람이 읽기 쉬운 한국어로 변환한다.
숫자만 던지지 않고 등급어(우수·양호·보통·미흡)와 함께 제시한다.
"""
from __future__ import annotations

# 단도 6개 섹션 → 한국어 이름
SECTION_KR = {
    "A": "하방보호(안전마진)",
    "B": "사업의 질·수익성",
    "C": "저평가 여부",
    "D": "밸류트랩 배제(안정성)",
    "E": "주주환원",
    "F": "경영진·내부자",
}

# 하위지표 코드 → 한국어
SUBSCORE_KR = {
    "A1": "순현금", "A2": "청산가치(NCAV)", "A3": "재무건전성", "A4": "FCF 안정성",
    "B1": "자본수익성(ROIC)", "B2": "마진 예측가능성", "B3": "이익 추세", "B4": "해자(경쟁우위)",
    "C1": "저평가(멀티플)", "C2": "과거 밴드 위치", "C3": "이익 추세", "C4": "과도낙폭",
    "D1": "매출·이익 추세", "D2": "급락 원인", "D3": "산업 전망", "D4": "재무 생존력",
    "E1": "주주환원", "E2": "상법 수혜", "E3": "촉매 근접",
    "F1": "자본배분", "F2": "내부자 정렬", "F3": "IR 투명성",
    "L1": "성장 활주로", "L2": "증분 자본수익", "L3": "해자 확장성",
    "L4": "경영진·문화", "L5": "단위경제", "L6": "옵셔널리티",
    # 버핏·멍거 섹션 (v1.0 프롬프트)
    "BA": "사업 단순성·예측가능성", "BB": "경제적 해자",
    "BC": "경영진·자본배분", "BD": "재무 건전성·이익의 질",
    "BE": "안전마진(내재가치 할인)",
    # 아웃사이더 섹션 (손다이크 v2)
    "OA": "자본배분 실적", "OB": "주당 가치 복리 성장",
    "OC": "경영진 독립성·오너십", "OD": "재무 규율·유연성",
    "OE": "코리아 디스카운트 해소도",
    # 피터 린치 섹션 (v1.0 프롬프트)
    "LA": "스토리 명료성", "LB": "성장의 질과 가격",
    "LC": "재무 건전성", "LD": "시장의 무관심", "LE": "카테고리 특화",
}

# 신호 → 한국어
VERDICT_KR = {
    "BUY": "적극 검토 대상", "WATCH": "관심 종목",
    "PASS": "보류", "EXCLUDE": "제외",
}

# 손익 항목(영문 키) → 한국어
FLOW_FIELD_KR = {
    "revenue": "매출", "operating_income": "영업이익", "gross_profit": "매출총이익",
    "net_income": "순이익", "net_income_controlling": "지배주주순이익",
    "cfo": "영업현금흐름", "capex": "설비투자(CAPEX)",
    "interest_expense": "이자비용", "depreciation": "감가상각비",
}

# 개별 플래그 → 한국어 설명
FLAG_KR = {
    "mktcap_missing": "시가총액 정보가 없어 밸류에이션 일부를 계산하지 못했습니다.",
    "history_missing": "다년 재무 이력이 부족해 성장률·추세 일부를 계산하지 못했습니다.",
    "borrowings_not_found": "차입금 항목을 재무제표에서 확인하지 못했습니다.",
    "operating_income_missing": "영업이익 항목이 재무제표에 없습니다.",
    "C1_pbr_psr_fallback": "적자로 인해 저평가 평가를 PBR·PSR 기준으로 대체했습니다.",
    "C3_annual_proxy": "이익 추세를 연간 기준으로 근사했습니다(분기 추세 미반영).",
    "E1_heuristic": "주주환원 점수는 근사 산정치입니다.",
    "E3_disclosure_proxy": "촉매 근접도를 최근 공시로 근사했습니다.",
    "capital_impairment_excluded": "자본잠식으로 평가 대상에서 제외됐습니다.",
    "framework_target_mismatch_caveat":
        "이 투자 관점은 한국 종목에는 적용범위 밖일 수 있습니다(해석 주의).",
    "tunneling_hard_excluded": "터널링 정황이 확인되어 강제 제외됐습니다.",
    "bottom_negative": "자산 바닥(청산가치)이 음수여서 하방 앵커가 성립하지 않습니다.",
    "upside_unavailable": "영업이익이 없거나 적자여서 업사이드를 계산하지 못했습니다.",
    "fcf_per_share_proxy": "주당가치 성장을 총액 FCF 성장으로 근사했습니다(주식수 시계열 미확보).",
    "D1_partial": "매출·이익 추세 중 일부만 확인됐습니다.",
    "high_leverage_penalty": "부채비율이 과다해 감점됐습니다.",
    "peg_unavailable": "적자 또는 성장률 미확보로 PEG를 계산하지 못했습니다.",
    "turnaround_uncertain": "턴어라운드 확도가 불확실합니다.",
    # 버핏·멍거 (v1.0)
    "BA_short_history": "10년치 이력이 부족해 예측가능성 평가가 제한적입니다.",
    "BB_type_capped": "해자 유형을 정성 근거로 특정하지 못해 해자 점수를 3.0으로 제한했습니다.",
    "BC_quant_only": "경영진·자본배분은 증분 자본수익(정량)만으로 평가됐습니다 — "
                     "1달러 테스트·환원의 질은 수동 검증 필요.",
    "BD_leveraged_roe": "높은 ROE가 레버리지에 의존하는 것으로 보여 할인했습니다(듀폰 분해).",
    "BE_dcf_simplified": "내재가치는 오너어닝스 기반 간이 산정치입니다(정밀 DCF 아님).",
    "G2_circle_of_competence_manual": "능력범위 게이트(10년 후 예측 가능성)는 사람이 판정해야 합니다.",
    "owner_earnings_estimated": "오너어닝스는 유지 자본지출 미구분으로 총 CAPEX 기준 추정치입니다.",
    # 아웃사이더 (손다이크 v2)
    "OA_quant_only": "자본배분 실적은 증분 자본수익(정량)만 반영 — 인수·매각 규율은 공시 대조 필요.",
    "OD_idle_cash": "과다 현금(미배치 자본)으로 재무 규율 점수를 중립 이하로 제한했습니다.",
    # 피터 린치 (v1.0)
    "dividend_yield_unavailable": "배당수익률 미확보 — 배당조정 PEG는 배당 0으로 보수 가정했습니다.",
    "growth_unsustainable_penalty": "성장률 30% 초과 — 지속 불가능성으로 감점했습니다(린치 원칙).",
    "inventory_check_unavailable": "재고 데이터 미확보 — 재고 증가율 vs 매출 증가율(대표 적신호)은 수동 확인 필요.",
    "asset_accessibility_manual": "자산이 주주에게 실제 접근 가능한지(배당·매각·소각)는 수동 검증 필요.",
    "cyclical_per_reverse_logic": "경기순환주 — 저PER이 이익 피크 신호일 수 있어 PER 역논리를 적용했습니다.",
    "late_stage_expansion_signal": "성장 둔화 + 높은 PER — 확장 후반부 신호로 감점했습니다.",
    "slow_grower_watch_cap": "저성장·배당형은 판정 상한이 관심종목 편입입니다(린치 원칙).",
    "catalyst_no_evidence": "촉매를 뒷받침할 공시 근거가 없습니다.",
    "short_history": "가격 시계열이 짧습니다.",
    "no_json": "정성 분석 결과 형식 오류.",
    "json_decode": "정성 분석 결과 형식 오류.",
}


def grade_word(score) -> str:
    """점수 → 등급어."""
    if score is None:
        return "미상"
    if score >= 4.0:
        return "우수"
    if score >= 3.0:
        return "양호"
    if score >= 2.5:
        return "보통"
    return "미흡"


def _josa(word: str, batchim: str, no_batchim: str) -> str:
    """마지막 한글 음절의 받침 유무로 조사 선택 (괄호 등 비한글 꼬리 무시)."""
    for ch in reversed(word):
        if "가" <= ch <= "힣":
            return batchim if (ord(ch) - 0xAC00) % 28 else no_batchim
    return batchim


def translate_flags(flags, ttm_backfilled: bool = False) -> list[str]:
    """플래그 목록 → 사람이 읽는 한국어 설명 목록(중복 제거·그룹화)."""
    seen = list(dict.fromkeys(flags or []))
    out: list[str] = []
    flow, ttm, insuff, unknown = [], [], [], []
    for f in seen:
        if f.endswith("_flow_basis_mismatch"):
            flow.append(f[: -len("_flow_basis_mismatch")])
        elif f.endswith("_ttm_fallback_annual"):
            ttm.append(f[: -len("_ttm_fallback_annual")])
        elif f.endswith("_insufficient"):
            insuff.append(f[: -len("_insufficient")])
        elif f in FLAG_KR:
            out.append(FLAG_KR[f])
        else:
            unknown.append(f)

    if flow:
        names = ", ".join(FLOW_FIELD_KR.get(x, x) for x in flow)
        if ttm_backfilled:
            out.insert(0, f"{names}: DART에서 전년 동기 보고서를 자동 재입수했지만 "
                          f"분기 원문에 해당 계정이 공시되지 않아 산출에서 제외했습니다 "
                          f"(관련 항목은 보수적으로 처리).")
        else:
            out.insert(0, f"{names}: 최근 12개월(TTM) 계산에 필요한 전년 동기 재무가 "
                          f"아직 적재되지 않아 비어 있습니다. 분기 재무 적재 후 "
                          f"자동으로 채워집니다.")
    if ttm:
        names = ", ".join(FLOW_FIELD_KR.get(x, x) for x in ttm)
        tail = " (재입수 후에도 분기 원문 미공시)" if ttm_backfilled else ""
        out.append(f"{names}{_josa(names, '은', '는')} 전년 동기 대신 "
                   f"직전 연간값으로 근사했습니다{tail}.")
    if insuff:
        names = ", ".join(SUBSCORE_KR.get(x, x) for x in insuff)
        out.append(f"다음 항목은 근거가 부족해 보수적으로 처리됐습니다 — {names}.")
    for f in unknown:
        out.append(f"(기타: {f})")
    return out
