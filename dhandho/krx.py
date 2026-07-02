"""KRX 일일 EOD 가격 + 시가총액 수집 — KRX 공식 Open API (v1 검증 로직 이식).

- 호스트 https://data-dbg.krx.co.kr/svc/apis 에 POST(JSON 바디), `AUTH_KEY` 헤더 인증.
- 엔드포인트: 유가증권 sto/stk_bydd_trd, 코스닥 sto/ksq_bydd_trd (일별매매정보, basDd).
- 인증키 발급과 별개로 데이터셋별 이용신청이 필요하다(v1 README 참조).

⚠️ 시총은 매일 변하므로 분기 재무 SSOT와 분리해 매 영업일 수집한다.
run_quarterly는 시총 없이 재무만 저장하고, 시총 결합은 트리거 A에서 수행한다.
"""
from __future__ import annotations

import datetime as dt
import time

import requests

import config

_BASE = "https://data-dbg.krx.co.kr/svc/apis"
_ENDPOINTS = ("sto/stk_bydd_trd", "sto/ksq_bydd_trd")   # KOSPI, KOSDAQ


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _call(endpoint: str, bas_dd: str, retries: int = 3) -> list[dict]:
    headers = {"AUTH_KEY": config.KRX_API_KEY, "Content-Type": "application/json"}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{_BASE}/{endpoint}", json={"basDd": bas_dd},
                              headers=headers, timeout=30)
            r.raise_for_status()
            return r.json().get("OutBlock_1", []) or []
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"KRX API failed {endpoint} {bas_dd}: {last_err}")


def get_market_snapshot(trd_dd: str) -> list[dict]:
    """단일 거래일 전 종목(유가+코스닥) 시세 스냅샷. 휴장일이면 빈 리스트.

    반환 행: ticker, name, close, mktcap, volume, value(거래대금), market.
    """
    out: list[dict] = []
    for ep in _ENDPOINTS:
        for row in _call(ep, trd_dd):
            out.append({
                "ticker": (row.get("ISU_CD") or "").strip(),
                "name": row.get("ISU_NM"),
                "close": _num(row.get("TDD_CLSPRC")),
                "mktcap": _num(row.get("MKTCAP")),
                "volume": _num(row.get("ACC_TRDVOL")),
                "value": _num(row.get("ACC_TRDVAL")),
                "market": row.get("MKT_NM") or ("KOSPI" if "stk" in ep else "KOSDAQ"),
            })
    return out


def is_trading_day(trd_dd: str) -> bool:
    """해당 일자에 시세가 존재하는가 (KRX 휴장일은 평일이어도 스킵)."""
    return len(_call(_ENDPOINTS[0], trd_dd)) > 0


def recent_trading_days(end_date: dt.date, count: int) -> list[str]:
    """end_date부터 거슬러 최근 count개 거래일(YYYYMMDD, 과거→최근 순)."""
    days: list[str] = []
    d = end_date
    scanned = 0
    while len(days) < count and scanned < count * 3 + 30:
        if d.weekday() < 5:
            trd = d.strftime("%Y%m%d")
            if is_trading_day(trd):
                days.append(trd)
        d -= dt.timedelta(days=1)
        scanned += 1
    return list(reversed(days))


def is_common_stock(ticker: str) -> bool:
    """보통주 판정 — 한국 종목코드 끝자리 0 = 보통주 (우선주·신주인수권 등 제외)."""
    return bool(ticker) and ticker[-1] == "0"


def passes_liquidity(rec: dict) -> bool:
    """v1 L1 유동성/품질 필터: 보통주만 + 20일 평균 거래대금 ≥ 1억 + 당일 데이터 존재."""
    if config.LIQ_COMMON_ONLY and not is_common_stock(rec.get("ticker", "")):
        return False
    if rec.get("halted"):
        return False
    values = (rec.get("values") or [])[-config.LIQ_WINDOW:]
    values = [v for v in values if v is not None]
    if not values or sum(values) / len(values) < config.LIQ_MIN_VALUE:
        return False
    return True


def get_all_eod(days: int = 60, end_date: dt.date | None = None,
                snapshots: dict[str, list[dict]] | None = None) -> tuple[dict, dict]:
    """전 종목 최근 ~days 거래일 종가·거래대금 + 당일 시가총액.

    반환: (eod, snapshots)
      eod: {ticker: {"ticker","closes","values","mktcap","name","market","halted"}}
      snapshots: {일자: 스냅샷 행 목록} — 일별 parquet 적재용
    `snapshots` 인자에 기존 {일자: 스냅샷}을 넘기면 해당 일자는 재조회하지 않는다.
    """
    end_date = end_date or dt.date.today()
    snapshots = dict(snapshots or {})
    trading_days = recent_trading_days(end_date, days)

    for trd in trading_days:
        if trd not in snapshots:
            snapshots[trd] = get_market_snapshot(trd)
            time.sleep(0.2)

    out: dict[str, dict] = {}
    last_day = trading_days[-1]
    for trd in trading_days:
        for row in snapshots[trd]:
            t = row["ticker"]
            if not t:
                continue
            rec = out.setdefault(t, {"ticker": t, "closes": [], "values": [],
                                     "mktcap": None, "name": row.get("name"),
                                     "market": row.get("market"), "halted": False})
            if row["close"] is not None:
                rec["closes"].append(row["close"])
                rec["values"].append(row.get("value"))
            if trd == last_day:
                rec["mktcap"] = row["mktcap"]
                if row["close"] is None:
                    rec["halted"] = True     # 당일 종가 결측 = 거래정지/관리 추정

    for rec in out.values():
        if len(rec["closes"]) < days // 2:
            rec["short_history"] = True
    return out, snapshots
