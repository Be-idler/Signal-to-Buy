"""run_quarterly의 병렬 수집·체크포인트 재개·시간 예산 중단 로직 테스트."""
import pandas as pd
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

    def read_parquet(self, path):
        return self.parquet.get(path)

    def exists(self, path):
        return path in self.parquet


class _NotifyStub:
    header_system = staticmethod(lambda m: m)
    send_bot1 = staticmethod(lambda t: True)
    notify_failure = staticmethod(lambda *a, **k: True)


@pytest.fixture
def env(monkeypatch):
    store = _MemStorage()
    monkeypatch.setattr(run_quarterly, "storage", store)
    monkeypatch.setattr(run_quarterly, "notify", _NotifyStub)
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
    assert run_quarterly.collect(2025, "11011") == 3          # 적재 종목 수 반환
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
    assert run_quarterly.collect(2025, "11011") == 3
    assert calls == ["C3"]                                    # 남은 1종목만 수집
    assert len(env.parquet["financials/2025_11011.parquet"]) == 3


def test_time_budget_pauses_with_checkpoint(env, monkeypatch):
    monkeypatch.setattr(run_quarterly, "_time_left", lambda: False)
    assert run_quarterly.collect(2025, "11011") is None       # 일시 중단
    assert "checkpoints/quarterly_2025_11011.json" in env.json
    assert "financials/2025_11011.parquet" not in env.parquet  # 미완료 → 업로드 안 함


def test_individual_failure_isolated(env, monkeypatch):
    def flaky(corp, y, r):
        if corp == "C2":
            raise RuntimeError("DART API error 900")
        return [{"account_id": "ifrs-full_Revenue", "account_nm": "매출액",
                 "thstrm_amount": "100", "sj_div": "IS"}]
    monkeypatch.setattr(run_quarterly.dart, "get_financials", flaky)
    assert run_quarterly.collect(2025, "11011") == 2
    df = env.parquet["financials/2025_11011.parquet"]
    assert set(df["ticker"]) == {"000010", "000030"}          # 실패 종목만 제외


def test_repair_missing_preserves_existing_rows(env):
    # 완료된 보고서: 체크포인트 rows는 비어 있고 parquet에 기존 2종목만 존재
    # (047050에 해당하는 000030이 원래 적재 시 누락된 상황을 재현)
    env.parquet["financials/2025_11011.parquet"] = pd.DataFrame(
        [{"ticker": "000010", "revenue": 100.0},
         {"ticker": "000020", "revenue": 200.0}], columns=run_quarterly.FIN_COLUMNS)
    run_quarterly.repair_missing(2025, "11011", ["000030"])
    df = env.parquet["financials/2025_11011.parquet"]
    assert set(df["ticker"]) == {"000010", "000020", "000030"}  # 기존 2종목 보존 + 신규 추가
    assert df.set_index("ticker").loc["000010", "revenue"] == 100.0
    assert df.set_index("ticker").loc["000020", "revenue"] == 200.0


def test_repair_missing_replaces_existing_ticker_row(env):
    env.parquet["financials/2025_11011.parquet"] = pd.DataFrame(
        [{"ticker": "000010", "revenue": 999.0}], columns=run_quarterly.FIN_COLUMNS)
    run_quarterly.repair_missing(2025, "11011", ["000010"])   # 재수집으로 교체
    df = env.parquet["financials/2025_11011.parquet"]
    assert len(df) == 1
    assert df.iloc[0]["revenue"] == 100.0                     # get_financials 픽스처 값


def test_repair_missing_requires_existing_report(env):
    with pytest.raises(RuntimeError, match="먼저 전체 적재"):
        run_quarterly.repair_missing(2025, "99999", ["000010"])


# ───────────────────────────── recollect_missing (누락 자동 재수집)

