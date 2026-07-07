"""트리거 B 체크포인트 소급 탐색(크론 지연 대비) 테스트."""
import datetime as dt

import run_trigger_b

TODAY = dt.date(2026, 7, 7)


def test_find_checkpoint_prefers_today(monkeypatch):
    store = {
        "checkpoints/trigger_a_20260707.json": {"finalists": {"005930": {}}},
        "checkpoints/trigger_a_20260706.json": {"finalists": {}},
    }
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: store.get(p))
    date_str, ckpt = run_trigger_b._find_checkpoint(TODAY)
    assert date_str == "20260707" and "005930" in ckpt["finalists"]


def test_find_checkpoint_looks_back_when_today_missing(monkeypatch):
    # 크론 지연으로 UTC 자정을 넘겨 실행 — 전일 체크포인트를 잡아야 한다
    store = {"checkpoints/trigger_a_20260706.json": {"finalists": {}}}
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: store.get(p))
    date_str, _ = run_trigger_b._find_checkpoint(TODAY)
    assert date_str == "20260706"


def test_find_checkpoint_skips_already_sent(monkeypatch):
    store = {
        "checkpoints/trigger_a_20260707.json": {"signal_sent": True},
        "checkpoints/trigger_a_20260706.json": {"finalists": {}},
    }
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: store.get(p))
    date_str, _ = run_trigger_b._find_checkpoint(TODAY)
    assert date_str == "20260706"


def test_find_checkpoint_none_within_lookback(monkeypatch):
    monkeypatch.setattr(run_trigger_b.storage, "load_json", lambda p: None)
    assert run_trigger_b._find_checkpoint(TODAY) is None


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
