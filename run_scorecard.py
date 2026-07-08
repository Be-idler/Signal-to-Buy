"""신호 성과 스코어카드 (애드온3 P1-1) — 월 1회 수동/크론 실행.

signals/ledger.parquet의 각 신호에 대해 forward return(+5/+20/+60/+120 거래일)과
동기간 시장(전 종목 시총가중 프록시) 대비 초과수익을 계산해
signals/scorecard.parquet에 저장하고, 점수 구간별 히트율을 요약한다.

이 리포트가 게이트 가중·임계 조정의 **유일한 실증 근거**다. 단:
- 표본이 충분해지기 전(신호 MIN_SAMPLE건 미만)에는 가중치를 손대지 않는다
  (소표본 과적합 방지 — 설계서 P1-1 원칙).
- forward return은 "N번째 이후 스냅샷" 기준이라, 파이프라인이 돈 거래일만
  스냅샷이 있으므로 실제 N거래일과 근사일 수 있다(스냅샷 결손 시 caveat).

사용: python run_scorecard.py            # 전체 재계산·요약
"""
from __future__ import annotations

import re
import sys
import traceback

from dhandho import ledger, notify, storage

HORIZONS = [5, 20, 60, 120]
MIN_SAMPLE = 30                     # 이 미만이면 가중치 조정 금지(과적합 방지)
SCORECARD_PATH = "signals/scorecard.parquet"
_SNAP_RE = re.compile(r"eod_(\d{8})\.parquet$")


def _snapshot_dates() -> list[str]:
    """prices/ 하위 eod_YYYYMMDD.parquet → 오름차순 거래일 목록."""
    dates = []
    for name in storage.list_prefix("prices"):
        m = _SNAP_RE.search(name)
        if m:
            dates.append(m.group(1))
    return sorted(set(dates))


def _load_snapshot(date: str, cache: dict) -> dict:
    """일자 스냅샷 → {ticker: (close, mktcap)}. 캐시로 중복 다운로드 방지."""
    if date in cache:
        return cache[date]
    df = storage.read_parquet(f"prices/eod_{date}.parquet")
    snap = {}
    if df is not None:
        for _, r in df.iterrows():
            snap[str(r["ticker"])] = (r.get("close"), r.get("mktcap"))
    cache[date] = snap
    return snap


def _market_level(snap: dict) -> float | None:
    """시총가중 시장 레벨 프록시 = 전 종목 시총 합(market.py와 동일 개념)."""
    total = sum(mc for _, mc in snap.values() if mc is not None)
    return total or None


def _fwd(date_pos: int, dates: list[str], n: int, ticker: str,
         base_close, base_level, cache: dict) -> tuple:
    """(종목 forward return, 시장 forward return, 초과) — 산출 불가 시 (None,None,None)."""
    tgt = date_pos + n
    if tgt >= len(dates) or base_close in (None, 0):
        return None, None, None
    snap = _load_snapshot(dates[tgt], cache)
    close, _ = snap.get(ticker, (None, None))
    if close in (None, 0):
        return None, None, None
    stock_ret = close / base_close - 1.0
    lvl = _market_level(snap)
    mkt_ret = (lvl / base_level - 1.0) if (lvl and base_level) else None
    excess = (stock_ret - mkt_ret) if mkt_ret is not None else None
    return stock_ret, mkt_ret, excess


def compute() -> "object | None":
    import pandas as pd

    led = storage.read_parquet(ledger.LEDGER_PATH)
    if led is None or not len(led):
        print("[scorecard] 신호 원장 없음 — 스킵")
        return None
    dates = _snapshot_dates()
    pos = {d: i for i, d in enumerate(dates)}
    cache: dict = {}

    rows = []
    for _, sig in led.iterrows():
        d = str(sig["date"])
        if d not in pos:
            continue                       # 신호일 스냅샷 없음(백필 원장 등)
        base_snap = _load_snapshot(d, cache)
        base_close = sig.get("close")
        if base_close in (None, 0):
            base_close = base_snap.get(str(sig["ticker"]), (None, None))[0]
        base_level = _market_level(base_snap)
        row = {"date": d, "ticker": str(sig["ticker"]), "name": sig.get("name"),
               "signal_type": sig.get("signal_type"), "surfaced": sig.get("surfaced"),
               "total": sig.get("total"), "A": sig.get("A"), "D": sig.get("D"),
               "rsi": sig.get("rsi"), "close": base_close}
        for n in HORIZONS:
            sr, mr, ex = _fwd(pos[d], dates, n, str(sig["ticker"]),
                              base_close, base_level, cache)
            row[f"ret_{n}"] = sr
            row[f"exc_{n}"] = ex
        rows.append(row)

    sc = pd.DataFrame(rows)
    if not len(sc):
        print("[scorecard] 성과 계산 가능한 신호 없음")
        return None
    storage.upload_parquet(sc, SCORECARD_PATH)
    print(f"[scorecard] {SCORECARD_PATH} 저장 ({len(sc)}행)")
    return sc


def _summary(sc) -> str:
    """점수 구간별 히트율(초과수익>0 비율)·평균 초과수익 — 사람이 읽는 요약."""
    import pandas as pd

    n = len(sc)
    lines = [f"신호 성과 스코어카드 — 표본 {n}건"]
    if n < MIN_SAMPLE:
        lines.append(f"⚠️ 표본 {n} < {MIN_SAMPLE} — 가중치·임계 조정 금지(과적합 방지)")
    bands = [("BUY권 4.0+", sc["total"] >= 4.0),
             ("3.5~4.0", (sc["total"] >= 3.5) & (sc["total"] < 4.0)),
             ("3.0~3.5", (sc["total"] >= 3.0) & (sc["total"] < 3.5)),
             ("<3.0", sc["total"] < 3.0)]
    for h in (20, 60):
        col = f"exc_{h}"
        matured = sc[sc[col].notna()]
        if not len(matured):
            continue
        lines.append(f"\n[+{h}거래일 초과수익]")
        for label, mask in bands:
            b = matured[mask.reindex(matured.index, fill_value=False)]
            if not len(b):
                continue
            hit = (b[col] > 0).mean()
            avg = b[col].mean()
            lines.append(f"  {label}: n={len(b)} 히트율 {hit:.0%} 평균 {avg:+.1%}")
    return "\n".join(lines)


def main() -> int:
    try:
        sc = compute()
        if sc is None:
            return 0
        report = _summary(sc)
        print(report)
        notify.send_bot1(notify.header_system("월간 신호 성과\n" + report))
        return 0
    except Exception:
        notify.notify_failure("run_scorecard", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
