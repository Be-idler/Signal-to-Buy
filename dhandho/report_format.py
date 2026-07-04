"""질의응답 리포트 포맷터 (애드온2 §5) + HEADER_QUERY 상수.

규칙(§2.1): 모든 질의응답 회신(ACK·오류 포함)은 이 모듈의 포맷터를 거친다.
호출부에서 헤더 문자열을 직접 써넣는 것은 금지.
헤더에 기준일 필수 — 나중에 대화 기록을 되짚을 때 시점을 알 수 있어야 한다.
"""
from __future__ import annotations

from dhandho.notify import fmt_date

HEADER_QUERY = "🔎 {name} {scheme} 방식 분석 ({date} 기준)"

DISCLAIMER = ("⚠️ 본 결과는 모델 시나리오이며 투자 자문이 아님. "
              "프레임워크 평결은 병렬 해석(평균 금지).")


def header(name: str, scheme_label: str, basis_date: str) -> str:
    return HEADER_QUERY.format(name=name, scheme=scheme_label,
                               date=fmt_date(basis_date))


def ack(name: str, scheme_label: str, basis_date: str) -> str:
    return (header(name, scheme_label, basis_date)
            + "\n⏳ 분석 중… (최대 2~3분)")


def error(name: str, scheme_label: str, basis_date: str, message: str) -> str:
    return header(name, scheme_label, basis_date) + f"\n⚠️ 분석 실패: {message}"


def usage_error(message: str) -> str:
    return f"🔎 질의 오류\n{message}"


def build(req: dict, ctx: dict) -> str:
    """리포트 조립 (§5 템플릿).

    req: query_parser.parse 출력 (+basis 확정 후 req['basis'])
    ctx: {close, mktcap, fin_as_of, corrected_from, valuation_lines[],
          points[], checklist[], entry, targets{}, assumptions[], flags[]}
    """
    lines = [header(req["name"], req["scheme_label"], ctx["basis"])]

    mktcap = ctx.get("mktcap")
    close = ctx.get("close")
    first = f"📌 {req['ticker']}"
    if close is not None:
        first += f" · 💰 종가 {close:,.0f}원"
    if mktcap is not None:
        first += f" · 시총 {mktcap / 1e8:,.0f}억원"
    lines.append(first)
    lines.append(f"   재무 as-of: {ctx.get('fin_as_of', '미상')}")
    if ctx.get("corrected_from"):
        lines.append(f"   기준일 보정: {fmt_date(ctx['corrected_from'])}"
                     f"→{fmt_date(ctx['basis'])} (휴장일)")

    if ctx.get("valuation_lines"):
        lines.append("\n[밸류에이션]")
        lines += [f" {ln}" for ln in ctx["valuation_lines"]]

    if ctx.get("points"):
        lines.append("\n[투자 포인트]")
        lines += [f" {ln}" for ln in ctx["points"]]

    lines.append(f"\n[적정 매수가]  {ctx.get('entry', '산출 불가')}")
    targets = ctx.get("targets") or {}
    if targets:
        lines.append("[기간별 목표가]")
        for horizon in ("6개월", "1년", "3년"):
            if horizon in targets:
                lines.append(f" · {horizon}: {targets[horizon]}")
    for a in ctx.get("assumptions") or []:
        lines.append(f"   ※ 가정: {a}")

    checklist = ctx.get("checklist") or []
    if checklist:
        lines.append("\n⚠️ 미검증 정성 체크리스트(사람이 확인):")
        lines += [f" · {c}" for c in checklist]
    if ctx.get("flags"):
        lines.append(f"플래그: {', '.join(sorted(set(ctx['flags']))[:10])}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
