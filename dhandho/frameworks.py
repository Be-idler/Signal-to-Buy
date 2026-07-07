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


# ════════════════════════════════════════════════════ 아웃사이더 (손다이크 v2 — 이원 배점)

def _mean_or_none(parts: list) -> float | None:
    vals = [p for p in parts if p is not None]
    return sum(vals) / len(vals) if vals else None


def score_outsiders(m: dict, qual: dict | None = None,
                    buyback: dict | None = None,
                    insider: list[dict] | None = None) -> dict:
    """손다이크 아웃사이더 v2 — 이원(二元) 배점.

    섹션: A 자본배분 실적(.30) · B 주당 가치 복리(.25) · C 경영진 독립성(.20, 게이트)
          · D 재무 규율(.15) · E 코리아 디스카운트 해소도(.10)
    이원 산출: 총점_적용(디스카운트 존속, E 포함) vs 총점_미적용(해소 가정,
    E 가중치를 A/B/C/D에 비례 재배분). 스프레드 = 상법 개정 재평가 옵션 가치.
    게이트: G1 터널링→제외 / G2 C<3.0→상한 보류 / G3 B<3.0→매수급 불가
          / G4 다년 FCF 미확보→상한 관심종목.
    buyback: {"bought": bool, "retired_ratio": 0~1, "issued_dilution": bool,
              "pre_mandate_retired": bool(2026.3 의무화 이전 자발적 소각)}
    """
    s = _Section("OUT")
    hard_exclude = bool(qual and (qual.get("E1", {}) or {}).get("tunneling_confirmed"))

    # ── A 자본배분 실적: A1 증분ROIC(정량) · A2 인수규율/A3 옵션전환/A4 축소용기(정성)
    wacc = config.WACC_PROXY
    roiic = m.get("roiic")
    a1 = _step(roiic, [(0.15, 5), (1.5 * wacc, 4), (wacc, 3), (0.0, 2)], 1) \
        if roiic is not None else None
    a_qual = _mean_or_none([_qual_score(qual, k) for k in ("A2", "A3", "A4")])
    oa = _mean_or_none([a1, a_qual])
    if oa is not None and a_qual is None:
        s.flags.append("OA_quant_only")      # 인수·매각 규율은 공시 대조 필요

    # ── B 주당 가치 복리: B1 주당 FCF CAGR · B2 희석 · B3 매입 기회주의성 · B4 소각
    fcf_cagr = m.get("fcf_cagr_5y")
    b1 = _step(fcf_cagr, [(0.15, 5), (0.10, 4), (0.07, 3), (0.0, 2)], 1) \
        if fcf_cagr is not None else None
    if b1 is not None:
        s.flags.append("fcf_per_share_proxy")   # 주식수 시계열 미확보 → 총액 CAGR proxy
    b2 = b3 = b4 = None
    if buyback is not None:
        if buyback.get("issued_dilution"):
            b2 = 1.5                          # 반복 희석(CB/BW/유증)
        elif buyback.get("bought"):
            b2 = 4.0
        else:
            b2 = 3.0
        if buyback.get("bought"):
            dd = m.get("drawdown_52w")
            b3 = 4.5 if (dd is not None and dd <= -0.25) else 3.0   # 저가 집중 매입 = 싱글턴식
            rr = buyback.get("retired_ratio", 0.0) or 0.0
            # 2026.3 소각 의무화 반영: 단순 의무 이행 = 3점(중립), 자발성·초과분에 가중
            b4 = _step(rr, [(0.8, 4.5), (0.5, 4.0), (0.01, 3.0)], 2.0)
            if buyback.get("pre_mandate_retired"):
                b4 = _clip(b4 + 0.5)          # 규제 이전 자발적 소각 = 진짜 신호
    ob = _mean_or_none([b1, b2, b3, b4])

    # ── C 경영진 독립성·오너십 [게이트]: 내부자 순매수(결정론) 외 정성
    c1 = None
    if insider is not None:
        changes = [t.get("change") for t in insider if t.get("change") is not None]
        net = sum(changes) if changes else 0
        c1 = 4.0 if net > 0 else 2.0 if net < 0 else 3.0
    c_qual = _mean_or_none([_qual_score(qual, k) for k in ("C2", "C3", "C4")])
    oc = _mean_or_none([c1, c_qual])

    # ── D 재무 규율·유연성: D1 부채의 도구적 사용 · D2 현금흐름의 질
    d1 = None
    ic, dr = m.get("interest_coverage"), m.get("debt_ratio")
    ncm = m.get("net_cash_to_mktcap")
    if dr is not None:
        if dr > 2.0:
            d1 = 1.5                          # 만성 과다차입
        elif ic is not None and ic >= 5 and dr < 1.0:
            d1 = 4.0                          # 여력 있는 보수적 부채
        else:
            d1 = 3.0
        if ncm is not None and ncm > 0.5:
            d1 = min(d1, 3.0)                 # 미배치 자본(현금 과다)은 중립 이하
            s.flags.append("OD_idle_cash")
    d2 = _step(m.get("cfo_to_ni"), [(1.2, 5), (1.0, 4), (0.8, 3), (0.5, 2)], 1) \
        if m.get("cfo_to_ni") is not None else None
    od = _mean_or_none([d1, d2])

    # ── E 코리아 디스카운트 해소도 (E2 환원의 질 · E3/E4 정성)
    e2 = None
    if buyback is not None:
        rr = (buyback.get("retired_ratio", 0.0) or 0.0) if buyback.get("bought") else 0.0
        e2 = 4.0 if rr >= 0.5 else 3.0 if rr > 0 else 2.0
    e_qual = _mean_or_none([_qual_score(qual, k) for k in ("E3", "E4")])
    oe = _mean_or_none([e2, e_qual])

    s.add("OA", oa, 0.30)
    s.add("OB", ob, 0.25)
    s.add("OC", oc, 0.20)
    s.add("OD", od, 0.15)
    s.add("OE", oe, 0.10)
    r = s.result()
    sub = r["subscores"]

    # 이원 배점: 적용(E 포함) vs 미적용(E 가중치를 A/B/C/D에 비례 재배분)
    total_applied = r["total"]
    total_reform = round(sub["OA"]["score"] * 0.3333 + sub["OB"]["score"] * 0.2778
                         + sub["OC"]["score"] * 0.2222 + sub["OD"]["score"] * 0.1667, 3)
    spread = round(total_reform - total_applied, 3)
    if spread >= 0.15:
        spread_note = "정책 촉매 비대칭 기회 — 구조적 할인에 갇힌 자본배분가 후보"
    elif spread <= -0.05:
        spread_note = "촉매 기대 의존 주의 — 개정 실현 시 오히려 하향 위험(역전)"
    else:
        spread_note = "스프레드 미미 — 개정은 촉매 아님, 기업 본질로 판단"

    gates = {
        "거버넌스": not hard_exclude,
        "경영진(C≥3.0)": sub["OC"]["score"] >= 3.0,
        "주당가치(B≥3.0)": sub["OB"]["score"] >= 3.0,
        "데이터(다년 FCF)": fcf_cagr is not None,
    }
    r["flags"].append("framework_target_mismatch_caveat")
    if hard_exclude:
        r["flags"].append("tunneling_hard_excluded")

    def _verdict(total: float) -> str:
        if hard_exclude or total < 2.5:
            return "제외"
        if not gates["데이터(다년 FCF)"]:
            return "관심종목 편입" if total >= 3.0 else "보류"
        if not gates["경영진(C≥3.0)"]:
            return "보류"
        if total >= 4.2 and gates["주당가치(B≥3.0)"]:
            return "적극 검토"
        if total >= 3.6 and gates["주당가치(B≥3.0)"]:
            return "분할 검토"
        if total >= 3.0:
            return "관심종목 편입"
        return "보류"

    return {"framework": "outsiders", "subscores": sub,
            "total": total_applied, "total_reform": total_reform,
            "spread": spread, "spread_note": spread_note,
            "gates": gates, "flags": r["flags"],
            "grade": _verdict(total_applied), "grade_reform": _verdict(total_reform)}


