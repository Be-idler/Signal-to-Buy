import config
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
