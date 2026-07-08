"""트랙3 워치리스트 판정 로직 테스트 (애드온3 P2-1)."""
from dhandho import watchlist


def _entry(**triggers):
    return {"ticker": "006050", "name": "국영지앤엠",
            "triggers": list(triggers.values())}


# ───────────────────────────── 가격 트리거 (교차 시에만)

def test_price_below_fires_on_downward_cross():
    e = {"triggers": [{"type": "price", "price": 1000, "direction": "below",
                       "note": "2차 트랜치"}]}
    # 직전 종가는 위, 오늘 이하로 내려옴 → 발화
    assert watchlist.price_alerts(e, close=980, prev_close=1050)
    assert "2차 트랜치" in watchlist.price_alerts(e, 980, 1050)[0]
    # 이미 아래에 계속 머무름(직전도 아래) → 반복 발화 안 함
    assert watchlist.price_alerts(e, 970, 980) == []
    # 위에 있음 → 발화 안 함
    assert watchlist.price_alerts(e, 1100, 1200) == []


def test_price_above_fires_on_upward_cross():
    e = {"triggers": [{"type": "price", "price": 1800, "direction": "above"}]}
    assert watchlist.price_alerts(e, 1850, 1700)
    assert watchlist.price_alerts(e, 1850, 1820) == []


def test_price_first_observation_fires_if_already_met():
    e = {"triggers": [{"type": "price", "price": 1000, "direction": "below"}]}
    assert watchlist.price_alerts(e, 950, None)          # prev 없음 + 조건 충족 → 1회
    assert watchlist.price_alerts(e, 1100, None) == []


def test_fundamental_trigger_ignored_by_price_check():
    e = {"triggers": [{"type": "fundamental", "condition": "Q2 흑자"}]}
    assert watchlist.price_alerts(e, 500, 2000) == []


# ───────────────────────────── 이벤트 (신규 공시만)

def test_event_alerts_new_only_and_keyword():
    disc = [
        {"report_nm": "감사보고서(의견거절)", "rcept_dt": "20260705"},
        {"report_nm": "주요사항보고서(자기주식소각결정)", "rcept_dt": "20260704"},
        {"report_nm": "분기보고서", "rcept_dt": "20260703"},           # 키워드 없음
        {"report_nm": "[기재정정]사업보고서", "rcept_dt": "20260601"},  # since 이전
    ]
    out = watchlist.event_alerts(disc, since_date="20260630")
    assert any("의견거절" in a for a in out)
    assert any("소각" in a for a in out)
    assert not any("20260601" in a for a in out)         # since 이전은 제외


def test_event_alerts_dedup_labels():
    disc = [{"report_nm": "[기재정정]A", "rcept_dt": "20260705"},
            {"report_nm": "[기재정정]B", "rcept_dt": "20260704"}]
    out = watchlist.event_alerts(disc, None)
    assert len([a for a in out if "정정공시" in a]) == 1


# ───────────────────────────── 게이트 재채점 (분기 갱신 시)

def test_gate_alert_fires_on_new_basis_when_broken():
    q = {"A_quant": 2.4, "D_quant": 3.5}
    a = watchlist.gate_alert(q, basis="2026 1분기", prev_basis="2025 사업")
    assert a and "A(하방) 2.4" in a
    # 같은 basis(같은 분기 재무) → 반복 알림 안 함
    assert watchlist.gate_alert(q, basis="2026 1분기", prev_basis="2026 1분기") is None
    # 게이트 정상 → 알림 없음
    assert watchlist.gate_alert({"A_quant": 3.2, "D_quant": 3.1},
                                basis="2026 1분기", prev_basis="2025 사업") is None


def test_capital_impairment_from_metrics():
    assert watchlist.capital_impairment_alert({"equity_controlling": -50.0})
    assert watchlist.capital_impairment_alert({"total_equity": 0.0})
    assert watchlist.capital_impairment_alert({"equity_controlling": 100.0}) is None
