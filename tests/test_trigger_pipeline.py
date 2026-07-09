"""트리거 A 전영업일 확정·트리거 B 체크포인트 소급 탐색 테스트."""
import datetime as dt

import run_trigger_a
import run_trigger_b
from dhandho import krx

# 2026-07-07(화)·07-08(수)·07-10(금)·07-13(월) — 요일 앵커
TODAY = dt.date(2026, 7, 8)          # 수요일


# ───────────────────────────── krx 전영업일 프리미티브

def test_prev_weekday_skips_weekend():
    assert krx.prev_weekday(dt.date(2026, 7, 8)) == dt.date(2026, 7, 7)   # 수→화
    assert krx.prev_weekday(dt.date(2026, 7, 13)) == dt.date(2026, 7, 10)  # 월→금


def test_previous_trading_session(monkeypatch):
    monkeypatch.setattr(krx, "recent_trading_days",
                        lambda end, n: ["20260707"] if end == dt.date(2026, 7, 7) else [])
    assert krx.previous_trading_session(dt.date(2026, 7, 8)) == "20260707"


# ───────────────────────────── trigger_a 전영업일 확정

def test_resolve_basis_prev_weekday_when_published(monkeypatch):
    # 수요일 실행 → 전영업일 화(20260707) 시세 발행됨 → basis=화
    monkeypatch.setattr(run_trigger_a.krx, "is_trading_day", lambda d: d == "20260707")
    monkeypatch.setattr(run_trigger_a.storage, "load_json", lambda p: None)
    basis, note = run_trigger_a._resolve_basis(TODAY, is_backfill=False)
    assert basis == "20260707" and "전영업일 20260707" in note


def test_resolve_basis_monday_uses_friday(monkeypatch):
    # 월요일 실행 → 전영업일 금(20260710)
    monkeypatch.setattr(run_trigger_a.krx, "is_trading_day", lambda d: d == "20260710")
    monkeypatch.setattr(run_trigger_a.storage, "load_json", lambda p: None)
    basis, _ = run_trigger_a._resolve_basis(dt.date(2026, 7, 13), is_backfill=False)
    assert basis == "20260710"


def test_resolve_basis_holiday_walks_back(monkeypatch):
    # 전영업일 화가 휴장(시세 없음) → 그 이전 거래일 월(20260706)로 소급
    monkeypatch.setattr(run_trigger_a.krx, "is_trading_day", lambda d: False)
    monkeypatch.setattr(run_trigger_a.krx, "recent_trading_days",
                        lambda end, n: ["20260706"])
    monkeypatch.setattr(run_trigger_a.storage, "load_json", lambda p: None)
    monkeypatch.setattr(run_trigger_a, "EOD_GRACE_MINUTES", 0)
    basis, note = run_trigger_a._resolve_basis(TODAY, is_backfill=False)
    assert basis == "20260706" and "휴장" in note and "직전 거래일 20260706" in note


def test_resolve_basis_skips_when_already_analyzed(monkeypatch):
    monkeypatch.setattr(run_trigger_a.krx, "is_trading_day", lambda d: d == "20260707")
    monkeypatch.setattr(run_trigger_a.storage, "load_json", lambda p: {"date": "20260707"})
    basis, note = run_trigger_a._resolve_basis(TODAY, is_backfill=False)
    assert basis is None and "이미 분석됨" in note


def test_resolve_basis_none_when_no_data(monkeypatch):
    monkeypatch.setattr(run_trigger_a.krx, "is_trading_day", lambda d: False)
    monkeypatch.setattr(run_trigger_a.krx, "recent_trading_days", lambda end, n: [])
    monkeypatch.setattr(run_trigger_a, "EOD_GRACE_MINUTES", 0)
    basis, note = run_trigger_a._resolve_basis(TODAY, is_backfill=False)
    assert basis is None and "시세 없음" in note


def test_resolve_basis_backfill_requires_that_day(monkeypatch):
    monkeypatch.setattr(run_trigger_a.krx, "is_trading_day", lambda d: d == "20260706")
    assert run_trigger_a._resolve_basis(dt.date(2026, 7, 6), is_backfill=True)[0] == "20260706"
    assert run_trigger_a._resolve_basis(dt.date(2026, 7, 5), is_backfill=True)[0] is None


