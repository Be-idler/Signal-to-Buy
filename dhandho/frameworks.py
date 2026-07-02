"""5개 프레임워크 스코어링 (명세서 §5, §13).

원칙:
- 점수 1.0~5.0, 근거불충분(None)은 2.5 상한 + 플래그 (§13.0)
- 5개 관점 점수는 절대 평균하지 않는다 (§6)
- 임계·가중은 제안값 — 백테스트 전까지 실매매 판단 근거로 쓰지 않는다

단도 섹션 구성은 v1 구현명세서 §4 그대로:
  A 질적우위(.25): A1 순현금 A2 NCAV A3 재무건전성 A4 FCF안정
  B 수익성(.20):   B1 ROIC(.35) B2 마진예측(.25) B3 추세(.15) B4 해자(.25)🔶
  C 저평가(.20):   C1 멀티플 C2 과거밴드 C3 이익추세 C4 과도낙폭
  D 안정성(.15):   D1 매출이익추세(.30) D2 급락원인(.30)🔶 D3 산업사양화(.20)🔶 D4 생존력(.20)
  E 주주환원(.10): E1 주주환원(.40)=미소각자사주+배당(결정론) E2 상법수혜(.35) E3 촉매근접(.25)
  F 내부자(.10):   F1 자본배분(.35)🔶 F2 내부자정렬(.30)=소유보고 순증감(결정론) F3 IR투명성(.35)🔶
  🔶 = LLM 그라운딩 항목 (B4·D2·D3·F1·F3, v1 §7)
"""
from __future__ import annotations

import config
from dhandho import sector_relative

CAP = config.INSUFFICIENT_CAP   # 2.5


def _clip(x: float, lo: float = 1.0, hi: float = 5.0) -> float:
    return max(lo, min(hi, x))


def _step(value, cuts: list[tuple[float, float]], below: float):
    """value ≥ cut이면 해당 점수(내림차순 cuts). None → None."""
    if value is None:
        return None
    for cut, score in cuts:
        if value >= cut:
            return score
    return below


class _Section:
    """하위점수 수집기 — None은 2.5 상한 + 플래그."""

    def __init__(self, name: str):
        self.name = name
        self.subs: dict[str, float] = {}
        self.flags: list[str] = []

    def add(self, key: str, score: float | None, weight: float):
        if score is None:
            score = CAP
            self.flags.append(f"{key}_insufficient")
        self.subs[key] = {"score": round(_clip(score), 2), "weight": weight}

    def total(self) -> float:
        return round(sum(v["score"] * v["weight"] for v in self.subs.values()), 3)

    def result(self) -> dict:
        return {"subscores": self.subs, "total": self.total(), "flags": self.flags}


def _qual_score(qual: dict | None, key: str) -> float | None:
    """LLM 정성 결과에서 점수 추출. 근거 없으면 None(→2.5 캡).

    §10: 근거(출처·일자) 없는 정성 점수는 2.5 상한 + 플래그.
    """
    if not qual or key not in qual:
        return None
    item = qual[key]
    score = item.get("score")
    if score is None:
        return None
    if not item.get("basis"):               # 근거 강제
        return min(float(score), CAP)
    return float(score)


# ════════════════════════════════════════════════════ 단도 (§13, 트랙1)

def _score_A(m: dict, peers: dict | None = None) -> _Section:
    """A 하방보호 (§13.1). peers: 업종 왜곡 시 백분위 치환용 값 풀."""
    s = _Section("A")
    a1 = _step(m.get("net_cash_to_mktcap"),
               [(0.40, 5), (0.25, 4), (0.10, 3), (0.0, 2)], 1)
    a2 = _step(m.get("ncav_to_mktcap"),
               [(1.0, 5), (0.67, 4), (0.33, 3), (0.0, 2)], 1)
    if peers:
        if peers.get("net_cash_to_mktcap"):
            p = sector_relative.percentile_score(
                m.get("net_cash_to_mktcap"), peers["net_cash_to_mktcap"], True)
            a1 = p if p is not None else a1
        if peers.get("ncav_to_mktcap"):
            p = sector_relative.percentile_score(
                m.get("ncav_to_mktcap"), peers["ncav_to_mktcap"], True)
            a2 = p if p is not None else a2

    ic = m.get("interest_coverage")
    if ic is None:
        a3 = None
    else:
        a3 = _step(ic, [(5, 5), (3, 4), (1.5, 3), (1, 2)], 1)  # inf ≥ 5 → 5
        dr = m.get("debt_ratio")
        if dr is not None:
            if dr > 2.0:
                a3 -= 1.0
            elif dr < 0.5:
                a3 += 0.5
        a3 = _clip(a3)

    n = m.get("fcf_negative_years")
    a4 = {0: 5, 1: 4, 2: 3, 3: 2}.get(n, 1) if n is not None else None

    s.add("A1", a1, 0.35)
    s.add("A2", a2, 0.25)
    s.add("A3", a3, 0.25)
    s.add("A4", a4, 0.15)
    return s


