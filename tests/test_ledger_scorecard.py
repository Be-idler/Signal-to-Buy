"""신호 원장·스코어카드 테스트 (애드온3 P1-1)."""
import pandas as pd
import pytest

import run_scorecard
from dhandho import ledger


def _result(a, d, total_secs=None):
    secs = {k: {"total": v} for k, v in (total_secs or {}).items()}
    secs.setdefault("A", {"total": a})
    secs.setdefault("D", {"total": d})
    for k in "BCEF":
        secs.setdefault(k, {"total": 3.0})
    return {"sections": secs}


def test_build_row_flattens_sections_and_evidence():
    row = ledger.build_row(
        date="20260706", basis="2025_11014", ticker="006050", name="국영지앤엠",
        signal_type="WATCH", surfaced=True,
        result=_result(3.2, 3.4, {"B": 2.9}), decision={"total": 3.06},
        rsi=25.5, close=1200.0, mktcap=8.0e10,
        market_ctx={"beta": 1.1, "stock_dd": -0.2, "market_dd": -0.1},
        evidence=["20260601000123", "20260515000999"], recorded_at="2026-07-07T00:00:00Z")
    assert row["ticker"] == "006050" and row["signal_type"] == "WATCH"
    assert row["A"] == 3.2 and row["D"] == 3.4 and row["B"] == 2.9
    assert row["total"] == 3.06 and row["close"] == 1200.0 and row["beta"] == 1.1
    assert row["evidence"] == "20260601000123;20260515000999"
    assert set(row) == set(ledger.LEDGER_COLUMNS)


def test_append_replaces_same_date(monkeypatch):
    store = {}
    monkeypatch.setattr(ledger.storage, "read_parquet",
                        lambda p: store.get(p))
    monkeypatch.setattr(ledger.storage, "upload_parquet",
                        lambda df, p: store.__setitem__(p, df.copy()))

    r1 = ledger.build_row(date="20260706", basis=None, ticker="A", name="A",
                          signal_type="BUY", surfaced=True, result=_result(4, 4),
                          decision={"total": 4.1}, rsi=20, close=100, mktcap=1e9,
                          market_ctx=None, evidence=None, recorded_at="t")
    assert ledger.append("20260706", [r1]) == 1
    # 다른 날짜 추가 → 누적
    r2 = dict(r1, date="20260707", ticker="B")
    assert ledger.append("20260707", [r2]) == 2
    # 같은 날짜 재실행 → 그 날짜 행 교체(중복 적재 안 됨)
    r1b = dict(r1, signal_type="WATCH")
    assert ledger.append("20260706", [r1b]) == 2
    final = store[ledger.LEDGER_PATH]
    assert set(final["date"]) == {"20260706", "20260707"}
    assert final[final["date"] == "20260706"].iloc[0]["signal_type"] == "WATCH"


def test_append_empty_returns_existing_len(monkeypatch):
    monkeypatch.setattr(ledger.storage, "read_parquet",
                        lambda p: pd.DataFrame({"date": ["20260706"]}))
    assert ledger.append("20260707", []) == 1


def test_scorecard_forward_return_and_excess(monkeypatch):
    # 3개 거래일 스냅샷: d0 신호, d1(+1), d2(+2). 종목 A는 +20%, 시장은 +10%.
    snaps = {
        "20260701": pd.DataFrame({"ticker": ["A", "B"], "close": [100.0, 100.0],
                                  "mktcap": [100.0, 100.0]}),
        "20260702": pd.DataFrame({"ticker": ["A", "B"], "close": [110.0, 105.0],
                                  "mktcap": [110.0, 105.0]}),
        "20260703": pd.DataFrame({"ticker": ["A", "B"], "close": [120.0, 100.0],
                                  "mktcap": [120.0, 100.0]}),
    }
    led = pd.DataFrame([{"date": "20260701", "ticker": "A", "name": "A",
                         "signal_type": "BUY", "surfaced": True, "total": 4.2,
                         "A": 4.0, "D": 3.5, "rsi": 22, "close": 100.0}])
    monkeypatch.setattr(run_scorecard.storage, "read_parquet",
                        lambda p: led if p == ledger.LEDGER_PATH else snaps.get(
                            p.split("/")[-1].replace("eod_", "").replace(".parquet", "")))
    monkeypatch.setattr(run_scorecard.storage, "list_prefix",
                        lambda pfx: [f"eod_{d}.parquet" for d in snaps])
    uploaded = {}
    monkeypatch.setattr(run_scorecard.storage, "upload_parquet",
                        lambda df, p: uploaded.__setitem__(p, df))
    monkeypatch.setattr(run_scorecard, "HORIZONS", [2])

    sc = run_scorecard.compute()
    row = sc.iloc[0]
    # A: 100→120 = +20%; 시장 합: 200→220 = +10%; 초과 = +10%
    assert row["ret_2"] == pytest.approx(0.20)
    assert row["exc_2"] == pytest.approx(0.10)


def test_scorecard_small_sample_guard(monkeypatch):
    sc = pd.DataFrame([{"total": 4.1, "exc_20": 0.05, "exc_60": None}])
    monkeypatch.setattr(run_scorecard, "MIN_SAMPLE", 30)
    out = run_scorecard._summary(sc)
    assert "가중치·임계 조정 금지" in out
