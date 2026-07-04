"""텔레그램 알림 — 봇1(트랙1 일일 단도 신호) / 봇2(트랙2 격주 다관점 랭킹).

신호 전용: 자동매매·주문실행 없음. 알림은 '후보' 제시까지, 판단은 사람.
"""
from __future__ import annotations

import requests

import config

_MAX_LEN = 4000   # 텔레그램 메시지 한도(4096)에 여유


# ------------------------------------------------------------------ 메시지 헤더 규격
# 봇 1개로 모든 유형을 수신하므로 헤더로 유형을 구분한다.
def fmt_date(yyyymmdd: str) -> str:
    """YYYYMMDD → YYYY-MM-DD (이미 하이픈 있으면 그대로)."""
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def header_daily(date: str) -> str:
    """봇1 — 일일 RSI 스크리닝."""
    return f"📋 단도투자 RSI<30 스크리닝 {fmt_date(date)}"


def header_biweekly(date: str) -> str:
    """봇2 — 격주 다관점 랭킹."""
    return f"📋 다관점 프레임워크 랭킹 {fmt_date(date)}"


def header_system(message: str) -> str:
    """시스템 경고."""
    return f"⚠️ [시스템] {message}"


def header_query(stock_name: str, scheme: str, basis_date: str) -> str:
    """질의응답 — 온디맨드 종목×스킴 분석."""
    return f"🔎 {stock_name} {scheme} 방식 분석 ({fmt_date(basis_date)} 기준)"


def _send(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        print("[notify] telegram not configured; message below:\n" + text)
        return False
    ok = True
    for i in range(0, len(text), _MAX_LEN):
        chunk = text[i:i + _MAX_LEN]
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": chunk,
                                "disable_web_page_preview": True},
                          timeout=30)
        ok = ok and r.ok
    return ok


def send_bot1(text: str) -> bool:
    """트랙1 — 일일 단도 신호(트리거 B, 개장 전)."""
    return _send(config.TELEGRAM_BOT1_TOKEN, config.TELEGRAM_BOT1_CHAT_ID, text)


def send_bot2(text: str) -> bool:
    """트랙2 — 격주 다관점 랭킹 다이제스트."""
    return _send(config.TELEGRAM_BOT2_TOKEN, config.TELEGRAM_BOT2_CHAT_ID, text)


def notify_failure(stage: str, error: str, bot: int = 1) -> bool:
    """파이프라인 실패 통보 (멱등·실패 시 봇 통보 원칙)."""
    text = header_system(f"{stage} 파이프라인 실패") + f"\n{error[:1000]}"
    return send_bot1(text) if bot == 1 else send_bot2(text)
