from dhandho.gate import decide_signal, quant_gate_pass


def _result(a=4.5, d=4.0, total=4.2, a_flags=None, d_flags=None, flags=None):
    return {
        "sections": {
            "A": {"total": a, "flags": a_flags or []},
            "B": {"total": 3.0, "flags": []},
            "C": {"total": 3.5, "flags": []},
            "D": {"total": d, "flags": d_flags or []},
            "E": {"total": 3.0, "flags": []},
            "F": {"total": 3.0, "flags": []},
        },
        "total": total,
        "flags": flags or [],
    }


def test_quant_gate_pass():
    assert quant_gate_pass({"A_quant": 3.0, "D_quant": 3.0})
    assert not quant_gate_pass({"A_quant": 2.9, "D_quant": 5.0})
    assert not quant_gate_pass({"A_quant": 5.0, "D_quant": 2.9})


def test_buy_candidate():
    d = decide_signal(_result())
    assert d["verdict"] == "BUY_CANDIDATE"
    assert d["gates_ok"]


def test_gate_fail_downgrades_to_hold():
    d = decide_signal(_result(d=2.5, total=3.5))
    assert d["verdict"] == "HOLD"


def test_gate_fail_low_total_excludes():
    d = decide_signal(_result(a=2.0, d=2.0, total=2.4))
    assert d["verdict"] == "EXCLUDE"


def test_insufficient_flag_in_gate_section_downgrades_to_watch():
    # §13.0: 게이트 섹션(A·D)에 근거불충분 플래그 → WATCH 강등
    d = decide_signal(_result(d_flags=["D4_insufficient"]))
    assert d["verdict"] == "WATCH"
    assert "D4_insufficient" in d["gate_flags"]


def test_watch_band():
    d = decide_signal(_result(total=3.4))
    assert d["verdict"] == "WATCH"


def test_low_total_excluded():
    d = decide_signal(_result(total=2.5))
    assert d["verdict"] == "EXCLUDE"
