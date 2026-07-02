"""Wilder RSI 계산 — v1 검증 로직.

RSI<30은 매수 신호가 아니라 "지금 들여다볼 종목"을 고르는 진입 타이밍
오버레이로만 쓴다(명세서 §0·§8).
"""
from __future__ import annotations


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """종가 시계열(과거→최근 순)로 Wilder 방식 RSI를 계산한다.

    데이터가 period+1개 미만이면 None(근거불충분).
    """
    if closes is None or len(closes) < period + 1:
        return None

    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    # 초기 평균: 첫 period개의 단순평균
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def filter_oversold(eod: dict[str, dict], period: int = 14,
                    threshold: float = 30.0) -> dict[str, float]:
    """{ticker: {"closes": [...], ...}} → RSI<threshold 종목의 {ticker: rsi}."""
    out: dict[str, float] = {}
    for ticker, row in eod.items():
        closes = row.get("closes")
        if row.get("halted"):          # 거래정지 종목 제외
            continue
        rsi = compute_rsi(closes, period)
        if rsi is not None and rsi < threshold:
            out[ticker] = rsi
    return out
