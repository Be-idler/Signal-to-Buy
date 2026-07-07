"""트리거 B (UTC 21:30 = KST 06:30, 개장 전) — 트랙1 일일 파이프라인 후반부.

⑤ 배치 결과 수신(미완료 시 대기·재시도) → ⑥ LLM 정성 포함 최종 게이트
(§13.4: A·D ≥ 3.0 + 플래그 검사 + 총점 임계) → 봇1 알림.

기준일은 미발송 체크포인트를 오늘부터 최대 2일 거슬러 찾는다(크론 지연으로
UTC 자정을 넘겨 실행돼도 트리거 A 결과를 놓치지 않도록). 발송 후에는
체크포인트에 signal_sent를 기록해 중복 발송을 막는다.
`--date YYYYMMDD`로 기준일 강제, `--test`로 테스트 발송 표시가 가능하다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
import traceback

import config
from dhandho import frameworks, gate, llm, notify, storage

WAIT_MINUTES = 60          # 배치 미완료 시 최대 대기
POLL_INTERVAL = 300
CKPT_LOOKBACK_DAYS = 2     # 크론 지연 대비 체크포인트 소급 탐색 범위


def _drop_reason(qual: dict, entry: dict, m: dict) -> str:
    """하락 사유: LLM D2(원문 근거) 우선 → 시장 요인 분해 → 52주 낙폭 순."""
    d2 = qual.get("drop_reason") or (qual.get("D2") or {}).get("reason")
    if d2:
        return d2
    mc = entry.get("market_context") or {}
    if mc.get("note"):
        return mc["note"]
    dd = m.get("drawdown_52w")
    return f"52주 고점 대비 {dd:+.0%}" if dd is not None else "하락사유 미확보"


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
    mc = entry.get("market_context") or {}
    if mc.get("verdict") in ("market", "mixed"):
        lines.append(f"  하락요인: {mc['note']}")
    return "\n".join(lines)


def _format_digest_row(ticker: str, entry: dict, decision: dict, qual: dict,
                       m: dict) -> str:
    """그라운딩 숏리스트 폴백 — 3줄/종목 (v1 §6)."""
    select = qual.get("selection_reason") or "선정사유 미확보"
    return (f"• {ticker} — 총점 {decision['total']:.2f} · {decision['verdict']} · "
            f"RSI {entry.get('rsi')}\n  하락사유: {_drop_reason(qual, entry, m)}"
            f"\n  선정사유: {select}")


def _find_checkpoint(today: dt.date) -> tuple[str, dict] | None:
    """오늘부터 최대 CKPT_LOOKBACK_DAYS 거슬러 미발송 체크포인트 탐색."""
    for back in range(CKPT_LOOKBACK_DAYS + 1):
        d = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        ckpt = storage.load_json(f"checkpoints/trigger_a_{d}.json")
        if ckpt is not None and not ckpt.get("signal_sent"):
            return d, ckpt
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="기준일 YYYYMMDD (트리거 A 체크포인트 일자 강제)")
    ap.add_argument("--test", action="store_true",
                    help="테스트 발송 표시(메시지 앞에 🧪 태그)")
    args = ap.parse_args(argv)
    prefix = "🧪 [테스트 발송]\n" if args.test else ""

    def send(text: str) -> bool:
        return notify.send_bot1(prefix + text)

    def mark_sent(date_str: str, ckpt: dict) -> None:
        ckpt["signal_sent"] = True
        ckpt["signal_sent_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        storage.save_json(ckpt, f"checkpoints/trigger_a_{date_str}.json")

    try:
        if args.date:
            date_str = args.date
            ckpt = storage.load_json(f"checkpoints/trigger_a_{date_str}.json")
            if ckpt is None:
                print(f"[trigger_b] no trigger_a checkpoint for {date_str} — skip")
                return 0
        else:
            found = _find_checkpoint(dt.date.today())
            if found is None:
                print("[trigger_b] no unsent trigger_a checkpoint "
                      f"(최근 {CKPT_LOOKBACK_DAYS + 1}일, 휴장일?) — skip")
                return 0
            date_str, ckpt = found
        finalists: dict = ckpt.get("finalists") or {}
        if not finalists:
            n_oversold = ckpt.get("oversold_count")
            lines = [notify.header_daily(date_str),
                     "정량 게이트 통과 종목 없음"
                     + (f" (RSI<30 후보 {n_oversold}종목)" if n_oversold else "")]
            near = ckpt.get("near_misses") or []
            if near:
                lines.append("게이트 근접 상위 (하방 A·안정 D 기준 각 3.0):")
                lines += [f"• {n.get('name') or n['ticker']} ({n['ticker']}) "
                          f"RSI {n['rsi']} — A {n['A_quant']:.1f} / D {n['D_quant']:.1f}"
                          for n in near]
            send("\n".join(lines))
            mark_sent(date_str, ckpt)
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
                    send(notify.header_system(
                        f"{notify.fmt_date(date_str)} LLM 배치 미완료(status={status}) — "
                        f"정성 미반영(2.5 캡) 신호로 대체 발송"))
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

        header = notify.header_daily(date_str) + "\n※ 최종 판단은 사람"
        if buys:
            send("\n\n".join([header] + buys))
        elif digest_rows:
            send("\n\n".join(
                [header + "\n(BUY 0건 — 그라운딩 숏리스트 폴백)"] + digest_rows))
        else:
            send(notify.header_daily(date_str) + "\nBUY 0건, 그라운딩 종목 없음")
        mark_sent(date_str, ckpt)
        print(f"[trigger_b] BUY {len(buys)} / digest {len(digest_rows)}")
        return 0
    except Exception:
        notify.notify_failure("trigger_b", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
