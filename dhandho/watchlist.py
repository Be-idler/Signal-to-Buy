"""트랙3 — 보유·관찰 종목 모니터링 로직 (애드온3 P2-1, 순수 함수).

시스템 원래 목표의 '나머지 절반'(보유·매도 측). 전면 자동 매도판정은 하지 않고,
기계가 잘하는 3가지만 "판단 재료"로 발송한다(매도 지시가 아님):
  ① 가격 트리거 도달(분할매수/목표가) — 하향/상향 교차 순간에만
  ② 이벤트 — 정정공시·감사의견·자본잠식·최대주주 변경 등 논지 점검 필요
  ③ 게이트 재채점 — 분기 재무 갱신 시 A(하방) 또는 D(밸류트랩)가 3.0 아래로 붕괴

네트워크·저장은 run_watchlist.py가 담당하고, 여기엔 판정 로직만 둔다(테스트 용이).
"""
from __future__ import annotations

# 공시 제목(report_nm) 키워드 → 사람이 읽는 이벤트 라벨. 🚨=심각, ⚠️=점검, ✅=긍정
EVENT_KEYWORDS = [
    ("의견거절", "🚨 감사 의견거절"),
    ("부적정", "🚨 감사 부적정의견"),
    ("한정", "⚠️ 감사 한정의견"),
    ("자본잠식", "🚨 자본잠식"),
    ("상장폐지", "🚨 상장폐지 관련"),
    ("관리종목", "🚨 관리종목 지정"),
    ("횡령", "🚨 횡령·배임"),
    ("배임", "🚨 횡령·배임"),
    ("소각", "✅ 자기주식 소각"),
    ("최대주주", "⚠️ 최대주주 관련"),
    ("정정", "⚠️ 정정공시"),
]

GATE_MIN = 3.0


def price_alerts(entry: dict, close, prev_close) -> list[str]:
    """가격 트리거 — 하향/상향 '교차'가 발생한 순간에만 발화(매일 반복 방지).

    direction below(기본): 직전 종가는 트리거 위, 오늘 종가가 트리거 이하로 내려옴.
    direction above: 그 반대(목표가 상단 도달).
    prev_close가 없으면(첫 관측) 이미 조건을 충족한 경우에 한해 1회 발화.
    """
    if close is None:
        return []
    out = []
    for trg in entry.get("triggers") or []:
        if (trg.get("type") or "price") != "price":
            continue
        price = trg.get("price")
        if price is None:
            continue
        direction = trg.get("direction", "below")
        note = trg.get("note") or ""
        if direction == "below":
            crossed = close <= price and (prev_close is None or prev_close > price)
            arrow = "이하 도달"
        else:
            crossed = close >= price and (prev_close is None or prev_close < price)
            arrow = "이상 도달"
        if crossed:
            out.append(f"⏰ 트리거 {arrow}: {close:,.0f}원 (기준 {price:,.0f}원)"
                       + (f" — {note}" if note else ""))
    return out


def event_alerts(disclosures, since_date: str | None) -> list[str]:
    """공시 이벤트 — since_date(YYYYMMDD) 이후 접수분 중 키워드 매칭만(신규만 발화)."""
    out, seen = [], set()
    for d in disclosures or []:
        rcept_dt = str(d.get("rcept_dt") or "")
        if since_date and rcept_dt and rcept_dt <= since_date:
            continue
        name = d.get("report_nm") or ""
        for kw, label in EVENT_KEYWORDS:
            if kw in name and label not in seen:
                seen.add(label)
                out.append(f"{label} ({rcept_dt}) — {name}")
                break
    return out


def gate_alert(quant: dict, basis: str | None, prev_basis: str | None) -> str | None:
    """게이트 재채점 — 분기 재무가 갱신(basis 변경)됐고 A 또는 D가 붕괴하면 발화.

    basis가 직전과 같으면(같은 분기 재무) 매일 반복 알림하지 않는다
    (설계서 P2-1 ③: '분기 재무 갱신 시' 재채점).
    """
    if basis is not None and basis == prev_basis:
        return None
    a = quant.get("A_quant")
    d = quant.get("D_quant")
    broken = []
    if a is not None and a < GATE_MIN:
        broken.append(f"A(하방) {a:.1f}")
    if d is not None and d < GATE_MIN:
        broken.append(f"D(밸류트랩) {d:.1f}")
    if not broken:
        return None
    return ("⚠️ 논지 훼손 가능 — 게이트 붕괴: " + ", ".join(broken)
            + f" (기준 각 {GATE_MIN:.1f}, 재무 {basis})")


def capital_impairment_alert(m: dict) -> str | None:
    """자본잠식 — 재무에서 직접 감지(공시 스캔과 별개의 안전망)."""
    eq = m.get("equity_controlling")
    if eq is None:
        eq = m.get("total_equity")
    if eq is not None and eq <= 0:
        return "🚨 자본잠식 신호 — 지배지분 자본 ≤ 0 (D4 재무 생존력 재평가 필요)"
    return None


def format_entry_alerts(label: str, alerts: list[str]) -> str:
    """한 종목의 알림 블록."""
    return f"• {label}\n" + "\n".join(f"  {a}" for a in alerts)
