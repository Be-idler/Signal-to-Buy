"""트리거 A (UTC 11:00 = KST 20:00) — 트랙1 일일 파이프라인 전반부.

흐름(§13.4 개정 — 매수 시그널을 정량만으로 먼저 도출하고, LLM은 시그널
종목에만 태운다. 이전엔 A/D만 보는 값싼 게이트만 거쳐 finalists 전체에
정성 자료를 모았지만, B4·D2·D3·F1·F3가 LLM 그라운딩 전엔 2.5로 보수적
캡되다 보니 최종 총점이 매수 임계(4.0)에 사실상 못 미쳤다 — 이제 그
항목들은 캡 대신 가중치에서 제외·재정규화해 순수 정량만으로 먼저 매수
시그널을 판정하고, LLM은 시그널 종목의 재배점(그라운딩)에만 쓴다):
① KRX EOD(가격+시총) 수집 → 일별 prices/eod_{date}.parquet 적재
② RSI<30 1차 후보
③ 후보의 분기 재무 + 당일 시총 결합 → metrics.compute_derived 재호출
④ 1차 정량 필터(A_quant·D_quant≥3.0, DART 호출 없음) → pre_finalists
④' 배당·자기주식·내부자 소유보고(결정론 입력) 확보 후 A~F 전 섹션
    재정규화 총점(LLM 전용 항목 제외)으로 매수 시그널 도출 → finalists
⑤ finalists(시그널 통과분)만 정기보고서 본문·수시공시 원문·뉴스·트렌드·
   수출통계 수집 → 델타 적재 (문서수집·LLM 비용을 시그널 종목으로 한정)
⑥ Haiku 추출 → Sonnet 채점 Batch 제출(async, −50%) → 체크포인트 저장

KRX 휴장일은 평일이어도 전체 스킵.
`--date YYYYMMDD`로 기준일을 지정하면 과거 일자 백필·테스트 실행이 가능하다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
import time
import traceback

import pandas as pd

import config
from dhandho import (dart, frameworks, gate, krx, llm, market, metrics, news, notify,
                     rsi, storage, trade, trends)

# 트랙1은 항상 **전영업일** 데이터로 분석한다 — KRX OpenAPI가 시세를 익영업일
# 오전 08:00경 발행하기 때문(당일 종가는 그날 안엔 절대 안 나온다). 크론은 월–금
# 08:05(trigger_a)·09:20(trigger_b) KST. 08:05에 전영업일 시세가 아직 미발행이면
# 짧게 그레이스 폴링하고, 끝내 없으면 그 평일을 휴장으로 간주해 직전 거래일로 소급.
EOD_GRACE_MINUTES = int(os.environ.get("EOD_GRACE_MINUTES", "30"))
EOD_POLL_SECONDS = 300

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


def _load_financials(today: dt.date, basis: str | None = None,
                     ) -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, str]]:
    """Drive의 최신 분기 SSOT + 다년 사업보고서 → (최신 fin, history, corp_code).

    ⚠️ 보고서별 손익 기준이 다르다(사업=연간, 분기/반기=기간).
    최신이 분기/반기면 기간 항목을 TTM(직전 연간 + 당기 누적 − 전년 동기 누적)으로
    변환하고, 재료가 없으면 직전 연간으로 폴백한다(metrics.build_ttm).
    """
    # 최신 보고서 우선순위: 올해/작년 분기·반기·사업 순으로 가장 최근 것
    # basis="YYYY_REPRT" 지정 시 해당 보고서만 사용(재수집 중 부분 적재 회피·백테스트)
    if basis:
        y, r = basis.split("_")
        candidates = [(int(y), r)]
    else:
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
    fin_basis = f"{latest_year}_{latest_reprt}"     # 신호 원장 귀속용(어느 보고서 기준인지)
    return fin_by_ticker, history_by_ticker, corp_by_ticker, fin_basis


def _resolve_basis(today: dt.date, is_backfill: bool) -> tuple[str | None, str]:
    """분석 기준일 확정 — 항상 **전영업일**. 반환 (basis_YYYYMMDD | None, note).

    - 백필(--date): 그 날짜에 시세가 있으면 그대로, 없으면 스킵(테스트·소급용).
    - 정기 실행: 직전 평일(월→금)을 대상으로 시세 발행을 짧게 그레이스 폴링한다.
      끝내 미발행이면 그 평일을 휴장으로 간주해 그 이전 시세 보유 거래일로 소급.
      확정된 전영업일이 이미 분석됐으면(체크포인트 존재) 스킵 — 중복 분석·발송 방지.
    """
    date_str = today.strftime("%Y%m%d")
    if is_backfill:
        return (date_str, "지정일") if krx.is_trading_day(date_str) else \
               (None, f"{date_str} 시세 없음(백필 대상 아님)")

    target = krx.prev_weekday(today)              # 달력상 직전 평일(월→금)
    target_str = target.strftime("%Y%m%d")
    deadline = time.time() + EOD_GRACE_MINUTES * 60
    basis, note = None, ""
    while True:
        reason = ""                               # 이번 폴에서 재시도가 필요한 사유
        try:
            if krx.is_trading_day(target_str):
                basis, note = target_str, f"전영업일 {target_str}"
                break
            reason = "시세 미발행"                  # 발행 지연 or 휴장
            if time.time() >= deadline:
                # 그레이스 초과에도 시세 없음 → 그 평일은 휴장 → 직전 거래일로 소급
                prior = krx.recent_trading_days(target - dt.timedelta(days=1), 1)
                if not prior:
                    return None, f"전영업일({target_str}) 및 그 이전 시세 없음"
                basis = prior[-1]
                note = f"전영업일({target_str}) 휴장 → 직전 거래일 {basis}"
                break
        except Exception as e:                    # noqa: BLE001 — KRX 일시 오류
            # 타임아웃 등 일시 오류는 하루를 스킵하지 않고 그레이스 내에서 재시도한다.
            if time.time() >= deadline:
                return None, f"KRX 조회 실패(그레이스 초과): {e}"
            reason = f"KRX 일시 오류({str(e)[:60]})"
        print(f"[trigger_a] 전영업일 {target_str} {reason} — {EOD_POLL_SECONDS}s 후 "
              f"재확인(그레이스 {EOD_GRACE_MINUTES}분)")
        time.sleep(EOD_POLL_SECONDS)

    if storage.load_json(f"checkpoints/trigger_a_{basis}.json") is not None:
        return None, f"{note} 이미 분석됨"
    return basis, f"{note} 기준 분석"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="기준일 YYYYMMDD (생략 시 오늘, 지정 시 백필·테스트)")
    ap.add_argument("--basis", help="재무 기준 보고서 강제 (예: 2025_11014) — "
                                    "재수집 중 부분 적재 회피·백테스트용")
    args = ap.parse_args(argv)
    today = (dt.datetime.strptime(args.date, "%Y%m%d").date() if args.date
             else krx.kst_today())              # UTC 러너라도 KST 기준일로 앵커
    date_str = today.strftime("%Y%m%d")
    try:
        # ⓪ 인증 프리플라이트 — Drive 토큰 만료/철회면 조용히 죽지 않고 원인을 짚어 경보
        ok, detail = storage.auth_status()
        if not ok:
            notify.send_bot1(notify.header_system(f"trigger_a 중단 — {detail}"))
            print(f"[trigger_a] auth preflight failed: {detail}")
            return 1

        # ① 분석 기준일 확정 — 항상 전영업일(당일 종가는 익영업일에야 발행됨)
        run_date = date_str                       # 실행일(발송·하트비트 표기용)
        basis, note = _resolve_basis(today, is_backfill=bool(args.date))
        if basis is None:
            print(f"[trigger_a] skip — {note}")
            notify.send_heartbeat(notify.header_heartbeat(run_date) + f"\nA: {note}")
            return 0
        print(f"[trigger_a] {note}")
        if basis != date_str:
            today = dt.datetime.strptime(basis, "%Y%m%d").date()
            date_str = basis
        eod, snapshots = krx.get_all_eod(days=config.EOD_LOOKBACK_DAYS, end_date=today)
        snap_df = pd.DataFrame(snapshots[date_str])
        storage.upload_parquet(snap_df, f"prices/eod_{date_str}.parquet")

        # ② RSI<30 1차 필터 + 유동성/품질 필터 (v1 L1: 보통주·거래대금·정지 제외)
        oversold = rsi.filter_oversold(eod, config.RSI_PERIOD, config.RSI_THRESHOLD)
        oversold = {t: v for t, v in oversold.items() if krx.passes_liquidity(eod[t])}
        print(f"[trigger_a] RSI<{config.RSI_THRESHOLD} + 유동성 필터: {len(oversold)} candidates")

        # ③ 분기 재무 + 당일 시총 결합 (전 종목 — peer pool 산출에도 필요)
        fin_by_ticker, history, corp_by, fin_basis = _load_financials(today, basis=args.basis)
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

        # ④ 1차 정량 필터 (§13.4, A/C/D만 — DART 호출 없이 가장 저렴) → pre_finalists
        pre_finalists: dict[str, dict] = {}
        scored: dict[str, dict] = {}
        for t in oversold:
            m = metrics_all.get(t)
            if m is None:
                continue
            quant = frameworks.score_dhandho_quant(m, peers=peers)
            scored[t] = quant
            if gate.quant_gate_pass(quant):
                pre_finalists[t] = {"rsi": round(oversold[t], 2), "quant": quant,
                                    "name": eod.get(t, {}).get("name"), "metrics": m}
        print(f"[trigger_a] 1차 정량통과(A/D≥3.0): {sorted(pre_finalists)}")

        # 게이트 근접 상위 후보 기록 — 통과 0건인 날의 임계 적정성 점검·메시지 가시성
        near_misses = [
            {"ticker": t, "name": eod.get(t, {}).get("name"),
             "rsi": round(oversold[t], 2),
             "A_quant": round(q["A_quant"], 2), "D_quant": round(q["D_quant"], 2)}
            for t, q in sorted(scored.items(),
                               key=lambda kv: -(kv[1]["A_quant"] + kv[1]["D_quant"]))
            if t not in pre_finalists][:5]
        for n in near_misses:
            print(f"[trigger_a] near-miss {n['ticker']} {n['name']} "
                  f"A {n['A_quant']} / D {n['D_quant']} (기준 각 3.0)")

        # ④' 2차 정량 필터 (§13.4 개정) — A~F 전 섹션(LLM 전용 항목 제외·재정규화)
        # 으로 매수 시그널을 직접 도출한다. E1·F2 결정론 입력을 위해 배당·자기
        # 주식·내부자 소유보고만 먼저 가져온다 — 정기보고서 본문 등 무거운 수집은
        # 이 게이트를 통과한 종목에만 뒤에서 수행해 정성 자료·LLM 비용을 시그널
        # 종목으로 한정한다(이번 개편의 핵심).
        finalists: dict[str, dict] = {}
        signal_scored: dict[str, dict] = {}
        pre_docs: dict[str, dict] = {}
        bgn = (today - dt.timedelta(days=90)).strftime("%Y%m%d")
        for t, entry in pre_finalists.items():
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
            pre_docs[t] = {"disclosures": disclosures, "executives": execs}
            quant_signal = frameworks.score_dhandho_quant_signal(
                entry["metrics"], peers=peers, disclosures=disclosures,
                shareholder=shareholder, insider=insider)
            signal_scored[t] = quant_signal
            if gate.quant_signal_gate_pass(quant_signal):
                finalists[t] = {**entry, "quant_signal": quant_signal,
                                "disclosures": disclosures, "insider": insider[:30],
                                "shareholder": shareholder}
        print(f"[trigger_a] 매수 시그널(A~F 재정규화 총점≥{config.SCORE_QUANT_SIGNAL_MIN}): "
              f"{sorted(finalists)}")

        # 2차 게이트 근접 상위 — 1차는 통과했으나 총점 미달(시그널 임계 점검용)
        signal_near_misses = [
            {"ticker": t, "name": pre_finalists[t].get("name"),
             "rsi": pre_finalists[t].get("rsi"),
             "total_signal": round(q["total_signal"], 2)}
            for t, q in sorted(signal_scored.items(), key=lambda kv: -kv[1]["total_signal"])
            if t not in finalists][:5]
        for n in signal_near_misses:
            print(f"[trigger_a] signal near-miss {n['ticker']} {n['name']} "
                  f"total_signal {n['total_signal']} (기준 {config.SCORE_QUANT_SIGNAL_MIN})")

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
                ctx = market.assess_decline(stock_dd, mkt_dd, b,
                                            window_label="최근 60거래일")
                # β·낙폭 원자료도 보존 — 트리거 B가 시장 기여도를 항상 표기할 수 있도록
                ctx.update({"beta": b, "stock_dd": stock_dd, "market_dd": mkt_dd})
                finalists[t]["market_context"] = ctx

        checkpoint = {"date": date_str, "finalists": {}, "batch_id": None,
                      "fin_basis": fin_basis, "oversold_count": len(oversold),
                      "pre_finalist_count": len(pre_finalists),
                      "near_misses": _jsonable(near_misses),
                      "signal_near_misses": _jsonable(signal_near_misses),
                      "peers_size": {k: len(v) for k, v in peers.items()}}

        if finalists:
            # ⑤ 정성 자료 수집(정기보고서 본문·수시공시 원문·뉴스·트렌드·수출통계) —
            # 매수 시그널 통과 종목 한정. 배당·자기주식·내부자·최근공시 목록은
            # 앞 단계(pre_docs)에서 이미 받아온 것을 재사용한다.
            docs: dict[str, dict] = {}
            for t in finalists:
                corp = corp_by.get(t)
                disclosures = pre_docs[t]["disclosures"]
                execs = pre_docs[t]["executives"]
                periodic = None
                disclosure_texts: list[dict] = []
                if corp:
                    # 정기보고서 본문(사업의 내용·MD&A) — B4·D3·F1 그라운딩의 핵심 원문.
                    # 제목 목록만 주면 LLM이 전 항목 '근거 부재'가 된다. best-effort.
                    try:
                        per = dart.get_latest_periodic(corp, date_str)
                        if per:
                            body = dart.get_document_text(per["rcept_no"])
                            periodic = {**per,
                                        "text": dart.extract_business_sections(body)}
                    except RuntimeError as e:
                        print(f"[trigger_a] periodic fetch failed {t}: {e}")
                    # 최근 수시공시 상위 2건 본문 — D2(급락 원인) 근거 보강. best-effort.
                    for d0 in disclosures[:2]:
                        try:
                            disclosure_texts.append({
                                "report_nm": d0.get("report_nm"),
                                "rcept_dt": d0.get("rcept_dt"),
                                "rcept_no": d0.get("rcept_no"),
                                "text": dart.get_document_text(d0["rcept_no"])[:1500]})
                        except RuntimeError as e:
                            print(f"[trigger_a] disclosure body failed {t} "
                                  f"{d0.get('rcept_no')}: {e}")
                # 뉴스 헤드라인 (tier 3) — D2 급락 사유 보조 근거. best-effort.
                news_items: list[dict] = []
                nm = (eod.get(t) or {}).get("name") or t
                try:
                    news_items = news.search_news(f"{nm} 주가")
                except Exception as e:                # noqa: BLE001
                    print(f"[trigger_a] 뉴스 검색 실패(무시) {t}: {e}")
                trend_note = None
                try:
                    tr = trends.search_trend(nm)
                    trend_note = tr["note"] if tr else None
                except Exception as e:                # noqa: BLE001
                    print(f"[trigger_a] 트렌드 조회 실패(무시) {t}: {e}")
                # 수출 통계 (관세청 — 전국 품목 + 시군구 프록시, best-effort).
                # 구조적 실적 훼손/성장성 판단 근거(D2·D3) — 점수 직접 반영 없음.
                trade_note = None
                if (config.CUSTOMS_COUNTRY_API_KEY and config.ANTHROPIC_API_KEY
                        and periodic and periodic.get("text")):
                    try:
                        hs = llm.extract_hs(periodic["text"])
                        notes = []
                        if hs:
                            ti = trade.export_yoy(hs["hs"], hs.get("product"))
                            if ti:
                                notes.append(ti["note"])
                            if hs.get("sido") and hs.get("sgg"):
                                ri = trade.region_export_yoy(
                                    hs["hs6"], hs["sido"], hs["sgg"])
                                if ri:
                                    notes.append(ri["note"])
                        trade_note = "\n".join(notes) or None
                    except Exception as e:            # noqa: BLE001
                        print(f"[trigger_a] 수출 통계 조회 실패(무시) {t}: {e}")
                docs[t] = {"disclosures": disclosures, "executives": execs,
                           "periodic": periodic,
                           "disclosure_texts": disclosure_texts,
                           "news": news_items,
                           "trend_note": trend_note,
                           "trade_note": trade_note}
                storage.save_json(docs[t], f"delta/{date_str}_{t}.json")

            # ⑥ LLM: 그라운딩 대상은 매수 시그널 총점 상위 LLM_MAX개 한정(안전상한)
            #    Haiku 추출(동기·저렴) → Sonnet 채점 Batch 제출(async, −50%)
            llm_targets = sorted(
                finalists, key=lambda t: -finalists[t]["quant_signal"]["total_signal"])
            llm_targets = llm_targets[:config.LLM_MAX]
            extracted = llm.extract_passages({t: docs[t] for t in llm_targets})
            batch_id = llm.submit_batch(extracted)
            checkpoint["batch_id"] = batch_id
            checkpoint["llm_targets"] = llm_targets
            print(f"[trigger_a] batch submitted: {batch_id} (targets={llm_targets})")

        checkpoint["finalists"] = _jsonable(finalists)
        storage.save_json(checkpoint, f"checkpoints/trigger_a_{date_str}.json")
        print(f"[trigger_a] checkpoint saved for {date_str}")

        # 하트비트 — A단계 생존 확인(후보→1차통과→시그널→배치). B는 당일 이 체크포인트로 발송.
        notify.send_heartbeat(
            notify.header_heartbeat(run_date)
            + f"\nA: {note} · RSI후보 {len(oversold)} → 1차정량통과 {len(pre_finalists)}"
              f" → 매수시그널 {len(finalists)}"
            + (f" → 배치제출 {len(checkpoint.get('llm_targets') or [])}"
               if checkpoint.get("batch_id") else " → 배치 없음(시그널 0)"))
        return 0
    except Exception:
        notify.notify_failure("trigger_a", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
