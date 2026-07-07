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