def _score_C(m: dict, peers: dict | None = None,
             band_series: list[float] | None = None) -> _Section:
    """C 밸류에이션 (§13.2)."""
    s = _Section("C")

    # C1 트레일링 멀티플 — 업종 백분위(저평가일수록 고득점). 적자면 PBR·PSR 폴백.
    c1 = None
    if peers:
        parts = []
        for key in ("ev_ebit", "per"):
            v = m.get(key)
            pool = peers.get(key) or []
            p = sector_relative.percentile_score(v, pool, higher_is_better=False)
            if p is not None:
                parts.append(p)
        if not parts:                        # 둘 다 무효(적자) → PBR·PSR 폴백
            for key in ("pbr", "psr"):
                v = m.get(key)
                pool = peers.get(key) or []
                p = sector_relative.percentile_score(v, pool, higher_is_better=False)
                if p is not None:
                    parts.append(p)
            if parts:
                s.flags.append("C1_pbr_psr_fallback")
        if parts:
            c1 = sum(parts) / len(parts)

    # C2 자기밴드 위치 — 자기 5년 멀티플 시계열 내 백분위 (하위일수록 고득점)
    c2 = None
    cur = m.get("ev_ebit") or m.get("pbr")
    if band_series and cur is not None:
        pool = [v for v in band_series if v is not None]
        if len(pool) >= 8:
            below = sum(1 for v in pool if v < cur) / len(pool)
            c2 = _step(1 - below, [(0.8, 5), (0.6, 4), (0.4, 3), (0.2, 2)], 1)

    # C3 이익추세 proxy — 연간 history 기반 YoY (분기 TTM 미확보 시 근사, 플래그)
    c3 = None
    hist = m.get("op_income_history")
    if hist and len(hist) >= 2 and hist[-2] is not None and hist[-1] is not None:
        prev, curr = hist[-2], hist[-1]
        if prev > 0 and curr < 0:
            c3 = 1.0                        # 적자전환
        elif prev <= 0 and curr <= 0:
            c3 = 1.5
        elif prev <= 0 < curr:
            c3 = 4.5                        # 흑자전환
        else:
            yoy = curr / prev - 1.0
            c3 = _step(yoy, [(0.20, 5), (0.05, 4), (-0.05, 3), (-0.25, 2)], 1)
        s.flags.append("C3_annual_proxy")   # TTM·QoQ 미반영 근사

    # C4 과도낙폭 — 52주 고점 대비, −25%~−45% 구간이 고점(§13.2)
    dd = m.get("drawdown_52w")
    if dd is None:
        c4 = None
    elif -0.45 <= dd <= -0.25:
        c4 = 4.5                            # D1·D2 교차 확인 후 5.0 여지
    elif -0.60 <= dd < -0.45 or -0.25 < dd <= -0.15:
        c4 = 3.0
    elif dd < -0.60:
        c4 = 2.0                            # 과도한 붕괴 — 구조훼손 의심
    else:
        c4 = 2.0                            # 낙폭 미미 — '싸진 것' 아님

    s.add("C1", c1, 0.35)
    s.add("C2", c2, 0.25)
    s.add("C3", c3, 0.25)
    s.add("C4", c4, 0.15)
    return s


