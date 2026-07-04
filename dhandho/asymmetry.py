"""비대칭(업사이드/다운사이드) 계산 — 단도의 핵심 (애드온1 §1.4).

순수함수: 입력(파생지표 dict, mktcap) → 출력(bottom, upside, ratio, 근거 텍스트).
검증 불가한 바닥은 바닥이 아니다 — 바닥값의 근거(어떤 자산)를 반드시 병기한다.
"""
from __future__ import annotations

CONSERVATIVE_MULTIPLE = 8.0    # 정상화이익 보수 멀티플 (업종 하위 40%ile 근사, 백테스트 보정 대상)


def compute(m: dict) -> dict:
    """m: metrics.compute_derived 출력 (mktcap 결합분 포함).

    반환: {"bottom_mktcap", "upside_mktcap", "ratio", "bottom_basis",
           "upside_basis", "negative_risk", "flags"}
    시총 대비 배수 기준으로 계산(주당 환산은 target_price에서 shares로 수행).
    """
    mktcap = m.get("mktcap")
    flags: list[str] = []
    if not mktcap or mktcap <= 0:
        return {"bottom_mktcap": None, "upside_mktcap": None, "ratio": None,
                "bottom_basis": "시총 미확보", "upside_basis": "",
                "negative_risk": False, "flags": ["mktcap_missing"]}

    # 다운사이드 바닥 = max(NCAV, 순현금) — 보수적 청산가치 근사.
    # §3 주의(자산 ≠ 실현가능가치): 소수주주 실현 가능성은 사람이 확인(체크리스트).
    candidates = []
    if m.get("ncav") is not None:
        candidates.append((m["ncav"], "NCAV(유동자산−총부채)"))
    if m.get("net_cash") is not None:
        candidates.append((m["net_cash"], "순현금(현금성+단기금융−차입금)"))
    if not candidates:
        return {"bottom_mktcap": None, "upside_mktcap": None, "ratio": None,
                "bottom_basis": "바닥 산출 불가(NCAV·순현금 미확보)", "upside_basis": "",
                "negative_risk": False, "flags": ["bottom_unavailable"]}
    bottom, bottom_basis = max(candidates, key=lambda x: x[0])
    if bottom < 0:
        bottom_basis += " — 음수(자산 바닥 없음)"
        flags.append("bottom_negative")

    # 업사이드 = 정상화 이익 × 보수적 멀티플. 정상화 이익 ≈ TTM 영업이익
    # (다년 평균이 더 보수적이나 접근 가능 데이터 기준 — 플래그로 명시)
    op = m.get("operating_income")
    if op is None or op <= 0:
        upside, upside_basis = None, "정상화이익 산출 불가(영업이익 없음/적자)"
        flags.append("upside_unavailable")
    else:
        upside = op * CONSERVATIVE_MULTIPLE
        upside_basis = f"영업이익 {op:,.0f} × 보수 멀티플 {CONSERVATIVE_MULTIPLE:.0f}x"

    ratio = None
    negative_risk = False
    if upside is not None:
        downside_gap = mktcap - bottom
        if downside_gap <= 0:
            negative_risk = True           # 바닥이 현재가 위 — '음(陰)의 리스크'
            ratio = float("inf")
        else:
            ratio = (upside - mktcap) / downside_gap

    return {"bottom_mktcap": bottom, "upside_mktcap": upside, "ratio": ratio,
            "bottom_basis": bottom_basis, "upside_basis": upside_basis,
            "negative_risk": negative_risk, "flags": flags}


def verdict(ratio: float | None, negative_risk: bool) -> str:
    """판정 가이드: ≥3:1 강한 비대칭 / 2~3:1 통과 / <2:1 관심종목 이하."""
    if negative_risk:
        return "바닥이 현재가 위 — 강한 비대칭(자산 실현가능성 검증 필수)"
    if ratio is None:
        return "산출 불가"
    if ratio >= 3:
        return f"{ratio:.1f}:1 — 강한 비대칭"
    if ratio >= 2:
        return f"{ratio:.1f}:1 — 통과"
    return f"{ratio:.1f}:1 — 관심종목 이하"
