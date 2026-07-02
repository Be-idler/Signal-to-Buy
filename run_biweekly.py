"""트랙2 (격주 주말, 봇2) — 4개 프레임워크 전수 랭킹 다이제스트.

LTGG·아웃사이더·마법공식·버핏멍거. RSI 게이트 없음(랭킹 기반, §2).
전수 조회는 storage.sync_prefix_to_local로 Drive parquet을 내려받아
DuckDB 로컬 쿼리로 수행한다(Drive는 httpfs 원격 직접 쿼리 불가).

정성(LLM) 하위지표는 이 배치에서 2.5 캡으로 남는다 — §10 원칙상 LLM은
게이트 통과 finalists에만 적용(비용). 정성 반영 심화 분석은 수동/후속.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import traceback

import duckdb
import pandas as pd

import config
from dhandho import frameworks, krx, metrics, notify, storage

FRAMEWORKS = ("magic_formula", "buffett", "outsiders", "ltgg")
FRAMEWORK_LABEL = {"magic_formula": "마법공식(바스켓)", "buffett": "버핏·멍거",
                   "outsiders": "아웃사이더", "ltgg": "LTGG"}


def _load_universe() -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Drive → 로컬 캐시 → DuckDB로 전수 재무 로드."""
    paths = storage.sync_prefix_to_local("financials")
    if not paths:
        raise RuntimeError("no financials in storage — run_quarterly first")
    glob = os.path.join(config.LOCAL_CACHE_DIR, "financials", "*.parquet")
    con = duckdb.connect()
    df = con.execute(
        f"SELECT *, regexp_extract(filename, '(\\d{{4}})_(\\d{{5}})', 1) AS bsns_year, "
        f"regexp_extract(filename, '(\\d{{4}})_(\\d{{5}})', 2) AS reprt "
        f"FROM read_parquet('{glob}', filename=true)").fetchdf()

    # 최신 보고서(연도·보고서코드 최신순) 1행 + 과거 사업보고서 history
    df["bsns_year"] = df["bsns_year"].astype(int)
    latest_rows: dict[str, dict] = {}
    history: dict[str, list[dict]] = {}
    all_rows: dict[tuple, dict] = {}                      # (ticker, year, reprt) → fin
    order = {"11011": 0, "11013": 1, "11012": 2, "11014": 3}   # 연내 시간 순
    df = df.sort_values(["ticker", "bsns_year", "reprt"],
                        key=lambda s: s.map(order) if s.name == "reprt" else s)
    for _, row in df.iterrows():
        d = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
        d["flags"] = d["flags"].split(";") if d.get("flags") else []
        t = d["ticker"]
        all_rows[(t, d["bsns_year"], d["reprt"])] = d
        latest_rows[t] = d                                # 정렬상 마지막이 최신
        if d["reprt"] == "11011":
            history.setdefault(t, []).append(d)
    for t in history:
        if history[t] and latest_rows[t] is history[t][-1]:
            history[t] = history[t][:-1]                  # 최신 연간이 latest면 중복 제거

    # 보고서 기준 불일치 보정: 최신이 분기/반기면 기간 항목을 TTM으로 변환
    for t, d in list(latest_rows.items()):
        if d["reprt"] != "11011":
            y = d["bsns_year"]
            latest_rows[t] = metrics.build_ttm(
                d, all_rows.get((t, y - 1, "11011")),
                all_rows.get((t, y - 1, d["reprt"])))
    return latest_rows, history


def _digest(framework: str, ranked: list[tuple[str, dict]],
            info: dict[str, dict]) -> str:
    lines = [f"◆ {FRAMEWORK_LABEL[framework]} 상위 {config.BIWEEKLY_TOP_N}"]
    shown = 0
    for ticker, r in ranked:
        score = r.get("score") if framework == "magic_formula" else r.get("total")
        if score is None or r.get("excluded"):
            continue
        name = (info.get(ticker) or {}).get("name", "")
        gates = r.get("gates")
        gate_txt = "" if gates is None else (" ✓" if all(gates.values()) else " (게이트 미통과)")
        lines.append(f"{shown + 1}. {ticker} {name} — {score:.2f} [{r.get('grade')}]{gate_txt}")
        shown += 1
        if shown >= config.BIWEEKLY_TOP_N:
            break
    if framework == "magic_formula":
        lines.append("※ 바스켓 랭킹 도구 — 단일 종목 확신 아님(§5.4)")
    return "\n".join(lines)


def main() -> int:
    date_str = dt.date.today().strftime("%Y%m%d")
    try:
        fin_by_ticker, history = _load_universe()

        # 최근 거래일 시총 결합
        days = krx.recent_trading_days(dt.date.today(), 1)
        snapshot = krx.get_market_snapshot(days[-1])
        info = {r["ticker"]: r for r in snapshot}

        metrics_all: dict[str, dict] = {}
        for t, fin in fin_by_ticker.items():
            mktcap = (info.get(t) or {}).get("mktcap")
            metrics_all[t] = metrics.compute_derived(fin, mktcap=mktcap,
                                                     history=history.get(t) or None)

        parts = [f"🗂 다관점 격주 랭킹 {date_str} (유니버스 {len(metrics_all)}종목)",
                 "※ 관점별 독립 점수 — 평균·합산 금지(§6). 미검증 임계, 판단은 사람."]
        scores_rows = []
        for fw in FRAMEWORKS:
            ranked = frameworks.rank_universe(metrics_all, fw, info_by_ticker=info)
            parts.append(_digest(fw, ranked, info))
            for t, r in ranked:
                scores_rows.append({"ticker": t, "framework": fw,
                                    "score": r.get("score") or r.get("total"),
                                    "grade": r.get("grade"),
                                    "excluded": r.get("excluded"),
                                    "date": date_str})

        storage.upload_parquet(pd.DataFrame(scores_rows),
                               f"scores/multiframework_{date_str}.parquet")
        notify.send_bot2("\n\n".join(parts))
        print(f"[biweekly] digest sent ({len(metrics_all)} tickers)")
        return 0
    except Exception:
        notify.notify_failure("biweekly", traceback.format_exc(), bot=2)
        raise


if __name__ == "__main__":
    sys.exit(main())
