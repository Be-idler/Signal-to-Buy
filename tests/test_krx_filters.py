import datetime as dt

import pytest

import config
from dhandho import krx
from dhandho.krx import is_common_stock, passes_liquidity


def _rec(**over):
    base = {"ticker": "005930", "halted": False,
            "closes": [100.0] * 30,
            "values": [2e8] * 30}      # 평균 거래대금 2억
    base.update(over)
    return base


def test_common_stock_code_rule():
    assert is_common_stock("005930")        # 보통주(끝자리 0)
    assert not is_common_stock("005935")    # 우선주
    assert not is_common_stock("")


def test_liquidity_pass():
    assert passes_liquidity(_rec())


def test_preferred_stock_rejected():
    assert not passes_liquidity(_rec(ticker="005935"))


def test_halted_rejected():
    assert not passes_liquidity(_rec(halted=True))


def test_low_value_rejected():
    assert not passes_liquidity(_rec(values=[5e7] * 30))   # 평균 5천만 < 1억


def test_window_uses_recent_days():
    # 과거엔 활발했지만 최근 20일 거래대금이 바닥 → 탈락
    values = [5e8] * 30 + [1e6] * config.LIQ_WINDOW
    assert not passes_liquidity(_rec(values=values))


# ───────────────────────────── get_all_eod 회복력 (KRX 저하 대비)

def _snap(ticker="005930", close=100.0, mktcap=1e11):
    return [{"ticker": ticker, "name": "삼성전자", "close": close,
             "mktcap": mktcap, "value": 2e8, "market": "KOSPI"}]


def test_get_all_eod_requires_basis_day(monkeypatch):
    # 기준일 스냅샷이 비어 있으면(발행 지연·저하) 명확히 raise
    monkeypatch.setattr(krx, "get_market_snapshot", lambda trd, retries=None: [])
    with pytest.raises(RuntimeError, match="기준일"):
        krx.get_all_eod(days=5, end_date=dt.date(2026, 7, 8))


def test_get_all_eod_best_effort_skips_failed_history(monkeypatch):
    # 기준일(7/8)은 성공, 과거 한 날(7/7)은 조회 실패 → 전체 중단 없이 나머지로 진행
    calls = {}

    def _fake(trd, retries=None):
        calls[trd] = calls.get(trd, 0) + 1
        if trd == "20260707":
            raise RuntimeError("Read timed out")
        return _snap(close=100.0 + int(trd[-2:]))
    monkeypatch.setattr(krx, "get_market_snapshot", _fake)
    monkeypatch.setattr(krx.time, "sleep", lambda s: None)
    eod, snaps = krx.get_all_eod(days=4, end_date=dt.date(2026, 7, 8))
    assert "20260708" in snaps and snaps["20260708"]       # 기준일 확보
    assert "20260707" not in snaps                          # 실패일은 캐시 안 됨
    rec = eod["005930"]
    assert rec["mktcap"] is not None                        # 기준일 시총 반영
    # 실패한 7/7을 뺀 나머지 거래일 종가만 수집(전체 중단 아님)
    assert len(rec["closes"]) >= 3


def test_get_all_eod_caches_holiday_empty(monkeypatch):
    # 빈 응답(휴장)은 캐시해 재조회를 막고, 거래일 목록에서 제외
    def _fake(trd, retries=None):
        if trd == "20260708":
            return _snap()
        return [] if trd == "20260707" else _snap()         # 7/7 휴장
    monkeypatch.setattr(krx, "get_market_snapshot", _fake)
    monkeypatch.setattr(krx.time, "sleep", lambda s: None)
    eod, snaps = krx.get_all_eod(days=3, end_date=dt.date(2026, 7, 8))
    assert snaps.get("20260707") == []                      # 휴장 캐시
