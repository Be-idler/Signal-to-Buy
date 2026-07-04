"""빌 애크먼 (ackman) — 퀄리티 집중 + 촉매·행동주의 (애드온2 §4.2).

점수 = 퀄리티(60%) × 촉매 실현성(40%). 촉매 없는 저평가는 "가치함정 위험" 라벨.
한계(비판) 명기: 애크먼 스타일은 집중·행동주의 영향력이 전제 — 소수주주는
촉매를 만들 수 없고 기다릴 수만 있다. 촉매 점수는 외부 공시 근거 있는 것만(보수적).
"""
from __future__ import annotations

CAP = 2.5

LIMITS_NOTE = ("한계: 행동주의 영향력 전제 — 소수주주는 촉매를 만들 수 없음. "
               "촉매 실현성은 공시 근거가 있는 것만 반영(보수적).")


def _step(value, cuts, below):
    if value is None:
        return None
    for cut, score in cuts:
        if value >= cut:
            return score
    return below


def quality_score(m: dict) -> tuple[float, list[str]]:
    """퀄리티 게이트: FCF 창출력·안정성, 진입장벽 proxy, 레버리지, ROIC."""
    parts, flags = [], []
    fm = _step(m.get("fcf_margin"), [(0.12, 5), (0.07, 4), (0.03, 3), (0.0, 2)], 1)
    nyr = m.get("fcf_negative_years")
    stab = {0: 5, 1: 4, 2: 3, 3: 2}.get(nyr, 1) if nyr is not None else None
    moat = _step(m.get("gpa"), [(0.35, 5), (0.25, 4), (0.15, 3), (0.05, 2)], 1)
    dr = m.get("debt_ratio")
    lev = _step(-dr, [(-0.5, 5), (-1.0, 4), (-1.5, 3), (-2.5, 2)], 1) if dr is not None else None
    roic = _step(m.get("roic"), [(0.15, 5), (0.10, 4), (0.07, 3), (0.03, 2)], 1)
    for name, v in (("fcf_margin", fm), ("fcf_stability", stab),
                    ("moat_proxy", moat), ("leverage", lev), ("roic", roic)):
        if v is None:
            flags.append(f"{name}_insufficient")
            v = CAP
        parts.append(v)
    return round(sum(parts) / len(parts), 2), flags


def catalyst_score(m: dict, disclosures: list[dict] | None) -> tuple[float, list[str], list[str]]:
    """촉매 실현성 — 외부 공시 근거만 반영. 근거 없으면 보수적 2.0."""
    evidence: list[str] = []
    flags: list[str] = []
    if disclosures:
        for d in disclosures:
            nm = d.get("report_nm", "")
            if "소각" in nm:
                evidence.append(f"자사주 소각 공시({d.get('rcept_dt')})")
            elif "자기주식" in nm or "자사주" in nm:
                evidence.append(f"자사주 공시({d.get('rcept_dt')})")
            elif "주식등의대량보유" in nm:
                evidence.append(f"5%룰 대량보유 보고({d.get('rcept_dt')})")
    if not evidence:
        flags.append("catalyst_no_evidence")
        return 2.0, evidence, flags
    score = 3.0 + min(len(evidence), 3) * 0.5
    if any("소각" in e for e in evidence):
        score += 0.5
    return round(min(5.0, score), 2), evidence, flags


def score_ackman(m: dict, disclosures: list[dict] | None = None) -> dict:
    q, q_flags = quality_score(m)
    c, evidence, c_flags = catalyst_score(m, disclosures)
    total = round(0.6 * q + 0.4 * c, 2)
    value_trap = q >= 3.5 and c < 3.0 and (m.get("per") or 99) < 8
    grade = ("적극/분할 후보" if total >= 4.0 and c >= 3.0 else
             "관심" if total >= 3.0 else "제외")
    labels = ["가치함정 위험(촉매 부재)"] if value_trap else []
    return {"framework": "ackman", "quality": q, "catalyst": c, "total": total,
            "catalyst_evidence": evidence, "grade": grade, "labels": labels,
            "limits": LIMITS_NOTE, "flags": sorted(set(q_flags + c_flags))}
