"""질의응답 리포트 포맷터 (애드온2 §5) + HEADER_QUERY 상수.

가시성 개선: 점수 코드·플래그 원문 대신 한국어 라벨과 문어체 문장으로 구성한다.
규칙(§2.1): 모든 질의응답 회신(ACK·오류 포함)은 이 모듈의 포맷터를 거친다.
헤더에 기준일 필수 — 대화 기록을 되짚을 때 시점을 알 수 있어야 한다.
"""
from __future__ import annotations

from dhandho.notify import fmt_date

HEADER_QUERY = "🔎 {name} {scheme} 방식 분석 ({date} 기준)"

DISCLAIMER = ("⚠️ 본 결과는 모델이 계산한 참고용 시나리오이며 투자 자문이 아닙니다. "
              "최종 매수·매도 판단은 반드시 사람이 하십시오. "
              "여러 투자 관점의 평가는 서로 독립적으로 해석합니다(평균 금지).")


def header(name: str, scheme_label: str, basis_date: str) -> str:
    return HEADER_QUERY.format(name=name, scheme=scheme_label,
                               date=fmt_date(basis_date))


def ack(name: str, scheme_label: str, basis_date: str) -> str:
    return header(name, scheme_label, basis_date) + "\n⏳ 분석 중입니다… (최대 2~3분)"


def error(name: str, scheme_label: str, basis_date: str, message: str) -> str:
    return header(name, scheme_label, basis_date) + f"\n⚠️ 분석에 실패했습니다: {message}"


def usage_error(message: str) -> str:
    return f"🔎 질의 형식 오류\n{message}"


def _mktcap_kr(v) -> str:
    """원 단위 시총 → '8조 6,730억원' 형태."""
    if v is None:
        return "정보 없음"
    eok = v / 1e8
    jo = int(eok // 10000)
    rem = eok - jo * 10000
    if jo > 0:
        return f"{jo}조 {rem:,.0f}억원"
    return f"{eok:,.0f}억원"


def build(req: dict, ctx: dict) -> str:
    """리포트 조립 — 문어체 프로즈(§5 재구성).

    ctx 주요 키:
      basis, corrected_from, fin_as_of, close, mktcap
      headline(str), valuation_bullets(list), decline_note(str|None),
      section_title(str), section_scores(list[(이름, 점수, 등급어)]),
      score_caveat(str|None), entry(str), targets(dict), assumptions(list),
      checklist(list), data_status(list[str])
    """
    L = [header(req["name"], req["scheme_label"], ctx["basis"])]

    # 기본 정보 줄
    info = f"종목코드 {req['ticker']}"
    if ctx.get("close") is not None:
        info += f" · 종가 {ctx['close']:,.0f}원"
    if ctx.get("mktcap") is not None:
        info += f" · 시가총액 {_mktcap_kr(ctx['mktcap'])}"
    L.append(info)
    if ctx.get("fin_as_of"):
        L.append(f"재무 기준: {ctx['fin_as_of']}")
    if ctx.get("corrected_from"):
        L.append(f"※ 기준일 보정: {fmt_date(ctx['corrected_from'])} → "
                 f"{fmt_date(ctx['basis'])} (휴장일이라 직전 거래일로 조정)")

    if ctx.get("headline"):
        L.append("\n■ 한 줄 평가")
        L.append(f" {ctx['headline']}")

    if ctx.get("valuation_bullets"):
        L.append("\n■ 밸류에이션")
        L += [f" · {b}" for b in ctx["valuation_bullets"]]

    if ctx.get("decline_note"):
        L.append("\n■ 하락 요인")
        L.append(f" {ctx['decline_note']}")

    if ctx.get("section_scores"):
        L.append(f"\n■ {ctx.get('section_title', '항목별 평가')} "
                 f"(5점 만점, 높을수록 우수)")
        for name, score, word in ctx["section_scores"]:
            sc = "—" if score is None else f"{score:.1f}점"
            L.append(f" · {name}: {sc} ({word})")
        if ctx.get("score_caveat"):
            L.append(f" ※ {ctx['score_caveat']}")

    if ctx.get("entry") or ctx.get("targets"):
        L.append("\n■ 적정 매수가·목표가")
        L.append(f" 적정 매수가: {ctx.get('entry', '산출 불가')}")
        targets = ctx.get("targets") or {}
        for horizon in ("6개월", "1년", "3년", "5년"):
            if horizon in targets:
                L.append(f" {horizon} 목표가: {targets[horizon]}")
        for a in ctx.get("assumptions") or []:
            L.append(f"   ※ 가정: {a}")

    if ctx.get("checklist"):
        L.append("\n■ 사람이 직접 확인할 것")
        L += [f" · {c}" for c in ctx["checklist"]]

    if ctx.get("data_status"):
        L.append("\n■ 참고 (데이터 상태)")
        L += [f" · {s}" for s in ctx["data_status"]]

    # 클로드 채팅 심층분석 핸드오프 — 이 리포트 전체를 스킴별 프롬프트와 함께
    # 채팅에 붙여넣으면 정성 항목을 재채점해 최종 분석을 만들 수 있게 하는
    # 기계 판독용 요약(하위점수 코드·게이트·재채점 대상·원시 플래그).
    if ctx.get("handoff"):
        L.append("\n■ 클로드 심층 재검증 (선택) — 자동 분석을 넘어 더 깊게 볼 때, "
                 "아래 블록 전체를 프롬프트 링크와 함께 클로드 채팅에 붙여넣으세요")
        L += [f" {h}" for h in ctx["handoff"]]

    L.append("\n" + DISCLAIMER)
    return "\n".join(L)
