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
    """단도 최종 신호 판정 (score_dhandho 결과 입력).

    §13.0: 게이트 섹션(A·D)에 근거불충분 플래그가 끼면 WATCH 강등.
    §11 등급: 총점 ≥4.0 & 게이트 통과 → BUY 후보 / 3.0~4.0 → WATCH /
              게이트 미통과 → HOLD / <3.0 → EXCLUDE.
    """
    sections = result["sections"]
    a_total = sections["A"]["total"]
    d_total = sections["D"]["total"]
    total = result["total"]

    gates_ok = a_total >= config.GATE_A_MIN and d_total >= config.GATE_D_MIN
    gate_flags = [f for f in sections["A"]["flags"] + sections["D"]["flags"]
                  if f.endswith("_insufficient")]

    if not gates_ok:
        verdict = "HOLD" if total >= config.SCORE_WATCH_MIN else "EXCLUDE"
        reason = f"게이트 미통과 (A={a_total:.2f}, D={d_total:.2f})"
    elif gate_flags:
        verdict = "WATCH"                     # 게이트 섹션 근거불충분 → 강등
        reason = f"게이트 섹션 근거불충분: {', '.join(gate_flags)}"
    elif total >= config.SCORE_BUY_MIN:
        verdict = "BUY_CANDIDATE"             # '후보' — 판단은 사람
        reason = f"총점 {total:.2f} ≥ {config.SCORE_BUY_MIN}, 게이트 통과"
    elif total >= config.SCORE_WATCH_MIN:
        verdict = "WATCH"
        reason = f"총점 {total:.2f} (3.0~4.0 구간)"
    else:
        verdict = "EXCLUDE"
        reason = f"총점 {total:.2f} < {config.SCORE_WATCH_MIN}"

    return {"verdict": verdict, "reason": reason, "total": total,
            "A": a_total, "D": d_total, "gates_ok": gates_ok,
            "gate_flags": gate_flags, "flags": result["flags"]}
