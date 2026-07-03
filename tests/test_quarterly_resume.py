"""run_quarterly의 병렬 수집·체크포인트 재개·시간 예산 중단 로직 테스트."""
import pytest

import run_quarterly


class _MemStorage:
    def __init__(self):
        self.json: dict = {}
        self.parquet: dict = {}

    def load_json(self, path):
        return self.json.get(path)

    def save_json(self, obj, path):
        self.json[path] = obj

    def upload_parquet(self, df, path):
        self.parquet[path] = df

    def exists(self, path):
        return path in self.parquet


@pytest.fixture
def env(monkeypatch):
    store = _MemStorage()
    monkeypatch.setattr(run_quarterly, "storage", store)
    monkeypatch.setattr(run_quarterly.dart, "get_corp_codes",
                        lambda: {"000010": "C1", "000020": "C2", "000030": "C3"})
    monkeypatch.setattr(run_quarterly.dart, "get_financials",
                        lambda corp, year, reprt: [{"account_id": "ifrs-full_Revenue",
                                                    "account_nm": "매출액",
                                                    "thstrm_amount": "100",
                                                    "sj_div": "IS"}])
    monkeypatch.setattr(run_quarterly, "_time_left", lambda: True)
    return store


def test_collect_completes_and_uploads(env):
    assert run_quarterly.collect(2025, "11011") is True
    ckpt = env.json["checkpoints/quarterly_2025_11011.json"]
    assert len(ckpt["done"]) == 3 and ckpt["rows"] == []      # 완료 후 행 비움
    df = env.parquet["financials/2025_11011.parquet"]
    assert len(df) == 3 and set(df["ticker"]) == {"000010", "000020", "000030"}


def test_collect_resumes_from_checkpoint(env):
    # 이전 런에서 2종목 처리된 상태를 시뮬레이션
    env.json["checkpoints/quarterly_2025_11011.json"] = {
        "done": ["000010", "000020"],
        "rows": [{"ticker": "000010", "revenue": 100.0},
                 {"ticker": "000020", "revenue": 100.0}],
    }
    calls = []
    run_quarterly.dart.get_financials = lambda corp, y, r: calls.append(corp) or [
        {"account_id": "ifrs-full_Revenue", "account_nm": "매출액",
         "thstrm_amount": "100", "sj_div": "IS"}]
    assert run_quarterly.collect(2025, "11011") is True
    assert calls == ["C3"]                                    # 남은 1종목만 수집
    assert len(env.parquet["financials/2025_11011.parquet"]) == 3


def test_time_budget_pauses_with_checkpoint(env, monkeypatch):
    monkeypatch.setattr(run_quarterly, "_time_left", lambda: False)
    assert run_quarterly.collect(2025, "11011") is False      # 일시 중단
    assert "checkpoints/quarterly_2025_11011.json" in env.json
    assert "financials/2025_11011.parquet" not in env.parquet  # 미완료 → 업로드 안 함


def test_individual_failure_isolated(env, monkeypatch):
    def flaky(corp, y, r):
        if corp == "C2":
            raise RuntimeError("DART API error 900")
        return [{"account_id": "ifrs-full_Revenue", "account_nm": "매출액",
                 "thstrm_amount": "100", "sj_div": "IS"}]
    monkeypatch.setattr(run_quarterly.dart, "get_financials", flaky)
    assert run_quarterly.collect(2025, "11011") is True
    df = env.parquet["financials/2025_11011.parquet"]
    assert set(df["ticker"]) == {"000010", "000030"}          # 실패 종목만 제외