def test_recollect_fills_missing_and_preserves_present(env):
    # 유니버스 3종목 중 000030이 적재 시 누락된 상황
    env.parquet["financials/2025_11011.parquet"] = pd.DataFrame(
        [{"ticker": "000010", "revenue": 100.0},
         {"ticker": "000020", "revenue": 200.0}], columns=run_quarterly.FIN_COLUMNS)
    calls = []
    run_quarterly.dart.get_financials = lambda corp, y, r: calls.append(corp) or [
        {"account_id": "ifrs-full_Revenue", "account_nm": "매출액",
         "thstrm_amount": "100", "sj_div": "IS"}]
    run_quarterly.recollect_missing(2025, "11011")
    assert calls == ["C3"]                                     # 누락된 1종목만 재조회
    df = env.parquet["financials/2025_11011.parquet"]
    assert set(df["ticker"]) == {"000010", "000020", "000030"}
    assert df.set_index("ticker").loc["000010", "revenue"] == 100.0  # 기존 보존


def test_recollect_caches_no_data_tickers(env):
    # 000030은 DART 무자료 → 캐시에 기록되어 다음 실행 시 재조회 안 함
    env.parquet["financials/2025_11011.parquet"] = pd.DataFrame(
        [{"ticker": "000010", "revenue": 100.0},
         {"ticker": "000020", "revenue": 200.0}], columns=run_quarterly.FIN_COLUMNS)
    run_quarterly.dart.get_financials = lambda corp, y, r: []   # 전부 무자료
    run_quarterly.recollect_missing(2025, "11011")
    empty = env.json["checkpoints/recollect_empty_2025_11011.json"]
    assert empty == ["000030"]
    # 재실행: 무자료 캐시 덕분에 재조회 대상 없음 → get_financials 미호출
    calls = []
    run_quarterly.dart.get_financials = lambda corp, y, r: calls.append(corp) or []
    run_quarterly.recollect_missing(2025, "11011")
    assert calls == []


def test_recollect_transient_failure_not_cached(env):
    # 일시 오류로 실패한 종목은 무자료 캐시에 넣지 않아 다음 실행에서 재시도됨
    env.parquet["financials/2025_11011.parquet"] = pd.DataFrame(
        [{"ticker": "000010", "revenue": 100.0},
         {"ticker": "000020", "revenue": 200.0}], columns=run_quarterly.FIN_COLUMNS)

    def flaky(corp, y, r):
        raise RuntimeError("DART request failed after 3 retries")
    run_quarterly.dart.get_financials = flaky
    run_quarterly.recollect_missing(2025, "11011")
    assert env.json.get("checkpoints/recollect_empty_2025_11011.json") in (None, [])
    df = env.parquet["financials/2025_11011.parquet"]
    assert set(df["ticker"]) == {"000010", "000020"}          # 추가 없음, 기존 보존


def test_corp_codes_cached_on_success(env):
    codes = run_quarterly._corp_codes()
    assert codes == {"000010": "C1", "000020": "C2", "000030": "C3"}
    assert env.json[run_quarterly.CORP_CODES_CACHE] == codes   # Drive에 캐시됨


def test_corp_codes_falls_back_to_cache_on_failure(env, monkeypatch):
    env.json[run_quarterly.CORP_CODES_CACHE] = {"000010": "C1", "000099": "C9"}

    def boom():
        raise RuntimeError("corpCode.xml 다운로드가 120초를 초과함")
    monkeypatch.setattr(run_quarterly.dart, "get_corp_codes", boom)
    codes = run_quarterly._corp_codes()
    assert codes == {"000010": "C1", "000099": "C9"}           # 캐시 폴백


def test_corp_codes_reraises_without_cache(env, monkeypatch):
    def boom():
        raise RuntimeError("corpCode.xml 다운로드가 120초를 초과함")
    monkeypatch.setattr(run_quarterly.dart, "get_corp_codes", boom)
    with pytest.raises(RuntimeError, match="120초"):           # 캐시 없으면 그대로 실패
        run_quarterly._corp_codes()


def test_recollect_noop_when_complete(env):
    # 유니버스 전부 적재되어 있으면 재수집 대상 없음
    env.parquet["financials/2025_11011.parquet"] = pd.DataFrame(
        [{"ticker": t, "revenue": 1.0} for t in ("000010", "000020", "000030")],
        columns=run_quarterly.FIN_COLUMNS)
    calls = []
    run_quarterly.dart.get_financials = lambda corp, y, r: calls.append(corp) or []
    run_quarterly.recollect_missing(2025, "11011")
    assert calls == []
