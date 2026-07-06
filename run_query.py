"""질의응답 분석 파이프라인 (애드온2 §3~§6) — bot_listener·워크플로 공용.

CLI: python run_query.py <종목명|코드> <스킴> [기준일]
     예) python run_query.py 삼성전자 단도 2026-06-30

기준일 시점(PIT) 규율: 기준일에 알 수 있었던 정보만 사용(pit.py).
정성(LLM) 자동 채점 없음 — 정량 산출 + '사람이 확인할 체크리스트' 출력.
"""
from __future__ import annotations

import sys
import traceback

import datetime as dt

import config
from dhandho import (asymmetry, dart, frameworks, frameworks_ackman,
                     frameworks_lynch, gate, krx, market, metrics, notify, pit,
                     query_parser, report_format, report_labels, target_price)

_CHECKLIST_COMMON = [
    "최근 주가 부진이 일시적 악재인지, 구조적 문제인지 (공시·뉴스 원문으로 확인)",
    "경영진 이력과 내부자 지분 변동 (사업보고서 임원현황·소유보고)",
    "지배구조 리스크(터널링·관계자거래)가 있는지",
]
_CHECKLIST_SCHEME = {
    "dhandho": ["청산가치(순현금·NCAV)가 소수주주에게 실제로 실현 가능한지 "
                "(지주·터널링 구조라면 할인해서 봐야 함)"],
    "buffett": ["해자의 내구성과 경영진의 정직성을 정성적으로 검증"],
    "ltgg": ["시장 규모·침투율, 창업자의 장기지향 문화 (한국 종목에는 적용범위 밖일 수 있음)"],
    "outsiders": ["자사주를 실제로 소각하는지, 주주환원의 질"],
    "lynch": ["성장 스토리의 지속 가능성 (성장이 둔화되면 PEG가 급격히 나빠짐)"],
    "ackman": ["촉매를 누가 언제 실현할 수 있는지 (소수주주는 촉매를 만들 수 없음)"],
}


def load_universe_names() -> dict[str, str]:
    """종목명 해석용 최신 유니버스 {ticker: name}."""
    basis, _ = pit.resolve_basis_date(None)
    return {t: (r.get("name") or t) for t, r in pit.load_prices(basis).items()}


