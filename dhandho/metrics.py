"""공통 파생지표 풀 (명세서 §4) — 한 번 계산해 전 관점이 공유한다.

compute_derived(fin, mktcap=None, history=None, closes=None)
- fin:     dart.normalize_financials 출력 (최근 분기/연간)
- mktcap:  당일 시가총액 (트리거 A에서 결합 — 분기 저장 시엔 None)
- history: 과거 연간 정규화 재무 리스트 (과거→최근 순, 다년 지표용)
- closes:  최근 종가 시계열 (52주 낙폭 등)

값을 계산할 수 없으면 None을 넣는다 → 스코어링에서 2.5 상한 + 플래그(§13.0).
"""
from __future__ import annotations

import statistics

import config


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _cagr(first, last, years: int):
    if first is None or last is None or years <= 0:
        return None
    if first <= 0:                 # 음수/0 기점은 CAGR 정의 불가
        return None
    if last <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def _slope(values: list[float]) -> float | None:
    """단순 선형회귀 기울기 (연 단위, 값 스케일 정규화: 평균 절대값 대비)."""
    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return None
    n = len(vals)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(vals) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    raw = sum((x - mx) * (y - my) for x, y in zip(xs, vals)) / denom
    scale = sum(abs(v) for v in vals) / n
    return raw / scale if scale > 0 else None


def build_ttm(fin_interim: dict, prior_annual: dict | None,
              prior_same_interim: dict | None) -> dict:
    """분기/반기 보고서의 기간 항목을 TTM(직전 12개월)으로 변환.

    TTM = 직전 연간 + 당기 누적 − 전년 동기 누적.
    - BS(시점 잔액) 항목은 최신 보고서 값을 그대로 유지(가장 신선).
    - 누적치(*_cum)가 없으면 thstrm 값을 누적으로 간주(1분기는 동일,
      반기는 실데이터 검증 대상 — validate_dart.py로 확인).
    - 세 조각 중 하나라도 없으면 직전 연간으로 폴백 + 플래그
      (3개월치를 연간 지표에 그대로 쓰는 왜곡 방지).
    """
    from dhandho.dart import FLOW_KEYS
    out = dict(fin_interim)
    flags = list(fin_interim.get("flags", []))

    def _cum(fin: dict | None, key: str):
        if fin is None:
            return None
        return fin.get(key + "_cum", fin.get(key))

    for key in FLOW_KEYS:
        cur = _cum(fin_interim, key)
        ann = (prior_annual or {}).get(key)
        prev = _cum(prior_same_interim, key)
        if None not in (cur, ann, prev):
            out[key] = ann + cur - prev
        elif ann is not None:
            out[key] = ann                       # 연간 폴백 — 왜곡보다 낫다
            flags.append(f"{key}_ttm_fallback_annual")
        else:
            out[key] = None                      # 기간 불일치 값 사용 금지
            flags.append(f"{key}_flow_basis_mismatch")

    out["flags"] = sorted(set(flags))
    out["flow_basis"] = "TTM"
    return out