# ════════════════════════════════════════════════════ 버핏·멍거 (v1.0 프롬프트 정합)

def score_buffett(m: dict, qual: dict | None = None) -> dict:
    """버핏·멍거 — 섹션 A~E 가중 스코어 + 3중 게이트.

    A 사업 단순성·예측가능성(.15) · B 경제적 해자(.25) · C 경영진·자본배분(.20)
    · D 재무 건전성·이익의 질(.20) · E 내재가치 대비 안전마진(.20)
    게이트: G1 정직성(터널링 [Fact]→제외) / G2 능력범위(정성 — 체크리스트 위임)
          / G3 안전마진(E<3.0 = 할인 30% 미만 → 매수급 불가, 상한 관심종목)
    평균화 함정 방지: B≥3.5 그리고 C≥3.0 동시 충족 없이는 매수급 불가.
    """
    s = _Section("BUF")
    hard_exclude = bool(qual and (qual.get("G1") or qual.get("B6") or {})
                        .get("tunneling_confirmed"))

    # ── A 사업 단순성·예측가능성: 마진 변동성 + 적자 이력 (수요 지속성은 정성)
    cv = m.get("op_margin_cv")
    a1 = _step(-cv, [(-0.15, 5), (-0.30, 4), (-0.50, 3), (-0.80, 2)], 1) \
        if cv is not None else None
    hist = m.get("op_income_history") or []
    losses = sum(1 for v in hist if v is not None and v <= 0) if hist else None
    a2 = {0: 5.0, 1: 3.0}.get(losses, 1.5) if losses is not None else None
    ba = _mean_or_none([a1, a2])
    if hist and len(hist) < 6:
        s.flags.append("BA_short_history")   # 10년 변동성 미확보

    # ── B 경제적 해자: ROE≥15% 지속 + GP/A + 마진 방향. 유형 특정(정성) 없으면 3.0 상한.
    roe_mean, roe_sd = m.get("roe_mean"), m.get("roe_stdev")
    b1 = None
    if roe_mean is not None:
        b1 = _step(roe_mean, [(0.15, 5), (0.12, 4), (0.08, 3), (0.05, 2)], 1)
        if roe_sd is not None and roe_mean != 0 and roe_sd / abs(roe_mean) > 0.5:
            b1 = _clip(b1 - 1.0)             # 지속성 결여 감점
    b2 = _step(m.get("gpa"), [(0.35, 5), (0.25, 4), (0.15, 3), (0.05, 2)], 1) \
        if m.get("gpa") is not None else None
    gms = m.get("gross_margin_slope")        # 해자의 방향: 확장/침식
    b3 = _step(gms, [(0.01, 5), (0.0, 4), (-0.005, 3), (-0.02, 2)], 1) \
        if gms is not None else None
    bb = _mean_or_none([b1, b2, b3])
    moat_q = _qual_score(qual, "moat")
    if bb is not None:
        if moat_q is not None:
            bb = (bb + moat_q) / 2
        else:
            bb = min(bb, 3.0)                # 해자 유형 미특정 → 3점 초과 금지
            s.flags.append("BB_type_capped")

    # ── C 경영진·자본배분: 증분 ROIC(정량) + 1달러 테스트·환원의 질(정성/미확보)
    wacc = config.WACC_PROXY
    roiic = m.get("roiic")
    c1 = _step(roiic, [(2 * wacc, 5), (1.5 * wacc, 4), (wacc, 3), (0.0, 2)], 1) \
        if roiic is not None else None
    c_qual = _qual_score(qual, "capital_allocation")
    bc = _mean_or_none([c1, c_qual])
    if bc is not None and c_qual is None:
        bc = min(bc, 3.5)                    # 1달러 테스트·소각의 질 미검증 → 상한
        s.flags.append("BC_quant_only")

    # ── D 재무 건전성·이익의 질: 현금전환 + 부채 앵커(순부채/EBITDA·이자보상) + 듀폰
    oer = m.get("owner_earnings_ratio")      # 오너어닝스/순이익
    d1 = _step(oer, [(1.0, 5), (0.8, 4), (0.6, 3), (0.3, 2)], 1) if oer is not None else None
    nde = m.get("net_debt_to_ebitda")
    ic = m.get("interest_coverage")
    d2 = None
    if nde is not None:
        d2 = 5.0 if nde <= 0 else _step(-nde, [(-1.0, 4), (-2.5, 3), (-4.0, 2)], 1)
    elif ic is not None:
        d2 = _step(ic, [(10, 5), (5, 4), (3, 3), (1.5, 2)], 1)
    if d2 is not None and ic is not None and ic < 5:
        d2 = _clip(d2 - 1.0)                 # 이자보상 5x 미달 감점
    bd = _mean_or_none([d1, d2])
    dr = m.get("debt_ratio")
    if bd is not None and roe_mean is not None and roe_mean >= 0.12 \
            and dr is not None and dr > 1.5:
        bd = _clip(bd - 0.5)                 # 레버리지 의존 ROE 할인(듀폰)
        s.flags.append("BD_leveraged_roe")

    # ── E 내재가치 대비 안전마진: 오너어닝스 간이 DCF + 그레이엄 NCAV 교차검증
    be = None
    mos = None                               # 할인율(1 − 시총/내재가치)
    oe_abs = m.get("owner_earnings")
    mktcap = m.get("mktcap")
    if oe_abs is not None and oe_abs > 0 and mktcap:
        g = m.get("eps_cagr_5y")
        g_c = max(min(g if g is not None else 0.0, 0.05), 0.0)   # 성장 0~5% 보수 클립
        iv = oe_abs * (1 + g_c) / (0.10 - 0.025)   # 할인율 10%(국채+4%p 근사)·영구 2.5%
        mos = 1.0 - mktcap / iv if iv > 0 else None
        if mos is not None:
            be = _step(mos, [(0.50, 5), (0.40, 4), (0.30, 3), (0.15, 2)], 1)
            s.flags.append("BE_dcf_simplified")
    ncav = m.get("ncav_to_mktcap")           # 그레이엄 관점 교차검증(높은 쪽 채택)
    if ncav is not None and ncav >= 1.0:
        be = max(be or 0, 4.5)

    s.add("BA", ba, 0.15)
    s.add("BB", bb, 0.25)
    s.add("BC", bc, 0.20)
    s.add("BD", bd, 0.20)
    s.add("BE", be, 0.20)
    r = s.result()
    sub = r["subscores"]
    total = r["total"]

    gates = {
        "정직성(G1)": not hard_exclude,
        "해자(B≥3.5)": sub["BB"]["score"] >= 3.5,
        "경영진(C≥3.0)": sub["BC"]["score"] >= 3.0,
        "안전마진(E≥3.0)": sub["BE"]["score"] >= 3.0,
    }
    r["flags"].append("G2_circle_of_competence_manual")   # 능력범위는 사람 판정
    if hard_exclude:
        r["flags"].append("tunneling_hard_excluded")
        grade = "제외"
    elif total >= 4.2 and sub["BE"]["score"] >= 4.0 \
            and gates["해자(B≥3.5)"] and gates["경영진(C≥3.0)"]:
        grade = "적극 검토"
    elif total >= 3.6 and gates["안전마진(E≥3.0)"] \
            and gates["해자(B≥3.5)"] and gates["경영진(C≥3.0)"]:
        grade = "분할 검토"
    elif total >= 3.0:
        grade = "관심종목 편입"
    elif total >= 2.4:
        grade = "보류"
    else:
        grade = "제외"

    return {"framework": "buffett_munger", "subscores": sub, "total": total,
            "mos_discount": round(mos, 3) if mos is not None else None,
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