def _score_D(m: dict, qual: dict | None = None,
             audit: dict | None = None) -> _Section:
    """D 밸류트랩 배제 (§13.3). qual 없으면 D2·D3는 2.5 캡(정량 사전필터)."""
    s = _Section("D")

    # D1 구조적 추세 — 매출 CAGR + 영업이익 회귀 기울기
    d1 = None
    cagr = m.get("revenue_cagr_5y")
    slope = m.get("op_income_slope")
    if cagr is not None or slope is not None:
        rev_ok = None if cagr is None else (2 if cagr >= 0.05 else 1 if cagr >= -0.02 else 0)
        op_ok = None if slope is None else (2 if slope > 0.02 else 1 if slope >= -0.02 else 0)
        pts = [p for p in (rev_ok, op_ok) if p is not None]
        avg = sum(pts) / len(pts)
        d1 = {2.0: 5.0, 1.5: 4.0, 1.0: 3.0, 0.5: 2.0, 0.0: 1.0}.get(round(avg * 2) / 2, 3.0)
        if len(pts) == 1:
            s.flags.append("D1_partial")

    d2 = _qual_score(qual, "D2")            # 급락 원인 (정성·LLM)
    d3 = _qual_score(qual, "D3")            # 산업 사양화 (정성/반정량)

    # D4 재무 생존력 — 감사의견·자본잠식
    if audit:
        opinion = audit.get("opinion", "")
        going = audit.get("going_concern_doubt", False)
        impair = audit.get("impairment", "none")
        if going or impair == "full" or opinion in ("의견거절", "부적정"):
            d4 = 1.0
        elif impair == "partial" or opinion == "한정" or audit.get("emphasis"):
            d4 = 2.0
        else:
            d4 = 5.0
    else:
        te = m.get("total_equity") if "total_equity" in m else None
        d4 = 1.0 if (te is not None and te <= 0) else None   # 완전잠식만 정량 판정 가능

    s.add("D1", d1, 0.30)
    s.add("D2", d2, 0.30)
    s.add("D3", d3, 0.20)
    s.add("D4", d4, 0.20)
    return s


def score_dhandho_quant(m: dict, peers: dict | None = None,
                        band_series: list[float] | None = None,
                        audit: dict | None = None) -> dict:
    """트리거 A 정량 사전필터 (§13.4) — LLM 이전, 계산 가능한 정량만.

    D2·D3는 2.5 캡으로 보수적 산출. 반환: A/C/D 섹션 + A_quant·D_quant.
    """
    a = _score_A(m, peers)
    c = _score_C(m, peers, band_series)
    d = _score_D(m, qual=None, audit=audit)
    return {
        "A": a.result(), "C": c.result(), "D": d.result(),
        "A_quant": a.total(), "D_quant": d.total(),
        "flags": sorted(set(a.flags + c.flags + d.flags + list(m.get("flags", [])))),
    }


def _score_B(m: dict, qual: dict | None) -> _Section:
    """B 수익성 (v1 §4): B1 ROIC(.35) · B2 마진예측(.25) · B3 추세(.15) · B4 해자(.25)🔶."""
    s = _Section("B")
    b1 = _step(m.get("roic"), [(0.15, 5), (0.10, 4), (0.05, 3), (0.0, 2)], 1)
    cv = m.get("op_margin_cv")               # 마진 변동계수 낮음 = 예측가능성 높음
    b2 = _step(-cv, [(-0.2, 5), (-0.4, 4), (-0.7, 3), (-1.0, 2)], 1) if cv is not None else None
    b3 = _step(m.get("op_income_slope"),     # 이익 추세(회귀 기울기)
               [(0.05, 5), (0.02, 4), (-0.02, 3), (-0.05, 2)], 1)
    b4 = _qual_score(qual, "B4")             # 해자 (LLM 그라운딩)
    s.add("B1", b1, 0.35)
    s.add("B2", b2, 0.25)
    s.add("B3", b3, 0.15)
    s.add("B4", b4, 0.25)
    return s


