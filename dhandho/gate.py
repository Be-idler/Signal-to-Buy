"""2단계 게이트 (명세서 §13.4) — v1 검증 로직 계승.

- quant_gate_pass:  트리거 A 정량 사전필터 (A_quant·D_quant ≥ 3.0)
- decide_signal:    트리거 B 최종 게이트 (LLM 정성 포함 전체 A·D + 플래그 + 총점)

시스템은 후보 알림까지만 — 최종 매수/매도 판단은 사람이 한다.
"""
from __future__ import annotations

import config


def quant_gate_pass(quant: dict) -> bool:
    """score_dhandho_quant 결과로 2차 정량 필터 통과 여부."""
    return (quant["A_quant"] >= config.GATE_A_MIN
            and quant["D_quant"] >= config.GATE_D_MIN)


def decide_signal(result: dict) -> dict:
    """단도 최종 신호 판정 (score_dhandho 결과 입력) — v1 §5 그대로.

    게이트 통과 ⟺ A≥3.0 AND D≥3.0 AND A·D에 근거불충분 플래그 없음
    BUY   ⟺ 게이트 통과 AND 총점 ≥ 4.0   (LLM 그라운딩을 거친 고확신 후보)
    PASS  ⟺ 총점 < 3.0
    WATCH ⟺ 그 외
    """
    sections = result["sections"]
    a_total = sections["A"]["total"]
    d_total = sections["D"]["total"]
    total = result["total"]

    gate_flags = [f for f in sections["A"]["flags"] + sections["D"]["flags"]
                  if f.endswith("_insufficient")]
    gates_ok = (a_total >= config.GATE_A_MIN and d_total >= config.GATE_D_MIN
                and not gate_flags)

    if gates_ok and total >= config.SCORE_BUY_MIN:
        verdict = "BUY"
        reason = f"게이트 통과 & 총점 {total:.2f} ≥ {config.SCORE_BUY_MIN}"
    elif total < config.SCORE_WATCH_MIN:
        verdict = "PASS"
        reason = f"총점 {total:.2f} < {config.SCORE_WATCH_MIN}"
    else:
        verdict = "WATCH"
        if not gates_ok:
            why = (f"근거불충분 {', '.join(gate_flags)}" if gate_flags
                   else f"A={a_total:.2f}/D={d_total:.2f}")
            reason = f"게이트 미통과 ({why})"
        else:
            reason = f"총점 {total:.2f} < {config.SCORE_BUY_MIN}"

    return {"verdict": verdict, "reason": reason, "total": total,
            "A": a_total, "D": d_total, "gates_ok": gates_ok,
            "gate_flags": gate_flags, "flags": result["flags"]}