def analyze(req: dict) -> str:
    """파싱된 요청 → §5 리포트 텍스트."""
    ticker = req["ticker"]
    basis, corrected = pit.resolve_basis_date(req["date"])
    prices = pit.load_prices(basis)
    row = prices.get(ticker)
    if row is None or row.get("mktcap") is None:
        raise RuntimeError(f"{ticker}: {basis} 시세/시총 없음(거래정지?)")
    close, mktcap = row.get("close"), row["mktcap"]
    shares = (mktcap / close) if close else None

    fins, history, fin_as_of = pit.load_financials_asof(basis)
    if ticker not in fins:
        raise RuntimeError(f"{ticker}: 기준일 이전 재무 SSOT 없음")

    metrics_all = {
        t: metrics.compute_derived(f, mktcap=(prices.get(t) or {}).get("mktcap"),
                                   history=history.get(t) or None)
        for t, f in fins.items()
    }
    m = metrics_all[ticker]
    peers = {k: [x[k] for x in metrics_all.values() if x.get(k) is not None]
             for k in ("ev_ebit", "per", "pbr", "psr",
                       "net_cash_to_mktcap", "ncav_to_mktcap")}

    # PIT 공시(촉매 근거): 기준일 이전 180일 — DART 키 있을 때만
    disclosures: list[dict] = []
    corp = fins[ticker].get("corp_code")
    if config.DART_API_KEY and corp:
        try:
            bgn = f"{int(basis[:4]) - 1}{basis[4:]}" if basis[4:8] < "0630" else basis[:4] + "0101"
            disclosures = dart.get_recent_disclosures(corp, bgn, basis)
        except RuntimeError:
            pass

    asym = asymmetry.compute(m)
    flags = list(m.get("flags") or [])
    ctx = {"basis": basis, "corrected_from": req["date"] if corrected else None,
           "close": close, "mktcap": mktcap, "fin_as_of": _fin_as_of_kr(fin_as_of)}

    # 하락 요인 분해(시장 vs 개별) — 경량 2점 비교, best-effort (실패 시 생략)
    decline = _market_decline(ticker, close, basis, prices)
    if decline and decline.get("note"):
        ctx["decline_note"] = decline["note"]

    scheme = req["scheme"]
    if scheme == "dhandho":
        result = frameworks.score_dhandho(m, qual=None, peers=peers,
                                          disclosures=disclosures)
        decision = gate.decide_signal(result)
        secs = result["sections"]
        ctx["headline"] = _dhandho_headline(decision, secs)
        ctx["valuation_bullets"] = _dhandho_valuation(m, asym)
        ctx["section_title"] = "단도 6개 렌즈 평가"
        ctx["section_scores"] = [
            (report_labels.SECTION_KR[k], secs[k]["total"],
             report_labels.grade_word(secs[k]["total"])) for k in "ABCDEF"]
        ctx["score_caveat"] = ("정성 항목(해자·급락 원인·산업 전망·자본배분·IR 투명성)은 "
                               "이번 분석에서 미반영되어 보수적으로 처리됐습니다. "
                               "최종 판단에는 사람의 확인이 필요합니다.")
    elif scheme == "lynch":
        r = frameworks_lynch.score_lynch(m)
        ctx["headline"] = (f"피터 린치 관점에서 '{r['category']}' 유형으로 분류되며, "
                           f"평가 등급은 '{r['grade']}'입니다. {r['basis']}.")
        ctx["valuation_bullets"] = [
            f"카테고리: {r['category']}",
            f"PEG(주가수익성장배율): {_num(r.get('peg'))}"
            + ("  — 낮을수록 성장 대비 저평가" if r.get("peg") is not None else ""),
            f"5년 EPS 성장률: {_pct(m.get('eps_cagr_5y'))} · 부채비율: {_pct_ratio(m.get('debt_ratio'))}",
        ]
        ctx["section_scores"] = [("종합 평가", r["score"],
                                  report_labels.grade_word(r["score"]))]
        ctx["score_caveat"] = r["limits"]
        flags += r["flags"]
    elif scheme == "ackman":
        r = frameworks_ackman.score_ackman(m, disclosures)
        ctx["headline"] = (f"빌 애크먼 관점 종합 평가는 '{r['grade']}'입니다"
                           + (f" — {r['labels'][0]}." if r["labels"] else "."))
        ev = r["catalyst_evidence"]
        ctx["valuation_bullets"] = [
            f"기업 퀄리티: {r['quality']:.1f}점 · 촉매 실현성: {r['catalyst']:.1f}점",
            f"FCF 마진: {_pct(m.get('fcf_margin'))} · 자본수익성(ROIC): {_pct(m.get('roic'))}",
            ("촉매 근거: " + "; ".join(ev[:3])) if ev else "촉매를 뒷받침할 공시 근거가 없습니다.",
        ]
        ctx["section_scores"] = [("종합 평가", r["total"],
                                  report_labels.grade_word(r["total"]))]
        ctx["score_caveat"] = r["limits"]
        flags += r["flags"]
    else:                                        # ltgg / outsiders / buffett
        r, bullets = _score_other(scheme, m)
        ctx["headline"] = (f"{req['scheme_label']} 관점 종합 평가는 '{r['grade']}'입니다"
                           f" (종합 {r['total']:.1f}점 / 5점 만점).")
        ctx["valuation_bullets"] = bullets
        subs = r.get("subscores") or {}
        ctx["section_title"] = f"{req['scheme_label']} 항목별 평가"
        ctx["section_scores"] = [
            (report_labels.SUBSCORE_KR.get(k, k), v["score"],
             report_labels.grade_word(v["score"])) for k, v in subs.items()]
        ctx["score_caveat"] = ("정성 하위지표는 미반영되어 보수적으로 처리됐습니다. "
                               "근거 확보 후 재평가가 필요합니다.")
        flags += r.get("flags") or []

    evidence = (frameworks_ackman.catalyst_score(m, disclosures)[1]
                if scheme in ("dhandho", "ackman") else [])
    tp = target_price.compute(scheme, m, close, shares, asym=asym,
                              catalyst_evidence=evidence)
    ctx["entry"] = tp["entry"]
    ctx["targets"] = tp["targets"]
    ctx["assumptions"] = tp["assumptions"]
    ctx["checklist"] = _CHECKLIST_COMMON + _CHECKLIST_SCHEME.get(scheme, [])
    ctx["data_status"] = report_labels.translate_flags(flags)
    return report_format.build(req, ctx)


def _fin_as_of_kr(s: str) -> str:
    """'2026 1분기보고서(법정기한 …)' → 사람이 읽는 문장으로 다듬기."""
    return (s.replace("법정기한 ", "").replace(" 기준 추정)", " 이전 공시분)")
             .replace("손익 TTM 변환", "손익은 최근 12개월 환산"))


def _dhandho_headline(decision: dict, secs: dict) -> str:
    verdict_kr = report_labels.VERDICT_KR.get(decision["verdict"], decision["verdict"])
    total = decision["total"]
    if decision["verdict"] == "BUY":
        return (f"정량 기준으로 '{verdict_kr}'입니다. 하방보호와 밸류트랩 배제 기준을 "
                f"통과했고 종합 점수도 기준선을 넘었습니다 (종합 {total:.1f}점 / 5점 만점).")
    weak = []
    if secs["A"]["total"] < 3.0:
        weak.append("안전마진(하방보호)")
    if secs["D"]["total"] < 3.0:
        weak.append("안정성(밸류트랩 배제)")
    if secs["C"]["total"] < 3.0:
        weak.append("저평가 매력")
    weak_txt = "·".join(weak) if weak else "일부 항목"
    return (f"정량 기준 '{verdict_kr}' 대상입니다. {weak_txt}이(가) 단도 기준선에 못 미쳐, "
            f"현재 가격대에서는 매수 근거가 약합니다 (종합 {total:.1f}점 / 5점 만점).")