def _score_E(m: dict, shareholder: dict | None = None,
             policy: dict | None = None,
             disclosures: list[dict] | None = None) -> _Section:
    """E 주주환원 (v1 §4): E1 주주환원(.40)·E2 상법수혜(.35)·E3 촉매근접(.25).

    E1은 DART 정기보고서(배당·자기주식)로 결정론 산출:
      shareholder = {"dividend_paid": bool, "retired_any": bool,
                     "unretired_treasury": bool}
    E2·E3는 상법 타임라인(policy) 기반 — 미확보 시 2.5 캡.
      policy = {"beneficiary_score": 1~5, "catalyst_score": 1~5}
    (E1 세부 매핑은 v1 코드 대조 전 근사 — E1_heuristic 플래그)
    """
    s = _Section("E")
    e1 = None
    if shareholder is not None:
        e1 = 3.5 if shareholder.get("dividend_paid") else 2.0
        if shareholder.get("retired_any"):
            e1 += 1.0                        # 소각 실행 = 진짜 환원 (§9 한국조정)
        if shareholder.get("unretired_treasury"):
            e1 -= 0.5                        # 미소각 자사주 축적 = 감점
        e1 = _clip(e1)
        s.flags.append("E1_heuristic")
    e2 = policy.get("beneficiary_score") if policy else None
    e3 = policy.get("catalyst_score") if policy else None
    if e3 is None and disclosures:
        # 폴백: 확정 공시(자사주·공급계약) 존재 = 촉매 근접 근사
        buyback = any(("자기주식" in d.get("report_nm", "")) or ("자사주" in d.get("report_nm", ""))
                      for d in disclosures)
        supply = any("공급계약" in d.get("report_nm", "") for d in disclosures)
        e3 = 4.0 if buyback else 3.5 if supply else None
        if e3 is not None:
            s.flags.append("E3_disclosure_proxy")
    s.add("E1", e1, 0.40)
    s.add("E2", e2, 0.35)
    s.add("E3", e3, 0.25)
    return s


def _score_F(m: dict, qual: dict | None,
             insider: list[dict] | None = None) -> _Section:
    """F 내부자 (v1 §4): F1 자본배분(.35)🔶 · F2 내부자정렬(.30, 결정론) · F3 IR투명성(.35)🔶.

    F2는 임원·주요주주 소유보고(elestock) 순증감으로 결정론 산출.
    """
    s = _Section("F")
    f1 = _qual_score(qual, "F1")             # 자본배분 (LLM 그라운딩)
    f2 = None
    if insider is not None:
        changes = [t.get("change") for t in insider if t.get("change") is not None]
        if changes:
            net = sum(changes)
            f2 = 4.5 if net > 0 else 1.5 if net < 0 else 3.0
        else:
            f2 = 3.0                         # 최근 보고 없음 = 중립
    f3 = _qual_score(qual, "F3")             # IR 투명성 (LLM 그라운딩)
    s.add("F1", f1, 0.35)
    s.add("F2", f2, 0.30)
    s.add("F3", f3, 0.35)
    return s


def score_dhandho(m: dict, qual: dict | None = None, peers: dict | None = None,
                  band_series: list[float] | None = None,
                  audit: dict | None = None,
                  disclosures: list[dict] | None = None,
                  shareholder: dict | None = None,
                  insider: list[dict] | None = None,
                  policy: dict | None = None) -> dict:
    """단도 최종 스코어 (트리거 B — LLM 정성 반영 후 전체 A~F)."""
    sections = {
        "A": _score_A(m, peers),
        "B": _score_B(m, qual),
        "C": _score_C(m, peers, band_series),
        "D": _score_D(m, qual, audit),
        "E": _score_E(m, shareholder, policy, disclosures),
        "F": _score_F(m, qual, insider),
    }
    results = {k: v.result() for k, v in sections.items()}
    total = round(sum(results[k]["total"] * w
                      for k, w in config.DHANDHO_SECTION_WEIGHTS.items()), 3)
    flags = sorted(set(sum((results[k]["flags"] for k in results), [])
                       + list(m.get("flags", []))))
    return {"framework": "dhandho", "sections": results, "total": total, "flags": flags}


# ════════════════════════════════════════════════════ LTGG (§5.2, 트랙2)

