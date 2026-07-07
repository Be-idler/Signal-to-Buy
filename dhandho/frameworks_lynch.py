"""피터 린치 (lynch) — 카테고리 분류 + 가중 섹션 스코어 (v1.0 프롬프트 정합).

섹션: LA 스토리 명료성(.15) · LB 성장의 질과 가격(.30 ★핵심) · LC 재무 건전성(.20)
      · LD 시장의 무관심(.15) · LE 카테고리 특화(.20)
정량 미확보 항목(스토리·검색량·기관보유·재고)은 2.5 캡 + 체크리스트 위임.
매수급 게이트: LB ≥ 3.0. 저성장·배당형은 판정 상한 = 관심종목 편입.

한계(비판) 명기: 성장률의 지속성은 정량으로 보증 불가 — 성장 둔화 시 PEG는
급격히 악화(이중 타격). 적자·성장률 음수 기업엔 PEG 무의미 →
자산주/턴어라운드 루브릭으로 자동 전환.
"""
from __future__ import annotations

CAP = 2.5

LIMITS_NOTE = ("한계: PEG는 성장 지속성을 보증하지 않음(둔화 시 이중 타격). "
               "적자·성장률 음수엔 PEG 무의미 → 자산주/턴어라운드로 자동 전환됨. "
               "스토리·검색량·기관보유는 정량 미확보 — 사람이 확인 후 재평가 필요.")


def _clip(x: float, lo: float = 1.0, hi: float = 5.0) -> float:
    return max(lo, min(hi, x))


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


def peg_adjusted(m: dict, dividend_yield: float | None = None) -> float | None:
    """배당조정 린치지수 = (EPS 성장률% + 배당수익률%) ÷ PER — 높을수록 유리.

    배당수익률 미확보 시 0 가정(보수적). 프롬프트 B1 앵커의 기준값.
    """
    per, g = m.get("per"), m.get("eps_cagr_5y")
    if per is None or per <= 0 or g is None or g <= 0:
        return None
    dy = dividend_yield if dividend_yield is not None else 0.0
    return (g * 100.0 + dy * 100.0) / per


def _score_lb(m: dict, dividend_yield: float | None, flags: list[str]) -> float | None:
    """LB 성장의 질과 가격 (★핵심 .30)."""
    parts = []
    # B1 배당조정 린치지수 앵커: ≥2.0→5 / 1.5→4 / 1.0→3 / 0.5→2 / 미만→1
    y = peg_adjusted(m, dividend_yield)
    if y is not None:
        parts.append(5.0 if y >= 2.0 else 4.0 if y >= 1.5 else
                     3.0 if y >= 1.0 else 2.0 if y >= 0.5 else 1.0)
        if dividend_yield is None:
            flags.append("dividend_yield_unavailable")
    else:
        flags.append("peg_unavailable")
    # B2 성장률 적정성: 20~25% 스위트스폿, 30% 초과 지속은 감점(지속 불가능)
    g = m.get("eps_cagr_5y")
    if g is not None and g > 0:
        parts.append(5.0 if 0.20 <= g <= 0.25 else
                     4.0 if 0.15 <= g < 0.20 or 0.25 < g <= 0.30 else
                     3.0 if 0.10 <= g < 0.15 else
                     2.5 if g > 0.30 else                    # 과속 성장 — 둔화 리스크
                     2.0 if g >= 0.05 else 1.0)
        if g > 0.30:
            flags.append("growth_unsustainable_penalty")
    # B3 이익의 질: 현금전환(영업CF/순이익) proxy — 정상화 EPS 대체
    cq = m.get("cfo_to_ni")
    if cq is not None:
        parts.append(5.0 if cq >= 1.2 else 4.0 if cq >= 1.0 else
                     3.0 if cq >= 0.8 else 2.0 if cq >= 0.5 else 1.0)
    # B4 성장의 일관성: 다년 영업이익에서 YoY 하락·적자 횟수
    hist = [v for v in (m.get("op_income_history") or []) if v is not None]
    if len(hist) >= 4:
        declines = sum(1 for i in range(1, len(hist)) if hist[i] < hist[i - 1])
        losses = sum(1 for v in hist if v <= 0)
        parts.append(1.5 if losses >= 1 else
                     5.0 if declines == 0 else 4.0 if declines == 1 else
                     3.0 if declines == 2 else 2.0)
    return sum(parts) / len(parts) if parts else None


def _score_lc(m: dict, flags: list[str]) -> float | None:
    """LC 재무 건전성: C1 부채 앵커 · C3 FCF 창출력 (C2 재고는 데이터 미확보)."""
    parts = []
    nc, dr = m.get("net_cash"), m.get("debt_ratio")
    if nc is not None and nc >= 0:
        parts.append(5.0)
    elif dr is not None:
        parts.append(4.0 if dr < 0.25 else 3.0 if dr < 0.5 else
                     2.0 if dr < 1.0 else 1.0)
    fcf, nyr = m.get("fcf"), m.get("fcf_negative_years")
    if fcf is not None:
        if fcf > 0:
            parts.append(5.0 if nyr == 0 else 4.0)
        else:
            parts.append(1.5)
    flags.append("inventory_check_unavailable")    # C2 재고 vs 매출 — 수동 확인
    return sum(parts) / len(parts) if parts else None


