"""업종 상대 백분위 점수 (명세서 §13.0 — v1 §2 규칙).

금융·지주·리츠 등 절대 임계가 왜곡되는 업종은 percentile_score로 치환한다.
비교군이 5종목 미만이면 전체시장 폴백 또는 근거불충분(None) 처리.
"""
from __future__ import annotations

MIN_PEERS = 5


def percentile_score(value: float | None, peers: list[float],
                     higher_is_better: bool = True,
                     market_fallback: list[float] | None = None) -> float | None:
    """value의 비교군 내 백분위를 1.0~5.0 점수로 매핑.

    상위(유리한 쪽) 백분위일수록 5.0에 가깝다. 비교군 부족 시
    market_fallback 사용, 그것도 없으면 None(근거불충분).
    """
    if value is None:
        return None
    pool = [p for p in peers if p is not None]
    if len(pool) < MIN_PEERS:
        pool = [p for p in (market_fallback or []) if p is not None]
        if len(pool) < MIN_PEERS:
            return None

    below = sum(1 for p in pool if p < value)
    equal = sum(1 for p in pool if p == value)
    pct = (below + 0.5 * equal) / len(pool)      # 0~1, 값이 클수록 상위
    if not higher_is_better:
        pct = 1.0 - pct
    return round(1.0 + 4.0 * pct, 2)
