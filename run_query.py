"""질의응답 분석 파이프라인 (애드온2 §3~§6) — bot_listener·워크플로 공용.

CLI: python run_query.py <종목명|코드> <스킴> [기준일]
     예) python run_query.py 삼성전자 단도 2026-06-30

기준일 시점(PIT) 규율: 기준일에 알 수 있었던 정보만 사용(pit.py).
정성(LLM) 자동 채점 없음 — 정량 산출 + '사람이 확인할 체크리스트' 출력.
"""
from __future__ import annotations

import sys
import traceback

import config
from dhandho import (asymmetry, dart, frameworks, frameworks_ackman,
                     frameworks_lynch, gate, metrics, notify, pit,
                     query_parser, report_format, target_price)

_CHECKLIST_COMMON = [
    "급락/부진 원인이 일회성인지(D2) — 최근 공시·뉴스 원문 확인",
    "경영진 이력·내부자 거래(F) — DART 임원현황·소유보고",
    "지배구조 이벤트(터널링·관계자거래) 여부",
]
_CHECKLIST_SCHEME = {
    "dhandho": ["자산 바닥(NCAV·순현금)의 소수주주 실현 가능성 — 지주/터널링 구조 할인"],
    "buffett": ["해자 내구성(B1)·경영진 정직성(B6) 정성 검증"],
    "ltgg": ["TAM·침투율, 창업자·장기지향 문화(정성) — 한국 종목 적용범위 캐비엇"],
    "outsiders": ["자사주 소각률·환원의 질(§9 한국조정)"],
    "lynch": ["성장 스토리의 지속성 — 성장 둔화 시 PEG 이중 타격"],
    "ackman": ["촉매의 실현 주체·타임라인 — 소수주주는 촉매를 만들 수 없음"],
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
    ctx = {"basis": basis, "corrected_from": req["date"] if corrected else None,
           "close": close, "mktcap": mktcap, "fin_as_of": fin_as_of,
           "flags": list(m.get("flags") or [])}

    scheme = req["scheme"]
    if scheme == "dhandho":
        result = frameworks.score_dhandho(m, qual=None, peers=peers,
                                          disclosures=disclosures)
        decision = gate.decide_signal(result)
        secs = result["sections"]
        ctx["valuation_lines"] = [
            f"NCAV/시총 {_f(m.get('ncav_to_mktcap'))} · 순현금/시총 {_f(m.get('net_cash_to_mktcap'))}",
            f"EV/EBIT {_f(m.get('ev_ebit'))} · PER {_f(m.get('per'))} (시장 백분위 C1={secs['C']['subscores']['C1']['score']})",
            f"비대칭: {asymmetry.verdict(asym.get('ratio'), asym.get('negative_risk'))}",
        ]
        ctx["points"] = [
            f"[사실] 섹션 " + " ".join(f"{k}={secs[k]['total']:.2f}" for k in "ABCDEF"),
            f"[사실] 정량 판정 {decision['verdict']} — {decision['reason']}",
            "[유추] LLM 정성(B4·D2·D3·F1·F3) 미적용(2.5 캡) — 최종 판정은 일일 파이프라인/사람",
        ]
    elif scheme == "lynch":
        r = frameworks_lynch.score_lynch(m)
        ctx["valuation_lines"] = [
            f"카테고리: {r['category']} · PEG {_f(r.get('peg'))} · PER {_f(m.get('per'))}",
            f"EPS 성장(5y) {_pct(m.get('eps_cagr_5y'))} · 부채비율 {_f(m.get('debt_ratio'))}",
        ]
        ctx["points"] = [f"[사실] {r['basis']}", f"[사실] 점수 {r['score']} [{r['grade']}]",
                         f"[유추] {r['limits']}"]
        ctx["flags"] += r["flags"]
    elif scheme == "ackman":
        r = frameworks_ackman.score_ackman(m, disclosures)
        ctx["valuation_lines"] = [
            f"퀄리티 {r['quality']} × 촉매 {r['catalyst']} → 총점 {r['total']} [{r['grade']}]",
            f"FCF마진 {_pct(m.get('fcf_margin'))} · ROIC {_pct(m.get('roic'))} · 부채비율 {_f(m.get('debt_ratio'))}",
        ]
        ctx["points"] = ([f"[사실] 촉매 근거: {e}" for e in r["catalyst_evidence"][:3]]
                         or ["[사실] 촉매 공시 근거 없음"])
        ctx["points"] += [f"[유추] {lbl}" for lbl in r["labels"]]
        ctx["points"].append(f"[유추] {r['limits']}")
        ctx["flags"] += r["flags"]
    else:                                        # ltgg / outsiders / buffett
        scorer = {"ltgg": frameworks.score_ltgg,
                  "outsiders": frameworks.score_outsiders,
                  "buffett": frameworks.score_buffett}[scheme]
        r = scorer(m)
        key_lines = {
            "ltgg": f"매출CAGR(5y) {_pct(m.get('revenue_cagr_5y'))} · ROIIC {_pct(m.get('roiic'))}",
            "outsiders": f"FCF수익률 {_pct(m.get('fcf_yield'))} · FCF CAGR {_pct(m.get('fcf_cagr_5y'))} · ROIC {_pct(m.get('roic'))}",
            "buffett": f"오너어닝스수익률≈FCF/시총 {_pct(m.get('fcf_yield'))} · ROE(평균) {_pct(m.get('roe_mean'))} · EV/EBIT {_f(m.get('ev_ebit'))}",
        }
        gates = r.get("gates") or {}
        ctx["valuation_lines"] = [key_lines[scheme]]
        ctx["points"] = [
            f"[사실] 총점 {r['total']:.2f} [{r['grade']}] · 게이트 "
            + " ".join(f"{k}:{'✓' if v else '✗'}" for k, v in gates.items()),
            "[유추] 정성 하위지표 미적용(2.5 캡) — 근거 확보 후 재평가 필요",
        ]
        ctx["flags"] += r.get("flags") or []

    evidence = ([e for e in
                 (frameworks_ackman.catalyst_score(m, disclosures)[1])]
                if scheme in ("dhandho", "ackman") else [])
    tp = target_price.compute(scheme, m, close, shares, asym=asym,
                              catalyst_evidence=evidence)
    ctx["entry"] = tp["entry"]
    ctx["targets"] = tp["targets"]
    ctx["assumptions"] = tp["assumptions"]
    ctx["checklist"] = _CHECKLIST_COMMON + _CHECKLIST_SCHEME.get(scheme, [])
    return report_format.build(req, ctx)


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
