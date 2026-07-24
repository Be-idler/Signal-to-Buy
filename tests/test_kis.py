import json

import config
from dhandho import kis


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def _price_ok(ticker="005930", close="70000", avls="4184000"):
    return {"rt_cd": "0", "output": {
        "stck_prpr": close, "hts_avls": avls, "acml_vol": "1000000",
        "acml_tr_pbmn": "70000000000", "hts_kor_isnm": ""}}


def test_num_parses_and_guards():
    assert kis._num("1,234") == 1234.0
    assert kis._num("-") is None
    assert kis._num(None) is None
    assert kis._num("70000") == 70000.0


def test_get_token_success(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(kis.requests, "post",
                        lambda *a, **k: _Resp(200, {"access_token": "TOK"}))
    assert kis.get_token() == "TOK"


def test_get_token_missing_keys(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "")
    try:
        kis.get_token()
        assert False, "should raise"
    except RuntimeError:
        pass


def test_inquire_parses_mktcap_eok_to_won(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(kis.requests, "get",
                        lambda *a, **k: _Resp(200, _price_ok(avls="4184000")))
    lim = kis._RateLimiter(1000)
    row = kis._inquire("TOK", "005930", lim)
    assert row["close"] == 70000.0
    assert row["mktcap"] == 4184000 * 1e8      # 억원 → 원
    assert row["value"] == 70000000000.0


def test_inquire_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(kis.time, "sleep", lambda *_: None)   # 백오프 스킵
    calls = {"n": 0}

    def _get(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(500, {"rt_cd": "1", "msg_cd": "EGW00201",
                               "msg1": "초당 거래건수를 초과하였습니다."})
        return _Resp(200, _price_ok())

    monkeypatch.setattr(kis.requests, "get", _get)
    lim = kis._RateLimiter(1000)
    row = kis._inquire("TOK", "005930", lim, retries=5)
    assert row is not None and row["close"] == 70000.0
    assert calls["n"] == 3                       # 2회 유량초과 후 성공


def test_inquire_non_rate_error_returns_none(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(kis.time, "sleep", lambda *_: None)
    monkeypatch.setattr(kis.requests, "get",
                        lambda *a, **k: _Resp(200, {"rt_cd": "1", "msg1": "종목오류"}))
    lim = kis._RateLimiter(1000)
    assert kis._inquire("TOK", "999999", lim) is None


def test_fetch_snapshot_filters_and_maps(monkeypatch):
    monkeypatch.setattr(config, "KIS_APP_KEY", "k")
    monkeypatch.setattr(config, "KIS_APP_SECRET", "s")
    monkeypatch.setattr(kis, "get_token", lambda: "TOK")

    def _inq(token, ticker, limiter, retries=5):
        if ticker == "000000":                   # 실패 종목(제외 대상)
            return None
        return {"ticker": ticker, "name": None, "close": 100.0,
                "mktcap": 1e11, "volume": 1.0, "value": 1.0, "market": None}

    monkeypatch.setattr(kis, "_inquire", _inq)
    rows = kis.fetch_snapshot(["005930", "000000", "000660"], workers=2, rate=1000)
    got = {r["ticker"] for r in rows}
    assert got == {"005930", "000660"}           # 실패 종목 빠짐
