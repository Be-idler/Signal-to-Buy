"""신호 원장 (애드온3 P1-1) — 발신 신호를 백테스트 가능한 형태로 영속화.

현행 checkpoint의 signal_sent는 중복 발송 방지용 플래그일 뿐, 신호의 사후
성과를 평가할 수 있는 분석용 원장이 아니다. 이 모듈은 트리거 B가 채점한
finalists를 (date, ticker) 단위로 signals/ledger.parquet에 누적한다 —
**원장은 오늘부터 쌓여야 6개월 뒤 가중·임계 조정의 유일한 실증 근거가 된다.**

원칙:
- 발송 여부와 무관하게 채점된 finalists를 모두 기록한다(BUY만이 아니라 WATCH·PASS도
  기록해야 "게이트 통과점수 구간별 히트율"을 나중에 계산할 수 있다).
- surfaced 컬럼으로 실제 메시지에 노출됐는지 구분한다.
- 같은 (date, ticker)는 교체(재실행·테스트 발송이 원장을 오염시키지 않도록).
"""
from __future__ import annotations

from dhandho import storage

LEDGER_PATH = "signals/ledger.parquet"

# 분석에 필요한 최소 필드(설계서 P1-1) + 사후 성과계산·감사에 필요한 앵커
LEDGER_COLUMNS = [
    "date", "basis", "ticker", "name", "signal_type", "surfaced",
    "total", "A", "B", "C", "D", "E", "F",
    "rsi", "close", "mktcap",
    "beta", "stock_dd", "market_dd",
    "evidence", "recorded_at",
]


def build_row(*, date: str, basis: str | None, ticker: str, name: str | None,
              signal_type: str, surfaced: bool, result: dict, decision: dict,
              rsi, close, mktcap, market_ctx: dict | None,
              evidence: list[str] | None, recorded_at: str) -> dict:
    """채점 결과 → 원장 1행(플랫). 섹션 총점 A~F를 개별 컬럼으로 펼친다."""
    secs = (result or {}).get("sections") or {}
    mc = market_ctx or {}
    row = {c: None for c in LEDGER_COLUMNS}
    row.update({
        "date": date, "basis": basis, "ticker": ticker, "name": name,
        "signal_type": signal_type, "surfaced": bool(surfaced),
        "total": (decision or {}).get("total"),
        "rsi": rsi, "close": close, "mktcap": mktcap,
        "beta": mc.get("beta"), "stock_dd": mc.get("stock_dd"),
        "market_dd": mc.get("market_dd"),
        "evidence": ";".join(str(e) for e in (evidence or [])[:8]),
        "recorded_at": recorded_at,
    })
    for k in "ABCDEF":
        sec = secs.get(k)
        row[k] = sec.get("total") if isinstance(sec, dict) else None
    return row


def append(date_str: str, rows: list[dict]) -> int:
    """원장에 rows를 누적(같은 date의 기존 행은 교체). 반환: 원장 총 행수."""
    import pandas as pd

    if not rows:
        existing = storage.read_parquet(LEDGER_PATH)
        return 0 if existing is None else len(existing)

    new_df = pd.DataFrame(rows, columns=LEDGER_COLUMNS)
    existing = storage.read_parquet(LEDGER_PATH)
    if existing is not None and len(existing):
        # 같은 (date, ticker) 교체 — 재실행·테스트 발송이 중복 적재되지 않도록
        keep = existing[existing["date"].astype(str) != str(date_str)]
        merged = pd.concat([keep, new_df], ignore_index=True)
    else:
        merged = new_df
    storage.upload_parquet(merged, LEDGER_PATH)
    return len(merged)
