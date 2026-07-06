"""시장(지수) 요인 분해 — 개별 종목 급락이 지수 동반 하락으로 설명되는지 판정.

단도 관점: 성장 훼손 없는 시장 전체의 급락은 오히려 기회('Mr. Market의 소음').
지수 데이터를 따로 받지 않고, 이미 수집한 전 종목 EOD 스냅샷의 **시총 합**을
시장 프록시로 사용한다(별도 API·비용 없음). 최근 하락 중 시장 요인 비중을
추정해 '급락 원인' 후보에 '지수 동반 하락 가능성'을 제공한다.

순수 함수 모듈(네트워크 없음) — 스냅샷/시계열을 입력받는다.
"""
from __future__ import annotations

import statistics

DECLINE_THRESHOLD = -0.05      # 이 이상 하락해야 '급락'으로 보고 시장요인 판정
MARKET_SHARE_STRONG = 0.6      # 시장 기여 ≥60% → 시장 요인 우세
MARKET_SHARE_MIXED = 0.3       # 30~60% → 혼재


def market_level(rows, universe=None) -> float | None:
    """스냅샷 행들의 시총 합 = 시장 레벨 프록시. universe 지정 시 그 종목만."""
    total, n = 0.0, 0
    for r in rows:
        mc = r.get("mktcap")
        if mc is None:
            continue
        if universe is not None and r.get("ticker") not in universe:
            continue
        total += mc
        n += 1
    return total if n else None


def build_series(snapshots: dict) -> tuple[list[str], list[float | None]]:
    """{date: [rows]} → (dates_asc, levels). 마지막 날 종목집합을 고정 유니버스로.

    (유니버스를 고정해 특정일 결측 종목이 지수 프록시를 흔드는 것을 완화)
    """
    dates = sorted(snapshots)
    if len(dates) < 2:
        return dates, []
    universe = {r.get("ticker") for r in snapshots[dates[-1]]
                if r.get("mktcap") is not None}
    return dates, [market_level(snapshots[d], universe) for d in dates]


def drawdown(series) -> float | None:
    """peak→last 낙폭(음수). 값 2개 미만이면 None."""
    vals = [v for v in (series or []) if v is not None]
    if len(vals) < 2:
        return None
    peak = max(vals)
    return (vals[-1] / peak - 1.0) if peak else None


def returns(series) -> list[float | None]:
    out = []
    for i in range(1, len(series)):
        a, b = series[i - 1], series[i]
        out.append((b / a - 1.0) if (a and b) else None)
    return out


def stock_level_series(snapshots: dict, ticker: str, dates: list[str]) -> list:
    """일자별 해당 종목 종가(없으면 None)."""
    out = []
    for d in dates:
        close = None
        for r in snapshots[d]:
            if r.get("ticker") == ticker:
                close = r.get("close")
                break
        out.append(close)
    return out


def beta(stock_rets, market_rets) -> float | None:
    """cov(stock, market)/var(market). 유효 관측 10개 미만이면 None."""
    xs, ys = [], []
    for s, m in zip(stock_rets, market_rets):
        if s is None or m is None:
            continue
        xs.append(m)
        ys.append(s)
    if len(xs) < 10:
        return None
    var = statistics.pvariance(xs)
    if var == 0:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
    return cov / var


def two_point_change(snap_start_rows, snap_end_rows) -> float | None:
    """두 스냅샷(행 리스트) 공통 종목의 시총 합 변화율 — 경량 시장 변화 프록시."""
    us = {r["ticker"] for r in snap_start_rows if r.get("mktcap") is not None}
    ue = {r["ticker"] for r in snap_end_rows if r.get("mktcap") is not None}
    common = us & ue
    if not common:
        return None
    ls = market_level(snap_start_rows, common)
    le = market_level(snap_end_rows, common)
    if not ls or not le:
        return None
    return le / ls - 1.0


def assess_decline(stock_change, market_change, beta_val=None,
                   window_label="최근") -> dict:
    """개별 하락을 시장 요인 vs 개별 요인으로 분해.

    stock_change·market_change: 음수=하락. 반환: {verdict, market_share, note}.
    note=None이면 언급할 만한 급락이 아님(또는 판정 불가).
    """
    if stock_change is None or stock_change >= DECLINE_THRESHOLD:
        return {"verdict": None, "market_share": None, "note": None}
    if market_change is None:
        return {"verdict": None, "market_share": None, "note": None}

    beta_txt = f"β={beta_val:.2f}" if beta_val is not None else "β≈1 가정"
    if market_change >= -0.01:
        return {"verdict": "idiosyncratic", "market_share": 0.0,
                "note": f"{window_label} 지수는 거의 하락하지 않음"
                        f"({market_change:+.1%}) → 개별 요인 가능성 우세"}

    b = beta_val if (beta_val is not None and beta_val > 0) else 1.0
    expected = b * market_change                 # 음수
    share = max(0.0, min(expected / stock_change, 1.5))
    if share >= MARKET_SHARE_STRONG:
        return {"verdict": "market", "market_share": round(share, 2),
                "note": f"{window_label} 하락의 약 {min(share, 1.0):.0%}가 지수 동반 하락으로 "
                        f"설명(지수 {market_change:+.1%}, {beta_txt}) → 개별 악재보다 "
                        f"시장 요인 가능성"}
    if share >= MARKET_SHARE_MIXED:
        return {"verdict": "mixed", "market_share": round(share, 2),
                "note": f"{window_label} 하락 중 지수 기여 약 {share:.0%}"
                        f"(지수 {market_change:+.1%}, {beta_txt}) → 시장·개별 요인 혼재"}
    return {"verdict": "idiosyncratic", "market_share": round(share, 2),
            "note": f"{window_label} 지수({market_change:+.1%}) 대비 초과 하락 "
                    f"→ 개별 요인 우세"}
