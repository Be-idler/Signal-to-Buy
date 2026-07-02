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


def _format_buy(ticker: str, entry: dict, decision: dict, result: dict) -> str:
    """BUY 상세 (v1 format_buy 준용)."""
    lines = [
        f"🟢 {ticker}  [BUY]",
        f"  RSI {entry.get('rsi')} | 총점 {decision['total']:.2f} "
        f"(A {decision['A']:.2f} / D {decision['D']:.2f})",
        f"  {decision['reason']}",
    ]
    secs = result["sections"]
    lines.append("  섹션: " + " ".join(f"{k}={secs[k]['total']:.1f}" for k in "ABCDEF"))
    return "\n".join(lines)


def _format_digest_row(ticker: str, entry: dict, decision: dict, qual: dict,
                       m: dict) -> str:
    """그라운딩 숏리스트 폴백 — 3줄/종목 (v1 §6)."""
    drop = (qual.get("drop_reason")
            or (qual.get("D2") or {}).get("reason"))
    if not drop:
        dd = m.get("drawdown_52w")
        drop = f"52주 고점 대비 {dd:+.0%}" if dd is not None else "하락사유 미확보"
    select = qual.get("selection_reason") or "선정사유 미확보"
    return (f"• {ticker} — 총점 {decision['total']:.2f} · {decision['verdict']} · "
            f"RSI {entry.get('rsi')}\n  하락사유: {drop}\n  선정사유: {select}")


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

        # ⑥ 최종 게이트 → v1 알림 정책: BUY 우선, BUY 0건이면 그라운딩 숏리스트 폴백
        buys, digest_rows = [], []
        for ticker, entry in sorted(finalists.items()):
            qual = qual_by_ticker.get(ticker) or {}
            result = frameworks.score_dhandho(
                entry["metrics"], qual=qual,
                disclosures=entry.get("disclosures"),
                shareholder=entry.get("shareholder"),
                insider=entry.get("insider"))
            decision = gate.decide_signal(result)
            storage.save_json(
                {"date": date_str, "result": result, "decision": decision,
                 "qual": qual},
                f"signals/{date_str}_{ticker}.json")
            if decision["verdict"] == "BUY":
                buys.append(_format_buy(ticker, entry, decision, result))
            if qual and not qual.get("_error"):     # 그라운딩된 종목만 폴백 대상
                digest_rows.append(_format_digest_row(ticker, entry, decision,
                                                      qual, entry["metrics"]))

        header = f"📊 단도 일일 신호 {date_str} — 최종 판단은 사람"
        if buys:
            notify.send_bot1("\n\n".join([header] + buys))
        elif digest_rows:
            notify.send_bot1("\n\n".join(
                [header + "\n(BUY 0건 — 그라운딩 숏리스트 폴백)"] + digest_rows))
        else:
            notify.send_bot1(f"📭 {date_str} 단도 트랙: BUY 0건, 그라운딩 종목 없음")
        print(f"[trigger_b] BUY {len(buys)} / digest {len(digest_rows)}")
        return 0
    except Exception:
        notify.notify_failure("trigger_b", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