def score_ltgg(m: dict, qual: dict | None = None) -> dict:
    s = _Section("LTGG")
    if (m.get("total_equity") is not None and m["total_equity"] <= 0):
        return {"framework": "ltgg", "total": 1.0, "subscores": {}, "gates": {"quality_floor": False},
                "flags": ["capital_impairment_excluded"], "grade": "제외"}

    l1 = _step(m.get("revenue_cagr_5y"),
               [(0.20, 5), (0.15, 4), (0.10, 3), (0.05, 2)], 1)
    roiic = m.get("roiic")
    wacc = config.WACC_PROXY
    l2 = _step(roiic, [(2 * wacc, 5), (1.2 * wacc, 4), (0.8 * wacc, 3), (0.0, 2)], 1) \
        if roiic is not None else None
    l3 = _qual_score(qual, "L3")            # 해자 확장성
    l4 = _qual_score(qual, "L4")            # 경영진·문화·장기지향
    gm = m.get("gross_margin_slope")
    l5 = _step(gm, [(0.02, 5), (0.0, 4), (-0.005, 3), (-0.02, 2)], 1) if gm is not None else None
    l6 = _qual_score(qual, "L6")            # 비대칭 옵셔널리티

    s.add("L1", l1, 0.25)
    s.add("L2", l2, 0.20)
    s.add("L3", l3, 0.20)
    s.add("L4", l4, 0.15)
    s.add("L5", l5, 0.10)
    s.add("L6", l6, 0.10)
    r = s.result()
    gates = {"L1": r["subscores"]["L1"]["score"] >= 3.0,
             "L2": r["subscores"]["L2"]["score"] >= 3.0}
    r["flags"].append("framework_target_mismatch_caveat")   # §10: 적용범위 밖 캐비엇 상시 유지
    return {"framework": "ltgg", "subscores": r["subscores"], "total": r["total"],
            "gates": gates, "flags": r["flags"],
            "grade": _grade(r["total"], all(gates.values()))}


# ════════════════════════════════════════════════════ 아웃사이더 (§5.3)

def score_outsiders(m: dict, qual: dict | None = None,
                    buyback: dict | None = None) -> dict:
    """buyback: {"bought": bool, "retired_ratio": float(0~1), "issued_dilution": bool}
    — 자사주 취득·소각 공시 기반(한국조정: 미소각 축적은 감점)."""
    s = _Section("OUT")

    fcf_cagr = m.get("fcf_cagr_5y")
    o1 = _step(fcf_cagr, [(0.15, 5), (0.08, 4), (0.03, 3), (0.0, 2)], 1) \
        if fcf_cagr is not None else None
    if o1 is not None:
        s.flags.append("O1_per_share_proxy")   # 주식수 시계열 미확보 → FCF 총액 CAGR proxy

    o2 = _step(m.get("roic"), [(0.15, 5), (0.10, 4), (0.07, 3), (0.03, 2)], 1)

    o3 = None
    if buyback is not None:
        if buyback.get("bought"):
            rr = buyback.get("retired_ratio", 0.0) or 0.0
            # 소각비율이 핵심(§9): 취득만 하고 안 태우면 감점
            o3 = _step(rr, [(0.8, 5), (0.5, 4), (0.2, 3)], 2.0)
        elif buyback.get("issued_dilution"):
            o3 = 1.0
        else:
            o3 = 2.5

    fm = m.get("fcf_margin")
    o4 = None
    if fm is not None:
        base = _step(fm, [(0.10, 5), (0.05, 4), (0.02, 3), (0.0, 2)], 1)
        if fcf_cagr is not None and fcf_cagr < 0:
            base = _clip(base - 1.0)
        o4 = base

    o5 = _qual_score(qual, "O5")            # 린 본사·분권화
    o6 = _qual_score(qual, "O6")            # 오너십·독립적 사고

    s.add("O1", o1, 0.25)
    s.add("O2", o2, 0.20)
    s.add("O3", o3, 0.20)
    s.add("O4", o4, 0.15)
    s.add("O5", o5, 0.10)
    s.add("O6", o6, 0.10)
    r = s.result()
    gates = {"O1": r["subscores"]["O1"]["score"] >= 3.0,
             "O3": r["subscores"]["O3"]["score"] >= 3.0}
    r["flags"].append("framework_target_mismatch_caveat")
    return {"framework": "outsiders", "subscores": r["subscores"], "total": r["total"],
            "gates": gates, "flags": r["flags"],
            "grade": _grade(r["total"], all(gates.values()))}


