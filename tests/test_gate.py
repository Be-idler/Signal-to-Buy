from dhandho.gate import decide_signal, quant_gate_pass, quant_signal_gate_pass


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


def test_quant_signal_gate_pass():
    # §13.4 개정 — A/D 최소선(3.0) + 재정규화 총점(SCORE_QUANT_SIGNAL_MIN=4.0)
    assert quant_signal_gate_pass({"A_quant": 3.0, "D_quant": 3.0, "total_signal": 4.0})
    assert not quant_signal_gate_pass(
        {"A_quant": 3.0, "D_quant": 3.0, "total_signal": 3.9})   # 총점 미달
    assert not quant_signal_gate_pass(
        {"A_quant": 2.9, "D_quant": 5.0, "total_signal": 4.5})   # A 미달
    assert not quant_signal_gate_pass(
        {"A_quant": 5.0, "D_quant": 2.9, "total_signal": 4.5})   # D 미달


def test_buy():
    d = decide_signal(_result())
    assert d["verdict"] == "BUY"
    assert d["gates_ok"]


def test_gate_fail_is_watch():
    d = decide_signal(_result(d=2.5, total=3.5))
    assert d["verdict"] == "WATCH"
    assert not d["gates_ok"]


def test_low_total_is_pass_even_with_gate_fail():
    d = decide_signal(_result(a=2.0, d=2.0, total=2.4))
    assert d["verdict"] == "PASS"


def test_insufficient_flag_blocks_buy():
    # v1 §5: A·D 근거불충분 플래그 → 게이트 미통과 → BUY 불가(WATCH)
    d = decide_signal(_result(d_flags=["D4_insufficient"]))
    assert d["verdict"] == "WATCH"
    assert not d["gates_ok"]
    assert "D4_insufficient" in d["gate_flags"]


def test_watch_band():
    d = decide_signal(_result(total=3.4))
    assert d["verdict"] == "WATCH"


def test_low_total_pass():
    d = decide_signal(_result(total=2.5))
    assert d["verdict"] == "PASS"
