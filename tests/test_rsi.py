from dhandho.rsi import compute_rsi, filter_oversold


def test_rsi_needs_enough_data():
    assert compute_rsi([1, 2, 3], period=14) is None


def test_rsi_all_gains_is_100():
    closes = list(range(1, 40))
    assert compute_rsi(closes) == 100.0


def test_rsi_all_losses_is_low():
    closes = list(range(40, 1, -1))
    rsi = compute_rsi(closes)
    assert rsi is not None and rsi < 5


def test_rsi_mixed_in_range():
    closes = [100 + (3 if i % 2 else -3) for i in range(40)]
    rsi = compute_rsi(closes)
    assert 30 < rsi < 70


def test_filter_oversold_excludes_halted_and_short():
    down = [float(x) for x in range(60, 20, -1)]
    eod = {
        "AAA": {"closes": down, "halted": False},
        "BBB": {"closes": down, "halted": True},      # 거래정지 제외
        "CCC": {"closes": [1.0, 2.0], "halted": False},  # 데이터 부족 → None
        "DDD": {"closes": [float(x) for x in range(1, 60)], "halted": False},  # 상승 → RSI 높음
    }
    out = filter_oversold(eod)
    assert set(out) == {"AAA"}
    assert out["AAA"] < 30
