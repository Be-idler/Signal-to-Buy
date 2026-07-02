"""텔레그램 알림 — 봇1(트랙1 일일 단도 신호) / 봇2(트랙2 격주 다관점 랭킹).

신호 전용: 자동매매·주문실행 없음. 알림은 '후보' 제시까지, 판단은 사람.
"""
from __future__ import annotations

import requests

import config

_MAX_LEN = 4000   # 텔레그램 메시지 한도(4096)에 여유


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
    text = f"⚠️ [{stage}] 파이프라인 실패\n{error[:1000]}"
    return send_bot1(text) if bot == 1 else send_bot2(text)