def compute_derived(fin: dict, mktcap: float | None = None,
                    history: list[dict] | None = None,
                    closes: list[float] | None = None) -> dict:
    m: dict = {"flags": list(fin.get("flags", []))}

    rev = fin.get("revenue")
    op = fin.get("operating_income")
    ni = fin.get("net_income_controlling") or fin.get("net_income")
    ta = fin.get("total_assets")
    tl = fin.get("total_liabilities")
    te = fin.get("total_equity")
    eq_ctrl = fin.get("equity_controlling") or te
    ca = fin.get("current_assets")
    cl = fin.get("current_liabilities")
    cash = fin.get("cash_and_equivalents")
    st_inv = fin.get("short_term_investments") or 0.0
    borrow = fin.get("total_borrowings")
    cfo = fin.get("cfo")
    capex = fin.get("capex")
    interest = fin.get("interest_expense")
    gp = fin.get("gross_profit")
    ppe = fin.get("ppe")

    # ------------------------------------------------ 재무 단독분
    m["revenue"] = rev
    m["operating_income"] = op
    m["net_income"] = ni
    m["total_equity"] = te                                    # 자본잠식 판정용 passthrough
    m["op_margin"] = _div(op, rev)
    m["net_margin"] = _div(ni, rev)
    m["gross_margin"] = _div(gp, rev)
    m["gpa"] = _div(gp, ta)                                   # GP/A
    m["roe"] = _div(ni, eq_ctrl)
    m["roa"] = _div(ni, ta)

    # FCF = 영업현금흐름 − CAPEX (capex는 취득액 절대값 취급)
    m["fcf"] = (cfo - abs(capex)) if (cfo is not None and capex is not None) else None
    m["fcf_margin"] = _div(m["fcf"], rev)

    # 오너어닝스(버핏 1986) = 순이익 + 감가상각 − 유지CAPEX.
    # 유지/성장 CAPEX 구분 불가 → 총 CAPEX 사용(보수적 추정치, 플래그).
    dep = fin.get("depreciation")
    if ni is not None and dep is not None and capex is not None:
        m["owner_earnings"] = ni + dep - abs(capex)
        m["flags"].append("owner_earnings_estimated")
    else:
        m["owner_earnings"] = None
    # 이익의 현금전환: 오너어닝스/순이익, 영업CF/순이익 (흑자일 때만 의미)
    m["owner_earnings_ratio"] = (_div(m["owner_earnings"], ni)
                                 if (ni is not None and ni > 0) else None)
    m["cfo_to_ni"] = _div(cfo, ni) if (ni is not None and ni > 0) else None

    # EBITDA ≈ 영업이익 + 감가상각 → 순부채/EBITDA (버핏 D 섹션 부채 앵커)
    m["ebitda"] = (op + dep) if (op is not None and dep is not None) else None

    cash_like = (cash + st_inv) if cash is not None else None
    m["net_cash"] = (cash_like - borrow) if (cash_like is not None and borrow is not None) else None
    m["net_debt_to_ebitda"] = (_div(-m["net_cash"], m["ebitda"])
                               if (m["net_cash"] is not None and m["ebitda"] is not None
                                   and m["ebitda"] > 0) else None)
    m["ncav"] = (ca - tl) if (ca is not None and tl is not None) else None
    m["debt_ratio"] = _div(tl, te)                            # 부채비율(총부채/자기자본)
    if borrow is None:
        m["interest_coverage"] = None                         # 차입금 파싱 실패 → 근거불충분
    elif borrow == 0:
        m["interest_coverage"] = float("inf")                 # 무차입
    elif interest in (None, 0):
        m["interest_coverage"] = None
    else:
        m["interest_coverage"] = _div(op, abs(interest))

    # ROIC = NOPAT / 투하자본 (NOPAT=영업이익×(1−유효세율 25% 근사),
    # 투하자본 = 자기자본 + 총차입금 − 현금성)
    if op is not None and te is not None and borrow is not None and cash_like is not None:
        invested = te + borrow - cash_like
        m["roic"] = _div(op * 0.75, invested) if invested and invested > 0 else None
    else:
        m["roic"] = None

    # ROC(Greenblatt) = EBIT / (순운전자본 + 순고정자산)
    if op is not None and ca is not None and cl is not None and ppe is not None:
        base = (ca - cl) + ppe
        m["roc_greenblatt"] = _div(op, base) if base and base > 0 else None
    else:
        m["roc_greenblatt"] = None

    # ------------------------------------------------ 시총 결합분
    m["mktcap"] = mktcap
    if mktcap is not None and mktcap > 0:
        m["per"] = _div(mktcap, ni) if (ni is not None and ni > 0) else None
        m["pbr"] = _div(mktcap, eq_ctrl) if (eq_ctrl is not None and eq_ctrl > 0) else None
        m["psr"] = _div(mktcap, rev) if (rev is not None and rev > 0) else None
        ev = None
        if borrow is not None and cash_like is not None:
            ev = mktcap + borrow - cash_like
        m["ev"] = ev
        m["ev_ebit"] = _div(ev, op) if (ev is not None and op is not None and op > 0) else None
        m["earnings_yield"] = _div(op, ev) if (ev is not None and ev > 0 and op is not None) else None
        m["net_cash_to_mktcap"] = _div(m["net_cash"], mktcap)
        m["ncav_to_mktcap"] = _div(m["ncav"], mktcap)
        m["fcf_yield"] = _div(m["fcf"], mktcap)
    else:
        for k in ("per", "pbr", "psr", "ev", "ev_ebit", "earnings_yield",
                  "net_cash_to_mktcap", "ncav_to_mktcap", "fcf_yield"):
            m[k] = None
        m["flags"].append("mktcap_missing")

    # ------------------------------------------------ 다년분 (history: 과거→최근 연간)
    if history:
        series = list(history) + [fin]
        revs = [h.get("revenue") for h in series]
        ops = [h.get("operating_income") for h in series]
        nis = [(h.get("net_income_controlling") or h.get("net_income")) for h in series]
        fcfs = []
        for h in series:
            c, x = h.get("cfo"), h.get("capex")
            fcfs.append((c - abs(x)) if (c is not None and x is not None) else None)

        n = len(series) - 1
        m["revenue_cagr_5y"] = _cagr(revs[0], revs[-1], n) if n >= 4 else None
        m["revenue_cagr_3y"] = _cagr(revs[-4], revs[-1], 3) if len(revs) >= 4 else None
        m["eps_cagr_5y"] = _cagr(nis[0], nis[-1], n) if n >= 4 else None   # 주식수 미확보 시 순이익 CAGR proxy
        m["fcf_cagr_5y"] = _cagr(fcfs[0], fcfs[-1], n) if n >= 4 else None
        m["fcf_negative_years"] = (sum(1 for f in fcfs[-5:] if f is not None and f < 0)
                                   if any(f is not None for f in fcfs[-5:]) else None)
        m["op_income_slope"] = _slope(ops[-5:] if len(ops) >= 5 else ops)
        m["revenue_slope"] = _slope(revs[-5:] if len(revs) >= 5 else revs)

        margins = [_div(o, r) for o, r in zip(ops, revs)]
        margins = [x for x in margins if x is not None]
        if len(margins) >= 3 and statistics.mean(margins) != 0:
            m["op_margin_cv"] = statistics.pstdev(margins) / abs(statistics.mean(margins))
            m["gross_margin_slope"] = None
        else:
            m["op_margin_cv"] = None
        gms = [_div(h.get("gross_profit"), h.get("revenue")) for h in series]
        m["gross_margin_slope"] = _slope([g for g in gms if g is not None]) \
            if sum(g is not None for g in gms) >= 3 else None

        roes = []
        for h in series:
            e = h.get("equity_controlling") or h.get("total_equity")
            nn = h.get("net_income_controlling") or h.get("net_income")
            r = _div(nn, e)
            if r is not None:
                roes.append(r)
        m["roe_mean"] = statistics.mean(roes) if roes else None
        m["roe_stdev"] = statistics.pstdev(roes) if len(roes) >= 3 else None

        # ROIIC(증분ROIC) = ΔNOPAT / Δ투하자본 (최근 3년)
        def _invested(h):
            e, b = h.get("total_equity"), h.get("total_borrowings")
            c = h.get("cash_and_equivalents")
            s = h.get("short_term_investments") or 0.0
            if e is None or b is None or c is None:
                return None
            return e + b - (c + s)
        if len(series) >= 4:
            iv0, iv1 = _invested(series[-4]), _invested(series[-1])
            op0, op1 = ops[-4], ops[-1]
            if None not in (iv0, iv1, op0, op1) and (iv1 - iv0) > 0:
                m["roiic"] = ((op1 - op0) * 0.75) / (iv1 - iv0)
            else:
                m["roiic"] = None
        else:
            m["roiic"] = None

        # 자기밴드(C2)용: EV/EBIT 대신 PBR proxy 시계열은 가격 필요 → 이익 기반 proxy 저장
        m["op_income_history"] = ops
        m["revenue_history"] = revs
    else:
        for k in ("revenue_cagr_5y", "revenue_cagr_3y", "eps_cagr_5y", "fcf_cagr_5y",
                  "fcf_negative_years", "op_income_slope", "revenue_slope",
                  "op_margin_cv", "gross_margin_slope", "roe_mean", "roe_stdev", "roiic"):
            m[k] = None
        m["flags"].append("history_missing")

    # ------------------------------------------------ 가격 시계열분
    if closes:
        peak = max(closes)
        m["drawdown_52w"] = (closes[-1] / peak - 1.0) if peak > 0 else None
    else:
        m["drawdown_52w"] = None

    return m
