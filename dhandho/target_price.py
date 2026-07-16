"""스킴별 적정 매수가·기간별 목표가 산법 (애드온2 §6).

모든 목표가에 가정(성장률·멀티플)을 1줄 병기 — 숫자만 던지지 않는다.
산출값은 "모델 시나리오"이며 투자 자문이 아님(리포트 말미 고지, report_format).
"""
from __future__ import annotations

import statistics

from dhandho import asymmetry

NO_TARGET = "산출 안 함"


def _per_share(value_mktcap: float | None, shares: float | None) -> float | None:
    if value_mktcap is None or not shares or shares <= 0:
        return None
    return value_mktcap / shares


def _won(x: float | None) -> str:
    return f"{x:,.0f}원" if x is not None else "산출 불가"


def compute(scheme: str, m: dict, close: float | None, shares: float | None,
            asym: dict | None = None,
            catalyst_evidence: list[str] | None = None,
            closes: list[float] | None = None) -> dict:
    """반환: {"entry": str, "targets": {"6개월","1년","3년"}, "assumptions": [str]}."""
    fn = {"dhandho": _dhandho, "buffett": _buffett, "lynch": _lynch,
          "outsiders": _outsiders, "ltgg": _ltgg, "ackman": _ackman}.get(scheme)
    if fn is None:
        return {"entry": "미지원 스킴", "targets": {}, "assumptions": []}
    return fn(m, close, shares, asym or asymmetry.compute(m),
              catalyst_evidence or [], closes or [])


def _dhandho(m, close, shares, asym, evidence, closes):
    """바닥가 + MOS 15~30% 이내 & 비대칭 ≥2:1 충족가 (애드온1 §1.4 연동)."""
    bottom = _per_share(asym.get("bottom_mktcap"), shares)
    upside = _per_share(asym.get("upside_mktcap"), shares)
    assumptions = [f"바닥: {asym.get('bottom_basis')}",
                   f"업사이드: {asym.get('upside_basis')}"]
    if bottom is None or bottom <= 0:
        return {"entry": "산출 불가(자산 바닥 없음/음수)", "targets": {},
                "assumptions": assumptions}
    band_lo, band_hi = bottom * 1.15, bottom * 1.30
    if upside is not None:
        ratio2 = (upside + 2 * bottom) / 3          # 비대칭 2:1 등가가격
        band_hi = min(band_hi, ratio2)
        assumptions.append(f"비대칭 2:1 등가 가격 {_won(ratio2)}")
    entry = (f"{_won(band_lo)} ~ {_won(band_hi)} (바닥 +15~30% & 비대칭≥2:1)"
             if band_hi >= band_lo else
             f"{_won(band_hi)} 이하 (비대칭 2:1 제약이 우선)")
    # 자산가치 바닥이 현재가를 크게 밑돌면(우량·고평가형) 단도 딥밸류 산법의
    # 매수영역이 성립하지 않는다 — 오해 없도록 명시하고 목표가 표기도 중립화한다.
    deep_value_fit = not (close and band_hi < close * 0.5)
    if not deep_value_fit:
        entry += (f" — 현재가({_won(close)})를 크게 밑돎: "
                  "자산가치 기반 매수영역 아님(단도 딥밸류 부적합)")
    upside_below = bool(close and upside is not None and upside <= close)
    if evidence and upside and not upside_below:
        six = f"{_won(upside)} 수렴 가능 (확정 촉매 존재)"
    elif upside_below:
        six = (f"{NO_TARGET} — 정상화이익 추정가({_won(upside)})가 현재가 이하 "
               "(단도 수렴 시나리오 부적용)")
    else:
        six = f"{NO_TARGET} — 확정 촉매 없음"
    three = (f"{_won(upside)} (정상화이익×보수 멀티플 수렴 시나리오)"
             if not upside_below else
             f"{NO_TARGET} — 추정가({_won(upside)})가 현재가 이하 (시나리오 부적용)")
    targets = {
        "6개월": six,
        "1년": NO_TARGET + " — 단도는 2~3년 수렴 전제",
        "3년": three,
    }
    return {"entry": entry, "targets": targets, "assumptions": assumptions}


