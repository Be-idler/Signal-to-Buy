"""트리거 B (UTC 21:30 = KST 06:30, 개장 전) — 트랙1 일일 파이프라인 후반부.

⑤ 배치 결과 수신(미완료 시 대기·재시도) → ⑥ LLM 정성 포함 최종 게이트
(§13.4: A·D ≥ 3.0 + 플래그 검사 + 총점 임계) → 봇1 알림.

트리거 A·B는 같은 UTC 날짜를 공유한다(일자 어긋남 방지).
"""
from __future__ import annotations

import datetime as dt
import sys
import time
import traceback

import config
from dhandho import frameworks, gate, llm, notify, storage

WAIT_MINUTES = 60          # 배치 미완료 시 최대 대기
POLL_INTERVAL = 300


def _format_signal(ticker: str, entry: dict, decision: dict, result: dict) -> str:
    icon = {"BUY_CANDIDATE": "🟢", "WATCH": "🟡", "HOLD": "⚪", "EXCLUDE": "⚫"}
    lines = [
        f"{icon.get(decision['verdict'], '•')} {ticker}  [{decision['verdict']}]",
        f"  RSI {entry.get('rsi')} | 총점 {decision['total']:.2f} "
        f"(A {decision['A']:.2f} / D {decision['D']:.2f})",
        f"  {decision['reason']}",
    ]
    secs = result["sections"]
    lines.append("  섹션: " + " ".join(f"{k}={secs[k]['total']:.1f}" for k in "ABCDEF"))
    if decision["gate_flags"]:
        lines.append(f"  ⚠ 근거불충분: {', '.join(decision['gate_flags'][:5])}")
    return "\n".join(lines)


def main() -> int:
    date_str = dt.date.today().strftime("%Y%m%d")
    try:
        ckpt = storage.load_json(f"checkpoints/trigger_a_{date_str}.json")
        if ckpt is None:
            print(f"[trigger_b] no trigger_a checkpoint for {date_str} (휴장일?) — skip")
            return 0
        finalists: dict = ckpt.get("finalists") or {}
        if not finalists:
            notify.send_bot1(f"📭 {date_str} 단도 트랙: 정량 게이트 통과 종목 없음")
            return 0

        # ⑤ 배치 결과 수신 (미완료 시 대기·재시도)
        qual_by_ticker: dict = {}
        batch_id = ckpt.get("batch_id")
        if batch_id:
            deadline = time.time() + WAIT_MINUTES * 60
            while True:
                status, qual_by_ticker = llm.retrieve_batch(batch_id)
                if status == "ended":
                    break
                if time.time() > deadline:
                    notify.send_bot1(
                        f"⏳ {date_str} LLM 배치 미완료(status={status}) — "
                        f"정성 미반영(2.5 캡) 신호로 대체 발송")
                    break
                time.sleep(POLL_INTERVAL)

        # ⑥ 최종 게이트 + 알림
        messages = [f"📊 단도 일일 신호 {date_str} (finalists {len(finalists)})",
                    "※ 미검증 임계 기반 '후보' 알림 — 최종 판단은 사람"]
        for ticker, entry in sorted(finalists.items()):
            qual = qual_by_ticker.get(ticker) or {}
            result = frameworks.score_dhandho(
                entry["metrics"], qual=qual,
                disclosures=entry.get("disclosures"))
            decision = gate.decide_signal(result)
            storage.save_json(
                {"date": date_str, "result": result, "decision": decision},
                f"signals/{date_str}_{ticker}.json")
            messages.append(_format_signal(ticker, entry, decision, result))

        notify.send_bot1("\n\n".join(messages))
        print(f"[trigger_b] sent signals for {len(finalists)} finalists")
        return 0
    except Exception:
        notify.notify_failure("trigger_b", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
