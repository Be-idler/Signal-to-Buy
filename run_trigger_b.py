"""트리거 B (외부 크론 KST 09:20 = UTC 00:20) — 트랙1 일일 파이프라인 후반부.

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
import os
import sys
import time
import traceback

import config
from dhandho import frameworks, gate, krx, ledger, llm, notify, storage

# 배치 미완료 시 최대 대기. 트리거 A가 배치를 제출하고(08:05경) 트리거 B가 그
# ~75분 뒤(09:20)에 소비하므로 정상일엔 배치가 이미 완료돼 즉시 반환된다. 이
# 대기는 '살짝 늦은' 배치를 위한 여유일 뿐이므로 짧게 잡아 러너 점유 시간을
# 상한한다(호스티드 러너 분과금·러너 고갈 방지). 끝내 미완료면 정성 미반영(2.5
# 캡) 폴백 신호를 보낸다. 값은 환경변수로 튜닝 가능.
WAIT_MINUTES = int(os.environ.get("BATCH_WAIT_MINUTES", "15"))
POLL_INTERVAL = int(os.environ.get("BATCH_POLL_SECONDS", "180"))
CKPT_LOOKBACK_SESSIONS = 3   # 전영업일부터 최대 N 거래일 소급(미실행 만회)
CKPT_LOOKBACK_DAYS = 5       # KRX 장애 시 달력 기준 폴백 범위(연휴 대비)


# 하락사유가 '뚜렷하지 않음'을 나타내는 표지 — 이 경우 시장요인을 별도 줄로
# 두지 않고 하락사유 줄에 병합한다(개별 사유가 없으니 시장 맥락이 곧 사유 후보).
_UNCLEAR_MARKS = ("판단 불가", "판단불가", "불가", "부족", "부재", "미확보",
                  "미확인", "알 수 없", "확인되지 않", "없음")


def _drop_reason(qual: dict, entry: dict, m: dict) -> str:
    """하락 사유(종목 고유): LLM D2(원문 근거) 우선 → 52주 낙폭 순."""
    d2 = qual.get("drop_reason") or (qual.get("D2") or {}).get("reason")
    if d2:
        return d2
    dd = m.get("drawdown_52w")
    return f"52주 고점 대비 {dd:+.0%}" if dd is not None else "하락사유 미확보"


def _drop_lines(qual: dict, entry: dict, m: dict) -> list[str]:
    """'하락사유' 줄(들) 구성 — 사유가 뚜렷하면 [하락사유, 시장요인] 두 줄,
    뚜렷하지 않으면 시장요인을 하락사유에 병합한 한 줄."""
    reason = _drop_reason(qual, entry, m)
    mf = _market_factor_line(entry)               # "시장요인: ..." | None
    d2 = qual.get("drop_reason") or (qual.get("D2") or {}).get("reason")
    unclear = (not d2) or any(k in d2 for k in _UNCLEAR_MARKS)
    if unclear and mf:
        return [f"하락사유: {reason} · {mf.replace('시장요인: ', '시장요인 참고 — ', 1)}"]
    lines = [f"하락사유: {reason}"]
    if mf:
        lines.append(mf)
    return lines


def _label(ticker: str, entry: dict, names: dict) -> str:
    """종목명 (코드) — 이름 미확보 시 코드만."""
    name = entry.get("name") or names.get(ticker)
    return f"{name} ({ticker})" if name else ticker


def _market_factor_line(entry: dict) -> str | None:
    """시장 요인 분해 — β·지수 낙폭으로 시장 하락 기여도를 정량 표기.

    급락 판정이 있으면 assess_decline의 note를 쓰고, 없어도 β·낙폭 원자료가
    있으면 직접 계산해 항상 표기한다(종목 고유 사유와 시장 요인 구분).
    """
    mc = entry.get("market_context") or {}
    if mc.get("note"):
        return f"시장요인: {mc['note']}"
    b = mc.get("beta")
    sdd, mdd = mc.get("stock_dd"), mc.get("market_dd")
    if sdd is None or mdd is None:
        return None
    beta_txt = f"β {b:.2f}" if b is not None else "β≈1 가정"
    base = f"시장요인: 최근 60거래일 종목 {sdd:+.1%} vs 지수 {mdd:+.1%} ({beta_txt})"
    bb = b if (b is not None and b > 0) else 1.0
    if sdd < 0 and mdd < 0:
        share = max(0.0, min(bb * mdd / sdd, 1.0))
        return f"{base} → 시장 기여 약 {share:.0%}"
    return base


def _signal_line(entry: dict, decision: dict) -> str | None:
    """정량 시그널(LLM 이전) → LLM 재배점 후 총점 — 개편(§13.4) 투명성 표기."""
    sig = (entry.get("quant_signal") or {}).get("total_signal")
    if sig is None:
        return None
    return f"  정량시그널 {sig:.2f} → LLM 재배점 후 {decision['total']:.2f}"


def _format_buy(ticker: str, entry: dict, decision: dict, result: dict,
                names: dict) -> str:
    """BUY 상세 (v1 format_buy 준용)."""
    lines = [
        f"🟢 {_label(ticker, entry, names)}  [BUY]",
        f"  RSI {entry.get('rsi')} | 총점 {decision['total']:.2f} "
        f"(A {decision['A']:.2f} / D {decision['D']:.2f})",
        f"  {decision['reason']}",
    ]
    sl = _signal_line(entry, decision)
    if sl:
        lines.append(sl)
    secs = result["sections"]
    lines.append("  섹션: " + " ".join(f"{k}={secs[k]['total']:.1f}" for k in "ABCDEF"))
    mf = _market_factor_line(entry)
    if mf:
        lines.append(f"  {mf}")
    return "\n".join(lines)


def _format_digest_row(ticker: str, entry: dict, decision: dict, qual: dict,
                       m: dict, names: dict) -> str:
    """그라운딩 숏리스트 폴백 — 종목명·하락사유·시장요인·선정사유 (v1 §6 확장)."""
    select = qual.get("selection_reason") or "선정사유 미확보"
    lines = [f"• {_label(ticker, entry, names)} — 총점 {decision['total']:.2f} · "
             f"{decision['verdict']} · RSI {entry.get('rsi')}"]
    sl = _signal_line(entry, decision)
    if sl:
        lines.append(sl)
    lines += [f"  {ln}" for ln in _drop_lines(qual, entry, m)]
    lines.append(f"  선정사유: {select}")
    return "\n".join(lines)


def _find_checkpoint(today: dt.date) -> tuple[str, dict] | None:
    """전영업일부터 거래일 단위로 소급하며 미발송 체크포인트 탐색.

    trigger_a와 **동일한 '전영업일' 기준**으로 basis를 잡아 같은 체크포인트를 집는다
    (연휴로 basis가 며칠 전이어도 정확히 대응). KRX 조회 실패 시 달력 기준 폴백.
    """
    try:
        anchor = today
        for _ in range(CKPT_LOOKBACK_SESSIONS):
            basis = krx.previous_trading_session(anchor)
            if basis is None:
                break
            ckpt = storage.load_json(f"checkpoints/trigger_a_{basis}.json")
            if ckpt is not None and not ckpt.get("signal_sent"):
                return basis, ckpt
            anchor = dt.date(int(basis[:4]), int(basis[4:6]), int(basis[6:8]))
        return None
    except Exception as e:                        # noqa: BLE001 — KRX 장애 시 달력 폴백
        print(f"[trigger_b] KRX 조회 실패, 달력 기준 폴백: {e}")
        for back in range(1, CKPT_LOOKBACK_DAYS + 1):
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
        # ⓪ 인증 프리플라이트 — Drive 토큰 만료/철회면 조용히 죽지 않고 원인을 짚어 경보
        ok, detail = storage.auth_status()
        if not ok:
            notify.send_bot1(notify.header_system(f"trigger_b 중단 — {detail}"))
            print(f"[trigger_b] auth preflight failed: {detail}")
            return 1

        today = krx.kst_today()                   # UTC 러너라도 KST 기준일로 앵커
        if args.date:
            date_str = args.date
            ckpt = storage.load_json(f"checkpoints/trigger_a_{date_str}.json")
            if ckpt is None:
                print(f"[trigger_b] no trigger_a checkpoint for {date_str} — skip")
                return 0
        else:
            found = _find_checkpoint(today)
            if found is None:
                print("[trigger_b] no unsent trigger_a checkpoint "
                      f"(최근 {CKPT_LOOKBACK_SESSIONS}거래일, 휴장일?) — skip")
                # 하트비트 — 무소식이 '휴장/신호없음'인지 '시스템 사망'인지 구분
                notify.send_heartbeat(notify.header_heartbeat(today.strftime("%Y%m%d"))
                                      + "\nB: 미발송 체크포인트 없음(휴장일·이미 발송)")
                return 0
            date_str, ckpt = found
        finalists: dict = ckpt.get("finalists") or {}
        if not finalists:
            n_oversold = ckpt.get("oversold_count")
            n_pre = ckpt.get("pre_finalist_count")
            lines = [notify.header_daily(date_str),
                     "매수 시그널 종목 없음"
                     + (f" (RSI<30 후보 {n_oversold}종목"
                        + (f" → 1차정량통과 {n_pre}종목" if n_pre is not None else "")
                        + ")" if n_oversold else "")]
            near = ckpt.get("near_misses") or []
            if near:
                lines.append("1차 게이트 근접 상위 (하방 A·안정 D 기준 각 3.0):")
                lines += [f"• {n.get('name') or n['ticker']} ({n['ticker']}) "
                          f"RSI {n['rsi']} — A {n['A_quant']:.1f} / D {n['D_quant']:.1f}"
                          for n in near]
            signal_near = ckpt.get("signal_near_misses") or []
            if signal_near:
                lines.append("매수 시그널 근접 상위 (A~F 재정규화 총점 기준 "
                             f"{config.SCORE_QUANT_SIGNAL_MIN} 미달):")
                lines += [f"• {n.get('name') or n['ticker']} ({n['ticker']}) "
                          f"RSI {n['rsi']} — 총점 {n['total_signal']:.2f}"
                          for n in signal_near]
            send("\n".join(lines))
            mark_sent(date_str, ckpt)
            return 0

        # 종목명·종가 맵 — 원장에 종가를 남기려면 EOD parquet가 필요(항상 로드)
        names: dict = {}
        closes: dict = {}
        eod_df = storage.read_parquet(f"prices/eod_{date_str}.parquet")
        if eod_df is not None:
            names = dict(zip(eod_df["ticker"], eod_df["name"]))
            closes = dict(zip(eod_df["ticker"], eod_df["close"]))

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
        recorded_at = dt.datetime.now(dt.timezone.utc).isoformat()
        basis = ckpt.get("fin_basis") or ckpt.get("basis")
        buys, digest_rows = [], []
        scored: dict[str, dict] = {}   # 원장 적재용 종목별 (verdict, grounded, ...)
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
            grounded = bool(qual) and not qual.get("_error")
            scored[ticker] = {"entry": entry, "result": result, "decision": decision,
                              "grounded": grounded, "qual": qual}
            if decision["verdict"] == "BUY":
                buys.append(_format_buy(ticker, entry, decision, result, names))
            if grounded:                             # 그라운딩된 종목만 폴백 대상
                digest_rows.append(_format_digest_row(ticker, entry, decision,
                                                      qual, entry["metrics"], names))

        header = notify.header_daily(date_str) + "\n※ 최종 판단은 사람"
        if buys:
            send("\n\n".join([header] + buys))
        elif digest_rows:
            send("\n\n".join(
                [header + "\n(BUY 0건 — 그라운딩 숏리스트 폴백)"] + digest_rows))
        else:
            send(notify.header_daily(date_str) + "\nBUY 0건, 그라운딩 종목 없음")

        # 신호 원장 — 채점된 finalists 전부 적재(BUY만이 아니라 WATCH·PASS도:
        # 나중에 점수 구간별 히트율을 계산하려면 탈락분도 있어야 한다). surfaced는
        # 실제 메시지 노출 여부(BUY 발송 시 BUY만, 폴백 발송 시 그라운딩 종목).
        rows = []
        for ticker, s in scored.items():
            verdict = s["decision"]["verdict"]
            surfaced = (verdict == "BUY") if buys else (s["grounded"] if digest_rows else False)
            evidence = [d.get("rcept_no") for d in (s["entry"].get("disclosures") or [])
                        if d.get("rcept_no")]
            rows.append(ledger.build_row(
                date=date_str, basis=basis, ticker=ticker,
                name=s["entry"].get("name") or names.get(ticker),
                signal_type=verdict, surfaced=surfaced,
                result=s["result"], decision=s["decision"],
                rsi=s["entry"].get("rsi"), close=closes.get(ticker),
                mktcap=(s["entry"].get("metrics") or {}).get("mktcap"),
                market_ctx=s["entry"].get("market_context"),
                evidence=evidence, recorded_at=recorded_at))
        try:
            total = ledger.append(date_str, rows)
            print(f"[trigger_b] 신호 원장 {len(rows)}건 적재 (누적 {total}행)")
        except Exception as e:                       # noqa: BLE001 — 원장 실패는 발송을 막지 않음
            print(f"[trigger_b] 신호 원장 적재 실패(무시): {e}")

        mark_sent(date_str, ckpt)
        print(f"[trigger_b] BUY {len(buys)} / digest {len(digest_rows)}")
        return 0
    except Exception:
        notify.notify_failure("trigger_b", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