def _buffett(m, close, shares, asym, evidence, closes):
    """오너어닝스 기대수익률 ≥15% 역산가. 멀티플 재평가 미가정."""
    oe = m.get("fcf")                                # 오너어닝스 ≈ FCF (조정 근사)
    g = m.get("fcf_cagr_5y")
    assumptions = ["오너어닝스 ≈ FCF(TTM) 근사", "멀티플 재평가 미가정"]
    if oe is None or oe <= 0:
        return {"entry": "산출 불가(FCF 없음/음수)", "targets": {},
                "assumptions": assumptions}
    entry_ps = _per_share(oe / 0.15, shares)
    g_c = max(min(g if g is not None else 0.0, 0.15), 0.0)   # 성장 0~15% 보수 클립
    assumptions.append(f"오너어닝스 성장 {g_c:.0%}/년 (5y CAGR 보수 클립)")
    t1 = _per_share(oe * (1 + g_c) / 0.15, shares)
    t3 = _per_share(oe * (1 + g_c) ** 3 / 0.15, shares)
    return {"entry": f"{_won(entry_ps)} (기대수익률 15% 역산)",
            "targets": {"6개월": NO_TARGET + " — 복리 경로만 산출",
                        "1년": f"{_won(t1)} (성장 복리)",
                        "3년": f"{_won(t3)} (성장 복리)"},
            "assumptions": assumptions}


def _lynch(m, close, shares, asym, evidence, closes):
    """PEG=1 등가 PER × EPS (카테고리 보정)."""
    ni = m.get("net_income")
    g = m.get("eps_cagr_5y")
    assumptions = []
    if ni is None or ni <= 0 or g is None or g <= 0 or not shares:
        return {"entry": "산출 불가(적자/성장률 미확보 — PEG 무의미)",
                "targets": {}, "assumptions": ["린치 §4.1: 자산주/턴어라운드 루브릭 참조"]}
    eps = ni / shares
    fair_per = max(5.0, min(g * 100, 25.0))          # PEG=1 등가 PER, [5,25] 클립
    assumptions.append(f"PEG=1 등가 PER {fair_per:.0f} (EPS 성장 {g:.0%}, 5~25 클립)")
    entry = eps * fair_per
    t6 = eps * (1 + g) ** 0.5 * fair_per
    t1 = eps * (1 + g) * fair_per
    t3 = eps * (1 + g) ** 3 * fair_per * 0.9
    assumptions.append("3년: 성장 지속 + 카테고리 이동 위험 10% 할인")
    return {"entry": f"{_won(entry)} (PEG=1 등가)",
            "targets": {"6개월": f"{_won(t6)} (EPS 성장경로 × PEG 유지)",
                        "1년": f"{_won(t1)} (동일 가정)",
                        "3년": f"{_won(t3)}"},
            "assumptions": assumptions}


def _outsiders(m, close, shares, asym, evidence, closes):
    """FCF수익률 상위 진입가 + FCF/주 성장·소각 복리."""
    fcf = m.get("fcf")
    g = m.get("fcf_cagr_5y")
    assumptions = ["진입 기준 FCF수익률 8% (업종 80%ile 미확보 시 고정 근사 — 플래그)"]
    if fcf is None or fcf <= 0:
        return {"entry": "산출 불가(FCF 없음/음수)", "targets": {},
                "assumptions": assumptions}
    entry_ps = _per_share(fcf / 0.08, shares)
    g_c = max(min(g if g is not None else 0.0, 0.20), 0.0)
    assumptions.append(f"FCF/주 성장 {g_c:.0%}/년 (소각 효과는 공시 확인 후 가산)")
    t1 = _per_share(fcf * (1 + g_c) / 0.08, shares)
    t3 = _per_share(fcf * (1 + g_c) ** 3 / 0.08, shares)
    return {"entry": f"{_won(entry_ps)} (FCF수익률 8% 역산)",
            "targets": {"6개월": NO_TARGET,
                        "1년": f"{_won(t1)} (FCF/주 복리)",
                        "3년": f"{_won(t3)} (FCF/주 복리)"},
            "assumptions": assumptions}


