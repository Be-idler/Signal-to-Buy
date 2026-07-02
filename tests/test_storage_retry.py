import pytest

from dhandho import storage


class _FlakyRequest:
    """처음 fail_times번 일시 오류를 내고 그 후 성공하는 가짜 Drive 요청."""

    def __init__(self, fail_times: int, exc: Exception):
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    def execute(self, num_retries: int = 0):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return {"ok": True}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)


def test_transient_connection_error_retried():
    req = _FlakyRequest(2, ConnectionError("reset by peer"))
    assert storage._execute(req, retries=5) == {"ok": True}
    assert req.calls == 3


def test_ssl_error_retried():
    import ssl
    req = _FlakyRequest(1, ssl.SSLError("EOF occurred"))
    assert storage._execute(req, retries=3) == {"ok": True}


def test_exhausted_retries_raise_runtime_error():
    req = _FlakyRequest(99, ConnectionError("down"))
    with pytest.raises(RuntimeError, match="Drive API failed"):
        storage._execute(req, retries=3)
    assert req.calls == 3
