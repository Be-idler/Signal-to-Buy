"""피터 린치 (lynch) — GARP·카테고리 분류 (애드온2 §4.1).

한계(비판) 명기: 성장률의 지속성은 정량으로 보증 불가 — 성장 둔화 시 PEG는
급격히 악화(이중 타격). 적자·성장률 음수 기업엔 PEG 무의미 →
자산주/턴어라운드 루브릭으로 자동 전환.
"""
from __future__ import annotations

CAP = 2.5

LIMITS_NOTE = ("한계: PEG는 성장 지속성을 보증하지 않음(둔화 시 이중 타격). "
               "적자·성장률 음수엔 PEG 무의미 → 자산주/턴어라운드로 자동 전환됨.")


def classify(m: dict) -> str:
    """카테고리 자동 분류 — 턴어라운드 → 자산주 → 성장 구간 순."""
    hist = m.get("op_income_history") or []
    if len(hist) >= 2 and hist[-2] is not None and hist[-1] is not None \
            and hist[-2] <= 0 < hist[-1]:
        return "턴어라운드"
    ncav = m.get("ncav_to_mktcap")
    ncash = m.get("net_cash_to_mktcap")
    if (ncav is not None and ncav >= 0.8) or (ncash is not None and ncash >= 0.5):
        return "자산주"
    g = m.get("eps_cagr_5y")
    if g is None:
        cv = m.get("op_margin_cv")
        if cv is not None and cv > 0.7:
            return "경기순환"
        return "분류불가"
    if g > 0.20:
        return "고성장"
    if g >= 0.10:
        return "스톨워트"
    return "저성장·배당"


def peg(m: dict) -> float | None:
    """PEG = 트레일링 PER ÷ EPS 성장률(%). 적자·음수 성장은 None."""
    per, g = m.get("per"), m.get("eps_cagr_5y")
    if per is None or g is None or g <= 0:
        return None
    return per / (g * 100.0)


def score_lynch(m: dict) -> dict:
    """카테고리별 점수 (1.0~5.0). 반환: category/peg/score/grade/limits/flags."""
    category = classify(m)
    flags: list[str] = []
    p = peg(m)

    if category == "자산주":
        # 자산주는 PEG 대신 NCAV/시총
        v = m.get("ncav_to_mktcap")
        score = (5.0 if v is not None and v >= 1.0 else
                 4.0 if v is not None and v >= 0.8 else
                 3.0 if v is not None and v >= 0.5 else CAP)
        basis = f"NCAV/시총 {v:.2f}" if v is not None else "NCAV 미확보"
    elif category == "턴어라운드":
        # 흑자전환 확도: 이익 추세 기울기·재무 생존력으로 근사
        slope = m.get("op_income_slope")
        dr = m.get("debt_ratio")
        score = 3.5
        if slope is not None and slope > 0.05:
            score += 0.5
        if dr is not None and dr < 1.0:
            score += 0.5
        basis = f"흑자전환 (기울기 {slope}, 부채비율 {dr})"
        flags.append("turnaround_uncertain")
    elif p is not None:
        score = (5.0 if p <= 0.5 else 4.0 if p <= 1.0 else
                 3.0 if p <= 1.5 else 2.0 if p <= 2.0 else 1.0)
        basis = f"PEG {p:.2f} (PER {m.get('per'):.1f} / 성장 {m.get('eps_cagr_5y'):.0%})"
    else:
        score = CAP
        basis = "PEG 산출 불가(적자 또는 성장률 미확보) → 2.5 캡"
        flags.append("peg_unavailable")

    # 보조: 부채비율 과다 감점 (린치의 대차대조표 점검)
    dr = m.get("debt_ratio")
    if dr is not None and dr > 2.0:
        score = max(1.0, score - 1.0)
        flags.append("high_leverage_penalty")

    score = round(min(5.0, max(1.0, score)), 2)
    grade = ("적극/분할 후보" if score >= 4.0 else
             "관심" if score >= 3.0 else "제외")
    return {"framework": "lynch", "category": category, "peg": p,
            "score": score, "basis": basis, "grade": grade,
            "limits": LIMITS_NOTE, "flags": flags}