def _ltgg(m, close, shares, asym, evidence, closes):
    """분할매수 밴드 + 5년 목표가(기본).

    LTGG(베일리 기포드)는 단기 정밀 목표가를 부정하고 장기(5년+) 성장에
    베팅한다. 따라서 목표가는 '5년 목표가'를 기본으로 산출하고, 단기
    (6개월·1년·3년)는 산출하지 않는다. 5년 목표가는 현재가에 매출 5년
    CAGR을 5년 복리한 성장 시나리오(밸류에이션 배수 유지 가정)다.
    """
    assumptions = ["LTGG는 단기 목표가를 부정 — 5년 성장 목표가를 기본으로 제시"]
    if close is None:
        return {"entry": "산출 불가(종가 미확보)", "targets": {}, "assumptions": assumptions}

    # 분할매수 밴드 (변동성 기반)
    if len(closes) >= 20:
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))
                if closes[i - 1]]
        vol = statistics.pstdev(rets) * (252 ** 0.5) if rets else 0.4
    else:
        vol = 0.4
        assumptions.append("변동성 시계열 부족 → 연 40% 가정")
    lo, hi = close * (1 - 0.5 * vol), close * (1 + 0.25 * vol)

    # 5년 목표가: 매출 5년 CAGR로 주가가 성장에 연동(배수 유지 가정)
    g = m.get("revenue_cagr_5y")
    if g is not None:
        g_c = max(min(g, 0.40), 0.0)                 # 성장 0~40%/년 보수 클립
        mult = (1 + g_c) ** 5
        t5 = close * mult
        five_year = (f"{_won(t5)} (5년 매출 CAGR {g_c:.0%} 지속 → 약 {mult:.1f}배 "
                     f"시나리오 · 밸류에이션 배수 유지 가정 · 확률은 사람이 판정)")
        assumptions.append("5년 목표가 = 현재가 × (1+매출CAGR)^5 (성장 0~40% 클립, 배수 불변)")
    else:
        five_year = "산출 불가(매출 성장률 미확보)"

    return {"entry": f"{_won(lo)} ~ {_won(hi)} 분할매수 밴드 (연변동성 {vol:.0%} 기반)",
            "targets": {"6개월": f"{NO_TARGET} — 단기 목표가는 산출하지 않음(장기 성장 전제)",
                        "1년": f"{NO_TARGET} — 동일",
                        "3년": f"{NO_TARGET} — 동일",
                        "5년": five_year},
            "assumptions": assumptions}


def _ackman(m, close, shares, asym, evidence, closes):
    """촉매 미반영가. 촉매 근거 없으면 목표가 산출 거부."""
    if not evidence:
        return {"entry": "목표가 산출 거부 — 촉매 공시 근거 없음(§4.2 보수 원칙)",
                "targets": {}, "assumptions": ["촉매(소각·5%룰·행동주의 공시) 확인 후 재질의"]}
    upside = _per_share(asym.get("upside_mktcap"), shares)
    assumptions = [f"업사이드: {asym.get('upside_basis')}",
                   f"촉매 근거: {'; '.join(evidence[:3])}"]
    return {"entry": f"{_won(close)} 이하 (촉매 미반영 현재가 기준 — SOTP 정밀산출은 수동)",
            "targets": {"6개월": NO_TARGET,
                        "1년": f"{_won(upside)} (촉매 실현 시나리오)",
                        "3년": f"{_won(upside)} (촉매 타임라인 연동)"},
            "assumptions": assumptions}
