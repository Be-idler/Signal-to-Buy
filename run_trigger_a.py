"""트리거 A (UTC 11:00 = KST 20:00) — 트랙1 일일 파이프라인 전반부.

흐름(지시문 2단계에서 보정된 순서 — 시총 결합 결함 수정):
① KRX EOD(가격+시총) 수집 → 일별 prices/eod_{date}.parquet 적재
② RSI<30 1차 후보
③ 후보의 분기 재무 + 당일 시총 결합 → metrics.compute_derived 재호출
④ 결합 지표로 정량 스코어링(A_quant·D_quant) → finalists
⑤ finalists 정성 자료 수집(DART 정정·수시공시·임원약력) → 델타 적재
⑥ Haiku 추출 → Sonnet 채점 Batch 제출(async, −50%) → 체크포인트 저장

KRX 휴장일은 평일이어도 전체 스킵.
`--date YYYYMMDD`로 기준일을 지정하면 과거 일자 백필·테스트 실행이 가능하다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
import time
import traceback

import pandas as pd

import config
from dhandho import (dart, frameworks, gate, krx, llm, market, metrics, notify,
                     rsi, storage)

# 당일 실행 시 KRX 시세 발행 지연 대기(휴장일과 미발행을 구분할 수 없어 폴링)
EOD_WAIT_MINUTES = 60
EOD_POLL_SECONDS = 600

# 최신 보고서 parquet가 이보다 적으면 미완성 적재(빈 파일·부분 적재)로 간주하고
# 직전 보고서로 폴백한다(상장 전종목 정상 적재는 ~2,700행).
MIN_FIN_ROWS = 100


def _jsonable(obj):
    """checkpoint 저장용: inf/NaN → JSON 안전값."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj):
            return 1e18 if obj > 0 else -1e18
        if math.isnan(obj):
            return None
    return obj


def _shareholder_summary(dividend: list[dict], treasury: list[dict]) -> dict:
    """DART 배당·자기주식 현황 → E1 결정론 입력 (v1: 미소각 자사주비중+배당)."""
    def _num(v):
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    dividend_paid = any(
        "현금배당" in (d.get("item") or "") and (_num(d.get("current")) or 0) > 0
        for d in dividend)
    retired_any = any((_num(t.get("retired")) or 0) > 0 for t in treasury)
    unretired = any((_num(t.get("end")) or 0) > 0 for t in treasury)
    return {"dividend_paid": dividend_paid, "retired_any": retired_any,
            "unretired_treasury": unretired}


def _df_to_fins(df) -> dict[str, dict]:
    """parquet DataFrame → {ticker: 정규화 fin dict} (NaN→None, flags 파싱)."""
    fins: dict[str, dict] = {}
    if df is None:
        return fins
    for _, row in df.iterrows():
        d = row.to_dict()
        raw_flags = d.get("flags")
        d["flags"] = raw_flags.split(";") if isinstance(raw_flags, str) and raw_flags else []
        d = {k: (None if (not isinstance(v, list) and pd.isna(v)) else v)
             for k, v in d.items()}
        fins[d["ticker"]] = d
    return fins


def _load_financials(today: dt.date) -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, str]]:
    """Drive의 최신 분기 SSOT + 다년 사업보고서 → (최신 fin, history, corp_code).

    ⚠️ 보고서별 손익 기준이 다르다(사업=연간, 분기/반기=기간).
    최신이 분기/반기면 기간 항목을 TTM(직전 연간 + 당기 누적 − 전년 동기 누적)으로
    변환하고, 재료가 없으면 직전 연간으로 폴백한다(metrics.build_ttm).
    """
    # 최신 보고서 우선순위: 올해/작년 분기·반기·사업 순으로 가장 최근 것
    candidates = []
    for y in (today.year, today.year - 1):
        for r in (dart.REPRT_Q3, dart.REPRT_HALF, dart.REPRT_Q1, dart.REPRT_ANNUAL):
            candidates.append((y, r))
    latest, latest_year, latest_reprt = None, None, None
    for y, r in candidates:
        path = f"financials/{y}_{r}.parquet"
        if not storage.exists(path):
            continue
        df = storage.read_parquet(path)
        n = 0 if df is None else len(df)
        if n < MIN_FIN_ROWS:
            print(f"[trigger_a] ⚠️ {path} {n}행 — 미완성 적재로 간주, 직전 보고서로 폴백"
                  f" (누락 재수집 필요: quarterly-bulk recollect {y}/{r})")
            continue
        latest, latest_year, latest_reprt = df, y, r
        break
    if latest is None:
        raise RuntimeError("no quarterly financials in storage — run_quarterly first")

    fin_by_ticker = _df_to_fins(latest)
    corp_by_ticker = {t: d.get("corp_code") for t, d in fin_by_ticker.items()}

    if latest_reprt != dart.REPRT_ANNUAL:
        prior_annual = _df_to_fins(storage.read_parquet(
            f"financials/{latest_year - 1}_{dart.REPRT_ANNUAL}.parquet"))
        prior_same = _df_to_fins(storage.read_parquet(
            f"financials/{latest_year - 1}_{latest_reprt}.parquet"))
        fin_by_ticker = {t: metrics.build_ttm(f, prior_annual.get(t), prior_same.get(t))
                         for t, f in fin_by_ticker.items()}
        print(f"[trigger_a] flow basis: TTM ({latest_year}_{latest_reprt} 기준)")

    history_by_ticker: dict[str, list[dict]] = {t: [] for t in fin_by_ticker}
    for y in range(today.year - 6, today.year):
        path = f"financials/{y}_{dart.REPRT_ANNUAL}.parquet"
        df = storage.read_parquet(path)
        if df is None:
            continue
        for _, row in df.iterrows():
            d = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
            t = d.get("ticker")
            if t in history_by_ticker:
                history_by_ticker[t].append(d)
    return fin_by_ticker, history_by_ticker, corp_by_ticker