# ════════════════════════════════════════════════════ 버핏·멍거 (§5.5)

def score_buffett(m: dict, qual: dict | None = None) -> dict:
    s = _Section("BUF")

    # B1 해자 내구성: 정성 + GP/A·마진 안정성 (정성 없으면 정량 proxy를 2.5 캡)
    b1_q = _qual_score(qual, "B1")
    gpa = m.get("gpa")
    cv = m.get("op_margin_cv")
    b1_quant = None
    if gpa is not None and cv is not None:
        b1_quant = (_step(gpa, [(0.35, 5), (0.25, 4), (0.15, 3), (0.05, 2)], 1)
                    + _step(-cv, [(-0.2, 5), (-0.4, 4), (-0.7, 3), (-1.0, 2)], 1)) / 2
    if b1_q is not None and b1_quant is not None:
        b1 = (b1_q + b1_quant) / 2
    elif b1_q is not None:
        b1 = b1_q
    elif b1_quant is not None:
        b1 = min(b1_quant, CAP)             # 정성 근거 없이는 2.5 상한
        s.flags.append("B1_quant_only_capped")
    else:
        b1 = None

    roe_mean, roe_sd = m.get("roe_mean"), m.get("roe_stdev")
    b2 = None
    if roe_mean is not None:
        b2 = _step(roe_mean, [(0.15, 5), (0.10, 4), (0.07, 3), (0.03, 2)], 1)
        if roe_sd is not None and roe_mean != 0 and roe_sd / abs(roe_mean) > 0.5:
            b2 = _clip(b2 - 1.0)            # 변동 큼 → 감점
        if len(m.get("op_income_history") or []) < 6:
            s.flags.append("B2_short_history")   # 10y 미확보

    dr = m.get("debt_ratio")
    b3 = _step(-dr, [(-0.3, 5), (-0.6, 4), (-1.0, 3), (-2.0, 2)], 1) if dr is not None else None

    b4 = None
    nyr = m.get("fcf_negative_years")
    if nyr is not None:
        b4 = {0: 5, 1: 4, 2: 3, 3: 2}.get(nyr, 1)
        if m.get("fcf_margin") is not None and m["fcf_margin"] < 0:
            b4 = _clip(b4 - 1.0)

    gms = m.get("gross_margin_slope")
    b5 = _step(gms, [(0.01, 5), (0.0, 4), (-0.005, 3), (-0.02, 2)], 1) if gms is not None else None

    b6 = _qual_score(qual, "B6")            # 경영진 정직성 — 하드게이트
    hard_exclude = bool(qual and qual.get("B6", {}).get("tunneling_confirmed"))

    # B7 안전마진 — 밸류에이션 엔진 보류 중(§7): 멀티플·자산 기준 판정
    ev_ebit = m.get("ev_ebit")
    if ev_ebit is not None:
        b7 = _step(-ev_ebit, [(-6, 5), (-8, 4), (-10, 3), (-14, 2)], 1)
    elif m.get("pbr") is not None:
        b7 = _step(-m["pbr"], [(-0.6, 5), (-1.0, 4), (-1.5, 3), (-2.5, 2)], 1)
    else:
        b7 = None

    s.add("B1", b1, 0.20)
    s.add("B2", b2, 0.20)
    s.add("B3", b3, 0.12)
    s.add("B4", b4, 0.15)
    s.add("B5", b5, 0.13)
    s.add("B6", b6, 0.12)
    s.add("B7", b7, 0.08)
    r = s.result()
    gates = {"B1": r["subscores"]["B1"]["score"] >= 3.0,
             "B2": r["subscores"]["B2"]["score"] >= 3.0,
             "B6": r["subscores"]["B6"]["score"] >= 3.0 and not hard_exclude}
    grade = "제외" if hard_exclude else _grade(r["total"], all(gates.values()))
    if hard_exclude:
        r["flags"].append("tunneling_hard_excluded")   # §6 글로벌 거버넌스 게이트
    return {"framework": "buffett_munger", "subscores": r["subscores"], "total": r["total"],
            "gates": gates, "flags": r["flags"], "grade": grade}