def _dhandho_valuation(m: dict, asym: dict) -> list[str]:
    out = []
    nc = m.get("net_cash_to_mktcap")
    if nc is None:
        out.append("순현금 상태: 계산 불가")
    elif nc >= 0:
        out.append(f"순현금 상태: 순현금 우위 — 보유 순현금이 시가총액의 약 {nc:.0%}")
    else:
        out.append(f"순현금 상태: 순부채 우위 — 순차입금이 시가총액의 약 {abs(nc):.0%}")
    nv = m.get("ncav_to_mktcap")
    if nv is None:
        out.append("청산가치(NCAV): 계산 불가")
    elif nv >= 0:
        out.append(f"청산가치(유동자산−총부채): 시가총액의 약 {nv:.0%}")
    else:
        out.append(f"청산가치(유동자산−총부채): 시가총액의 약 {nv:.0%} — "
                   f"유동자산으로 총부채를 감당하지 못하는 구조")
    per, ee = m.get("per"), m.get("ev_ebit")
    if per is None and ee is None:
        out.append("이익 대비 밸류에이션(PER·EV/EBIT): 산출 불가 — 이번 분기 손익 데이터가 "
                   "아직 채워지지 않았습니다")
    else:
        parts = []
        if per is not None:
            parts.append(f"PER {per:.1f}배")
        if ee is not None:
            parts.append(f"EV/EBIT {ee:.1f}배")
        out.append("이익 대비 밸류에이션: " + " · ".join(parts))
    out.append("업사이드/다운사이드 비대칭: "
               + asymmetry.verdict(asym.get("ratio"), asym.get("negative_risk")))
    return out


def _score_other(scheme: str, m: dict) -> tuple[dict, list[str]]:
    scorer = {"ltgg": frameworks.score_ltgg, "outsiders": frameworks.score_outsiders,
              "buffett": frameworks.score_buffett}[scheme]
    r = scorer(m)
    bullets = {
        "ltgg": [f"5년 매출 성장률(CAGR): {_pct(m.get('revenue_cagr_5y'))}",
                 f"증분 자본수익(ROIIC): {_pct(m.get('roiic'))}"],
        "outsiders": [f"FCF 수익률: {_pct(m.get('fcf_yield'))} · "
                      f"5년 FCF 성장률: {_pct(m.get('fcf_cagr_5y'))}",
                      f"자본수익성(ROIC): {_pct(m.get('roic'))}"],
        "buffett": [f"오너어닝스 수익률(≈FCF/시총): {_pct(m.get('fcf_yield'))}",
                    f"평균 자기자본이익률(ROE): {_pct(m.get('roe_mean'))} · "
                    f"EV/EBIT: {_num(m.get('ev_ebit'))}배"],
    }[scheme]
    gates = r.get("gates") or {}
    if gates:
        passed = all(gates.values())
        bullets.append("핵심 관문(게이트): "
                       + ("통과" if passed else "미통과"))
    return r, bullets


def _num(x) -> str:
    return f"{x:.1f}" if isinstance(x, (int, float)) else "산출 불가"


def _pct_ratio(x) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "—"


def _market_decline(ticker: str, close, basis: str, end_prices: dict,
                    lookback_days: int = 30) -> dict | None:
    """기준일과 약 lookback_days일 전 스냅샷 2점으로 시장 요인 분해.

    지수 데이터를 따로 받지 않고 전 종목 시총 합 변화를 시장 프록시로 쓴다.
    KRX 조회 ≤5회(휴장일 보정)로 가볍게. 실패하면 None(리포트에서 생략).
    """
    try:
        d = dt.date(int(basis[:4]), int(basis[4:6]), int(basis[6:8]))
        start_d = d - dt.timedelta(days=lookback_days)
        start_rows = None
        for _ in range(5):                      # 휴장일이면 하루씩 당겨 재시도
            rows = krx.get_market_snapshot(start_d.strftime("%Y%m%d"))
            if rows:
                start_rows = rows
                break
            start_d -= dt.timedelta(days=1)
        if not start_rows or not close:
            return None
        srow = next((r for r in start_rows if r.get("ticker") == ticker), None)
        if not srow or not srow.get("close"):
            return None
        stock_change = close / srow["close"] - 1.0
        mkt_change = market.two_point_change(start_rows, list(end_prices.values()))
        weeks = max(1, (d - start_d).days // 7)
        return market.assess_decline(stock_change, mkt_change, None,
                                     window_label=f"최근 약 {weeks}주")
    except Exception:                            # noqa: BLE001 — 부가정보라 실패 무시
        return None


def analyze_text(text: str) -> str:
    universe = load_universe_names()
    req = query_parser.parse(text, universe)
    return analyze(req)


def _f(x) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "—"


def _pct(x) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "—"


def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print(query_parser.USAGE)
        return 1
    try:
        report = analyze_text(text)
        notify.send_bot1(report)
        print(report)
        return 0
    except query_parser.ParseError as e:
        msg = report_format.usage_error(str(e))
        notify.send_bot1(msg)
        print(msg)
        return 1
    except Exception:
        notify.notify_failure("run_query", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