def _await_trading_data(date_str: str, is_backfill: bool) -> bool:
    """KRX 시세 존재 확인 — 당일 실행이면 발행 지연을 기다린다.

    과거 일자(백필·테스트)는 데이터가 이미 확정돼 있어 즉시 판정한다.
    당일 평일인데 시세가 없으면 발행 지연일 수 있어 최대 EOD_WAIT_MINUTES 폴링하고,
    끝내 없으면 휴장일로 간주하되 시스템 메시지로 알린다(무단 스킵 방지).
    """
    if krx.is_trading_day(date_str):
        return True
    d = dt.datetime.strptime(date_str, "%Y%m%d").date()
    if is_backfill or d.weekday() >= 5:
        return False
    deadline = time.time() + EOD_WAIT_MINUTES * 60
    while time.time() < deadline:
        print(f"[trigger_a] {date_str} KRX 시세 미발행 — {EOD_POLL_SECONDS}s 후 재확인")
        time.sleep(EOD_POLL_SECONDS)
        if krx.is_trading_day(date_str):
            return True
    notify.send_bot1(notify.header_system(
        f"{notify.fmt_date(date_str)} KRX 시세 미발행({EOD_WAIT_MINUTES}분 대기) — "
        f"휴장일로 간주하고 스킵"))
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="기준일 YYYYMMDD (생략 시 오늘, 지정 시 백필·테스트)")
    args = ap.parse_args(argv)
    today = (dt.datetime.strptime(args.date, "%Y%m%d").date() if args.date
             else dt.date.today())
    date_str = today.strftime("%Y%m%d")
    try:
        # ① EOD 수집 (휴장일 스킵)
        if not _await_trading_data(date_str, is_backfill=bool(args.date)):
            print(f"[trigger_a] {date_str} is not a trading day — skip")
            return 0
        eod, snapshots = krx.get_all_eod(days=config.EOD_LOOKBACK_DAYS, end_date=today)
        snap_df = pd.DataFrame(snapshots[date_str])
        storage.upload_parquet(snap_df, f"prices/eod_{date_str}.parquet")

        # ② RSI<30 1차 필터 + 유동성/품질 필터 (v1 L1: 보통주·거래대금·정지 제외)
        oversold = rsi.filter_oversold(eod, config.RSI_PERIOD, config.RSI_THRESHOLD)
        oversold = {t: v for t, v in oversold.items() if krx.passes_liquidity(eod[t])}
        print(f"[trigger_a] RSI<{config.RSI_THRESHOLD} + 유동성 필터: {len(oversold)} candidates")

        # ③ 분기 재무 + 당일 시총 결합 (전 종목 — peer pool 산출에도 필요)
        fin_by_ticker, history, corp_by = _load_financials(today)
        overlap = len(set(oversold) & set(fin_by_ticker))
        print(f"[trigger_a] 재무 {len(fin_by_ticker)}종목 / EOD {len(eod)}종목 / "
              f"RSI 후보∩재무 {overlap}종목")
        if oversold and not overlap:
            print(f"[trigger_a] ⚠️ 키 불일치 의심 — oversold 예시 "
                  f"{sorted(oversold)[:3]} vs 재무 예시 {sorted(fin_by_ticker)[:3]}")
        metrics_all: dict[str, dict] = {}
        for t, fin in fin_by_ticker.items():
            mktcap = eod.get(t, {}).get("mktcap")
            closes = eod.get(t, {}).get("closes")
            metrics_all[t] = metrics.compute_derived(fin, mktcap=mktcap,
                                                     history=history.get(t) or None,
                                                     closes=closes)
        # 시장 전체 peer pool (§13.0 — 업종 세분류 미확보 시 전체시장 폴백)
        peers = {k: [m[k] for m in metrics_all.values() if m.get(k) is not None]
                 for k in ("ev_ebit", "per", "pbr", "psr",
                           "net_cash_to_mktcap", "ncav_to_mktcap")}

        # ④ 정량 스코어링 → 2차 게이트 → finalists
        finalists: dict[str, dict] = {}
        scored: dict[str, dict] = {}
        for t in oversold:
            m = metrics_all.get(t)
            if m is None:
                continue
            quant = frameworks.score_dhandho_quant(m, peers=peers)
            scored[t] = quant
            if gate.quant_gate_pass(quant):
                finalists[t] = {"rsi": round(oversold[t], 2), "quant": quant,
                                "metrics": m}
        print(f"[trigger_a] finalists after quant gate: {sorted(finalists)}")

        # 게이트 근접 상위 후보 기록 — 통과 0건인 날의 임계 적정성 점검·메시지 가시성
        near_misses = [
            {"ticker": t, "name": eod.get(t, {}).get("name"),
             "rsi": round(oversold[t], 2),
             "A_quant": round(q["A_quant"], 2), "D_quant": round(q["D_quant"], 2)}
            for t, q in sorted(scored.items(),
                               key=lambda kv: -(kv[1]["A_quant"] + kv[1]["D_quant"]))
            if t not in finalists][:5]
        for n in near_misses:
            print(f"[trigger_a] near-miss {n['ticker']} {n['name']} "
                  f"A {n['A_quant']} / D {n['D_quant']} (기준 각 3.0)")

        # 시장 요인 분해: 급락이 지수 동반인지(전 종목 시총합 프록시, 추가 API 없음).
        # 60일치 EOD가 이미 메모리에 있어 종목별 베타까지 비용 없이 산출.
        if finalists:
            mdates, mlevels = market.build_series(snapshots)
            mrets = market.returns(mlevels)
            mkt_dd = market.drawdown(mlevels)
            for t in finalists:
                stock_dd = market.drawdown(eod.get(t, {}).get("closes"))
                srets = market.returns(market.stock_level_series(snapshots, t, mdates))
                b = market.beta(srets, mrets)
                finalists[t]["market_context"] = market.assess_decline(
                    stock_dd, mkt_dd, b, window_label="최근 60거래일")

        checkpoint = {"date": date_str, "finalists": {}, "batch_id": None,
                      "oversold_count": len(oversold),
                      "near_misses": _jsonable(near_misses),
                      "peers_size": {k: len(v) for k, v in peers.items()}}

        if finalists:
            # ⑤ 정성 자료 수집 (L-Δ 델타) — finalists 한정
            #    + E1(배당·자기주식)·F2(내부자 소유보고) 결정론 입력 (v1 §4)
            bgn = (today - dt.timedelta(days=90)).strftime("%Y%m%d")
            docs: dict[str, dict] = {}
            for t in finalists:
                corp = corp_by.get(t)
                disclosures, execs, insider = [], [], []
                shareholder = None
                if corp:
                    try:
                        disclosures = dart.get_recent_disclosures(corp, bgn, date_str)
                        execs = dart.get_executive_profiles(corp, today.year - 1)
                        insider = dart.get_insider_transactions(corp)
                        dividend = dart.get_dividend_info(corp, today.year - 1)
                        treasury = dart.get_treasury_stock(corp, today.year - 1)
                        shareholder = _shareholder_summary(dividend, treasury)
                    except RuntimeError as e:
                        print(f"[trigger_a] delta fetch failed {t}: {e}")
                docs[t] = {"disclosures": disclosures, "executives": execs}
                storage.save_json(docs[t], f"delta/{date_str}_{t}.json")
                finalists[t]["disclosures"] = disclosures
                finalists[t]["insider"] = insider[:30]
                finalists[t]["shareholder"] = shareholder

            # ⑥ LLM: 그라운딩 대상은 A_quant 상위 LLM_MAX개 한정 (v1 토큰절감 2축)
            #    Haiku 추출(동기·저렴) → Sonnet 채점 Batch 제출(async, −50%)
            llm_targets = sorted(finalists, key=lambda t: -finalists[t]["quant"]["A_quant"])
            llm_targets = llm_targets[:config.LLM_MAX]
            extracted = llm.extract_passages({t: docs[t] for t in llm_targets})
            batch_id = llm.submit_batch(extracted)
            checkpoint["batch_id"] = batch_id
            checkpoint["llm_targets"] = llm_targets
            print(f"[trigger_a] batch submitted: {batch_id} (targets={llm_targets})")

        checkpoint["finalists"] = _jsonable(finalists)
        storage.save_json(checkpoint, f"checkpoints/trigger_a_{date_str}.json")
        print(f"[trigger_a] checkpoint saved for {date_str}")
        return 0
    except Exception:
        notify.notify_failure("trigger_a", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