# ════════════════════════════════════════════════════ 마법공식 (§5.4, 전수 랭킹)

def rank_magic_formula(metrics_by_ticker: dict[str, dict],
                       info_by_ticker: dict[str, dict] | None = None) -> dict[str, dict]:
    """EY·ROC 순위 합산 → 백분위 → 1.0~5.0. 바스켓 도구(단일 종목 BUY 아님).

    제외: 금융·유틸리티(이름 기반 근사), 최소 시총 미달, EBIT≤0, 지표 결측.
    """
    info_by_ticker = info_by_ticker or {}
    eligible: dict[str, tuple[float, float]] = {}
    results: dict[str, dict] = {}

    for t, m in metrics_by_ticker.items():
        name = (info_by_ticker.get(t) or {}).get("name", "")
        reason = None
        if any(kw in name for kw in config.MAGIC_FORMULA_EXCLUDE_SECTORS):
            reason = "sector_excluded"
        elif m.get("mktcap") is None or m["mktcap"] < config.MAGIC_FORMULA_MIN_MKTCAP:
            reason = "mktcap_below_min"
        elif m.get("operating_income") is None or m["operating_income"] <= 0:
            reason = "ebit_nonpositive"
        elif m.get("earnings_yield") is None or m.get("roc_greenblatt") is None:
            reason = "metric_missing"
        if reason:
            results[t] = {"framework": "magic_formula", "excluded": reason,
                          "score": None, "grade": "제외"}
        else:
            eligible[t] = (m["earnings_yield"], m["roc_greenblatt"])

    if not eligible:
        return results

    by_ey = sorted(eligible, key=lambda t: eligible[t][0], reverse=True)
    by_roc = sorted(eligible, key=lambda t: eligible[t][1], reverse=True)
    ey_rank = {t: i for i, t in enumerate(by_ey)}
    roc_rank = {t: i for i, t in enumerate(by_roc)}
    combined = sorted(eligible, key=lambda t: ey_rank[t] + roc_rank[t])

    n = len(combined)
    for i, t in enumerate(combined):
        pct = i / n                          # 0=최상위
        score = 5.0 if pct <= 0.10 else round(_clip(1.0 + 4.0 * (1.0 - pct)), 2)
        results[t] = {
            "framework": "magic_formula", "rank": i + 1, "of": n,
            "ey": round(eligible[t][0], 4), "roc": round(eligible[t][1], 4),
            "score": score, "excluded": None,
            "grade": "상위 관심권" if pct <= 0.10 else _grade(score, True),
            "flags": ["basket_tool_not_single_buy"],
        }
    return results


# ════════════════════════════════════════════════════ 공통

def _grade(total: float, gates_ok: bool) -> str:
    """§11 등급 (제안 임계 — 백테스트로 보정)."""
    if not gates_ok:
        return "보류"
    if total >= config.SCORE_BUY_MIN:
        return "적극/분할 후보"
    if total >= config.SCORE_WATCH_MIN:
        return "관심"
    return "제외"


def rank_universe(metrics_by_ticker: dict[str, dict], framework: str,
                  qual_by_ticker: dict[str, dict] | None = None,
                  info_by_ticker: dict[str, dict] | None = None) -> list[tuple[str, dict]]:
    """트랙2 전수 랭킹 디스패처. framework: ltgg|outsiders|buffett|magic_formula.

    반환: [(ticker, result)] — score/total 내림차순, 제외 종목은 뒤로.
    """
    qual_by_ticker = qual_by_ticker or {}
    if framework == "magic_formula":
        results = rank_magic_formula(metrics_by_ticker, info_by_ticker)
        return sorted(results.items(),
                      key=lambda kv: (kv[1].get("score") is None,
                                      -(kv[1].get("score") or 0)))
    scorer = {"ltgg": score_ltgg, "outsiders": score_outsiders,
              "buffett": score_buffett}.get(framework)
    if scorer is None:
        raise ValueError(f"unknown framework: {framework}")
    results = {t: scorer(m, qual_by_ticker.get(t)) for t, m in metrics_by_ticker.items()}
    return sorted(results.items(), key=lambda kv: -kv[1]["total"])