# ───────────────────────────── trigger_b 체크포인트 소급 (전영업일 기준)

def _sessions(monkeypatch, chain: dict):
    """previous_trading_session(anchor) → chain 매핑으로 모킹."""
    monkeypatch.setattr(run_trigger_b.krx, "previous_trading_session",
                        lambda anchor: chain.get(anchor))


def test_find_checkpoint_prev_session(monkeypatch):
    _sessions(monkeypatch, {TODAY: "20260707"})
    store = {"checkpoints/trigger_a_20260707.json": {"finalists": {"005930": {}}}}
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: store.get(p))
    date_str, ckpt = run_trigger_b._find_checkpoint(TODAY)
    assert date_str == "20260707" and "005930" in ckpt["finalists"]


def test_find_checkpoint_walks_prior_session_when_sent(monkeypatch):
    # 전영업일(화)은 이미 발송됨 → 그 이전 거래일(월)의 미발송분을 잡는다
    _sessions(monkeypatch, {TODAY: "20260707", dt.date(2026, 7, 7): "20260706"})
    store = {
        "checkpoints/trigger_a_20260707.json": {"signal_sent": True},
        "checkpoints/trigger_a_20260706.json": {"finalists": {}},
    }
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: store.get(p))
    date_str, _ = run_trigger_b._find_checkpoint(TODAY)
    assert date_str == "20260706"


def test_find_checkpoint_none(monkeypatch):
    _sessions(monkeypatch, {TODAY: "20260707", dt.date(2026, 7, 7): "20260706",
                            dt.date(2026, 7, 6): "20260703"})
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: None)
    assert run_trigger_b._find_checkpoint(TODAY) is None


def test_find_checkpoint_calendar_fallback_on_krx_error(monkeypatch):
    # KRX 조회 실패 → 달력 기준 폴백으로 전일 체크포인트를 잡는다
    def _boom(anchor):
        raise RuntimeError("KRX down")
    monkeypatch.setattr(run_trigger_b.krx, "previous_trading_session", _boom)
    store = {"checkpoints/trigger_a_20260707.json": {"finalists": {}}}
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: store.get(p))
    date_str, _ = run_trigger_b._find_checkpoint(TODAY)
    assert date_str == "20260707"


# ───────────────────────────── 메시지 포맷 — 종목명·시장요인(β) 표기

def test_label_prefers_checkpoint_name_then_map():
    assert run_trigger_b._label("006050", {"name": "국영지앤엠"}, {}) == "국영지앤엠 (006050)"
    assert run_trigger_b._label("006050", {}, {"006050": "국영지앤엠"}) == "국영지앤엠 (006050)"
    assert run_trigger_b._label("006050", {}, {}) == "006050"


def test_market_factor_line_computes_share_from_beta():
    entry = {"market_context": {"note": None, "beta": 1.2,
                                "stock_dd": -0.20, "market_dd": -0.10}}
    line = run_trigger_b._market_factor_line(entry)
    assert "β 1.20" in line and "-10.0%" in line
    assert "시장 기여 약 60%" in line          # 1.2×(-10%)/(-20%) = 0.6


def test_market_factor_line_prefers_assess_note():
    entry = {"market_context": {"note": "최근 60거래일 하락의 약 80%가 지수 동반 하락으로 설명"}}
    assert "80%" in run_trigger_b._market_factor_line(entry)


def test_market_factor_line_none_without_data():
    assert run_trigger_b._market_factor_line({}) is None
    assert run_trigger_b._market_factor_line({"market_context": {"beta": 1.0}}) is None


def test_digest_row_includes_name_and_market_factor():
    entry = {"rsi": 25.5, "name": "국영지앤엠",
             "market_context": {"beta": 0.9, "stock_dd": -0.15, "market_dd": -0.09}}
    decision = {"total": 3.06, "verdict": "WATCH"}
    row = run_trigger_b._format_digest_row(
        "006050", entry, decision, {"drop_reason": "업황 부진"}, {}, {})
    assert "국영지앤엠 (006050)" in row
    assert "하락사유: 업황 부진" in row
    assert "시장요인:" in row and "β 0.90" in row
