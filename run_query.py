"""질의응답 — 특정 종목 × 투자스킴 온디맨드 분석 → 텔레그램 (🔎 헤더).

사용: python run_query.py <ticker> <framework>
  framework: dhandho | ltgg | outsiders | buffett | magic_formula

저장된 분기 SSOT(+TTM 변환) + 최근 거래일 시총으로 정량 분석만 수행한다.
LLM 정성 항목은 미적용(2.5 캡) — 정성 포함 판정은 일일 파이프라인 몫.
"""
from __future__ import annotations

import datetime as dt
import sys
import traceback

from dhandho import frameworks, gate, krx, metrics, notify
from run_trigger_a import _load_financials

SCHEME_LABEL = {"dhandho": "단도투자", "ltgg": "LTGG", "outsiders": "아웃사이더",
                "buffett": "버핏멍거", "magic_formula": "마법공식"}


def _fmt_subs(subs: dict) -> str:
    return " ".join(f"{k}={v['score']}" for k, v in subs.items())


def _analyze(ticker: str, framework: str) -> str:
    # 최근 거래일 시세 (이름·시총 기준일)
    basis_day = krx.recent_trading_days(dt.date.today(), 1)[-1]
    snapshot = krx.get_market_snapshot(basis_day)
    info = {r["ticker"]: r for r in snapshot}
    if ticker not in info:
        raise RuntimeError(f"{ticker}: KRX 시세에 없음 (상장폐지/코드 오류?)")
    name = info[ticker].get("name") or ticker

    fin_by_ticker, history, _ = _load_financials()
    if ticker not in fin_by_ticker:
        raise RuntimeError(f"{ticker}: 저장된 재무 없음 (run_quarterly 적재 범위 밖)")

    # 전 종목 지표 (peer pool·마법공식 랭킹용)
    metrics_all = {
        t: metrics.compute_derived(f, mktcap=(info.get(t) or {}).get("mktcap"),
                                   history=history.get(t) or None)
        for t, f in fin_by_ticker.items()
    }
    m = metrics_all[ticker]
    peers = {k: [x[k] for x in metrics_all.values() if x.get(k) is not None]
             for k in ("ev_ebit", "per", "pbr", "psr",
                       "net_cash_to_mktcap", "ncav_to_mktcap")}

    header = notify.header_query(name, SCHEME_LABEL[framework], basis_day)
    lines = [header, f"종목코드 {ticker} | 시총 {m.get('mktcap'):,.0f}원"
             if m.get("mktcap") else f"종목코드 {ticker}"]

    if framework == "dhandho":
        result = frameworks.score_dhandho(m, qual=None, peers=peers)
        decision = gate.decide_signal(result)
        secs = result["sections"]
        lines.append("섹션: " + " ".join(f"{k}={secs[k]['total']:.2f}" for k in "ABCDEF"))
        lines.append(f"총점 {decision['total']:.2f} → {decision['verdict']} "
                     f"({decision['reason']})")
        for k in "ABCDEF":
            lines.append(f"  [{k}] " + _fmt_subs(secs[k]["subscores"]))
        lines.append("※ LLM 정성(B4·D2·D3·F1·F3) 미적용 — 해당 항목 2.5 캡")
    elif framework == "magic_formula":
        ranked = frameworks.rank_magic_formula(metrics_all, info)
        r = ranked.get(ticker, {})
        if r.get("excluded"):
            lines.append(f"제외: {r['excluded']}")
        else:
            lines.append(f"랭킹 {r['rank']}/{r['of']} | 점수 {r['score']} "
                         f"| EY {r['ey']:.1%} ROC {r['roc']:.1%} [{r['grade']}]")
            lines.append("※ 바스켓 랭킹 도구 — 단일 종목 확신 아님")
    else:
        scorer = {"ltgg": frameworks.score_ltgg,
                  "outsiders": frameworks.score_outsiders,
                  "buffett": frameworks.score_buffett}[framework]
        r = scorer(m)
        lines.append(f"총점 {r['total']:.2f} [{r['grade']}] | 게이트 "
                     + " ".join(f"{k}:{'✓' if v else '✗'}"
                                for k, v in (r.get("gates") or {}).items()))
        if r.get("subscores"):
            lines.append("하위: " + _fmt_subs(r["subscores"]))
        lines.append("※ 정성 하위지표 미적용(2.5 캡)")

    flags = m.get("flags") or []
    if flags:
        lines.append(f"플래그: {', '.join(sorted(set(flags))[:8])}")
    lines.append("※ 미검증 임계 기반 참고 정보 — 최종 판단은 사람")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[2] not in SCHEME_LABEL:
        print("usage: python run_query.py <ticker> "
              "<dhandho|ltgg|outsiders|buffett|magic_formula>")
        return 1
    ticker, framework = sys.argv[1].strip(), sys.argv[2].strip()
    try:
        text = _analyze(ticker, framework)
        notify.send_bot1(text)
        print(text)
        return 0
    except Exception:
        notify.notify_failure("run_query", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
