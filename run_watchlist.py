"""트랙3 — 보유·관찰 종목 모니터링 (애드온3 P2-1). 매 영업일 트리거 A 직후 실행.

watchlist.yaml(레포 루트, 사람이 편집)에 보유/관찰 종목과 조건부 트리거를 적으면,
매일 ① 가격 트리거 교차 ② 공시 이벤트 ③ 게이트 붕괴(분기 재무 갱신 시)를 점검해
「👁 보유·관찰 모니터링」 알림을 보낸다. 모두 '판단 재료' — 매도 지시가 아니다.

상태: watchlist/state.json(Drive)에 종목별 직전 종가·재무기준·최근 이벤트일을 저장해
교차·신규 이벤트만 발화(매일 반복 알림 방지).

사용: python run_watchlist.py [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import traceback

from dhandho import (dart, frameworks, metrics, notify, pit, storage, watchlist)

WATCHLIST_FILE = "watchlist.yaml"
STATE_PATH = "watchlist/state.json"
EVENT_LOOKBACK_DAYS = 30


def _load_watchlist() -> list[dict]:
    """watchlist.yaml → 엔트리 목록. 파일 없음·비어있음·PyYAML 미설치 시 []."""
    import os
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        import yaml
    except ImportError:
        print("[watchlist] PyYAML 미설치 — watchlist.yaml 파싱 불가")
        return []
    with open(WATCHLIST_FILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or []
    entries = data.get("watchlist", []) if isinstance(data, dict) else data
    return [e for e in entries if e.get("ticker")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="기준일 YYYYMMDD (생략 시 최근 거래일)")
    args = ap.parse_args(argv)
    try:
        entries = _load_watchlist()
        if not entries:
            print("[watchlist] watchlist.yaml 비어있음 — 스킵")
            return 0

        ok, detail = storage.auth_status()
        if not ok:
            notify.send_bot1(notify.header_system(f"watchlist 중단 — {detail}"))
            return 1

        basis, _ = pit.resolve_basis_date(args.date)
        basis_d = dt.date(int(basis[:4]), int(basis[4:6]), int(basis[6:8]))
        prices = pit.load_prices(basis)
        state = storage.load_json(STATE_PATH) or {}
        try:
            fins, history, as_of = pit.load_financials_asof(basis)
        except RuntimeError as e:
            print(f"[watchlist] 재무 로드 실패({e}) — 가격·이벤트만 점검")
            fins, history, as_of = {}, {}, None

        bgn = (basis_d - dt.timedelta(days=EVENT_LOOKBACK_DAYS)).strftime("%Y%m%d")
        blocks, new_state = [], {}
        for e in entries:
            t = str(e["ticker"])
            st = state.get(t, {})
            label = f"{e.get('name') or prices.get(t, {}).get('name') or t} ({t})"
            close = prices.get(t, {}).get("close")
            alerts = watchlist.price_alerts(e, close, st.get("last_close"))

            fin = fins.get(t)
            if fin:
                m = metrics.compute_derived(
                    fin, mktcap=prices.get(t, {}).get("mktcap"),
                    history=history.get(t) or None)
                quant = frameworks.score_dhandho_quant(m)
                ga = watchlist.gate_alert(quant, as_of, st.get("basis"))
                if ga:
                    alerts.append(ga)
                ci = watchlist.capital_impairment_alert(m)
                if ci:
                    alerts.append(ci)

            corp = (fin or {}).get("corp_code")
            if corp:
                try:
                    disc = dart.get_recent_disclosures(corp, bgn, basis)
                    alerts += watchlist.event_alerts(disc, st.get("last_event_date"))
                except RuntimeError as ex:
                    print(f"[watchlist] {t} 공시 조회 실패(무시): {ex}")

            if alerts:
                blocks.append(watchlist.format_entry_alerts(label, alerts))
            new_state[t] = {"last_close": close, "basis": as_of,
                            "last_event_date": basis}

        storage.save_json(new_state, STATE_PATH)
        if blocks:
            notify.send_bot1(notify.header_watch(basis) + "\n※ 판단 재료 — 매도 지시 아님\n\n"
                             + "\n\n".join(blocks))
            print(f"[watchlist] {len(blocks)}종목 알림 발송")
        else:
            print(f"[watchlist] {len(entries)}종목 점검 — 알림 없음")
        return 0
    except Exception:
        notify.notify_failure("run_watchlist", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
