from dhandho.sector_relative import percentile_score


def test_top_of_pool_scores_high():
    peers = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert percentile_score(10, peers, higher_is_better=True) == 5.0
    assert percentile_score(0, peers, higher_is_better=True) == 1.0


def test_lower_is_better_inverts():
    peers = [5, 6, 7, 8, 9, 10]
    cheap = percentile_score(4, peers, higher_is_better=False)
    rich = percentile_score(11, peers, higher_is_better=False)
    assert cheap == 5.0 and rich == 1.0


def test_none_value_returns_none():
    assert percentile_score(None, [1, 2, 3, 4, 5]) is None


def test_insufficient_peers_falls_back_to_market():
    assert percentile_score(3, [1, 2], market_fallback=[1, 2, 3, 4, 5, 6]) is not None


def test_insufficient_everywhere_returns_none():
    assert percentile_score(3, [1, 2], market_fallback=[1]) is None