def _score_le(m: dict, category: str, flags: list[str]) -> float | None:
    """LE 카테고리별 특화 지표."""
    if category == "자산주":
        v = m.get("ncav_to_mktcap")
        flags.append("asset_accessibility_manual")   # 주주 접근 가능성은 수동 검증
        if v is None:
            return None
        return 5.0 if v >= 1.0 else 4.0 if v >= 0.8 else 3.0 if v >= 0.5 else CAP
    if category == "턴어라운드":
        score = 3.5
        slope, dr = m.get("op_income_slope"), m.get("debt_ratio")
        if slope is not None and slope > 0.05:
            score += 0.5                              # 회복 촉매의 정량 증거
        if dr is not None and dr < 1.0:
            score += 0.5                              # 생존력(부채 여력)
        flags.append("turnaround_uncertain")
        return score
    if category == "경기순환":
        # PER 역논리: 저PER + 이익이 다년 최고치(피크) = 고점 신호 → 1점대
        per = m.get("per")
        hist = [v for v in (m.get("op_income_history") or []) if v is not None]
        flags.append("cyclical_per_reverse_logic")
        if per is not None and len(hist) >= 4 and hist[-1] == max(hist) and per < 8:
            return 1.5
        return None                                   # 사이클 위치 판정은 수동
    if category == "고성장":
        # 확장 여력: 3년 CAGR ≥ 5년 CAGR = 가속 유지 / 둔화 + 고PER = 확장 후반
        g3, g5, per = m.get("revenue_cagr_3y"), m.get("revenue_cagr_5y"), m.get("per")
        if g3 is None or g5 is None:
            return None
        if g3 >= g5:
            return 4.5
        if per is not None and per > 25:
            flags.append("late_stage_expansion_signal")   # 성장 둔화 + PER 확장
            return 2.0
        return 3.0
    if category == "스톨워트":
        per = m.get("per")
        if per is None:
            return None
        return 4.5 if per <= 10 else 3.5 if per <= 15 else 2.5
    if category == "저성장·배당":
        flags.append("slow_grower_watch_cap")
        return CAP
    return None                                       # 분류불가


def score_lynch(m: dict, dividend_yield: float | None = None) -> dict:
    """가중 섹션 스코어. 반환: category/peg/peg_adj/score/subscores/grade/basis/limits/flags."""
    category = classify(m)
    flags: list[str] = []
    p = peg(m)
    p_adj = peg_adjusted(m, dividend_yield)

    la = None                                         # 스토리·검색량 — 정성 미확보
    lb = _score_lb(m, dividend_yield, flags)
    lc = _score_lc(m, flags)
    ld = None                                         # 기관보유·커버리지·검색량 미확보
    le = _score_le(m, category, flags)

    subscores = {}
    weights = {"LA": 0.15, "LB": 0.30, "LC": 0.20, "LD": 0.15, "LE": 0.20}
    for key, val in (("LA", la), ("LB", lb), ("LC", lc), ("LD", ld), ("LE", le)):
        if val is None:
            val = CAP
            flags.append(f"{key}_insufficient")
        subscores[key] = {"score": round(_clip(val), 2), "weight": weights[key]}

    total = round(sum(v["score"] * v["weight"] for v in subscores.values()), 3)

    # 게이트·판정: LB≥3.0 없이는 매수급 불가, 저성장은 상한 관심종목
    lb_score = subscores["LB"]["score"]
    if category == "분류불가":
        grade = "보류"
    elif total >= 4.2 and lb_score >= 3.0:
        grade = "적극 검토"
    elif total >= 3.5 and lb_score >= 3.0:
        grade = "분할 검토"
    elif total >= 3.0:
        grade = "관심종목 편입"
    elif total >= 2.5:
        grade = "보류"
    else:
        grade = "제외"
    if category == "저성장·배당" and grade in ("적극 검토", "분할 검토"):
        grade = "관심종목 편입"

    if p_adj is not None:
        basis = (f"배당조정 린치지수 {p_adj:.2f} (성장 {m.get('eps_cagr_5y'):.0%}"
                 f" / PER {m.get('per'):.1f})")
    elif category in ("자산주", "턴어라운드"):
        basis = f"{category} 루브릭 적용 (PEG 미적용)"
    else:
        basis = "PEG 산출 불가(적자 또는 성장률 미확보) → 보수적 캡"

    return {"framework": "lynch", "category": category, "peg": p, "peg_adj": p_adj,
            "score": total, "subscores": subscores, "basis": basis, "grade": grade,
            "limits": LIMITS_NOTE, "flags": sorted(set(flags))}
