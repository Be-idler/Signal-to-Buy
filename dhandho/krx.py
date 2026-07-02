"""KRX 일일 EOD 가격 + 시가총액 수집 (정보데이터시스템 JSON API).

⚠️ 시총은 매일 변하므로 분기 재무 SSOT와 분리해 매 영업일 수집한다(지시문 2단계).
run_quarterly는 시총 없이 재무만 저장하고, 시총 결합은 트리거 A에서 수행한다.
"""
from __future__ import annotations

import datetime as dt
import time

import requests

_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
}


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


def get_market_snapshot(trd_dd: str, retries: int = 3) -> list[dict]:
    """단일 거래일 전 종목 시세 스냅샷. trd_dd: YYYYMMDD.

    반환 행: ticker, name, close, mktcap, volume, sector(시장구분).
    휴장일이면 빈 리스트.
    """
    payload = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
        "locale": "ko_KR", "mktId": "ALL", "trdDd": trd_dd,
        "share": "1", "money": "1", "csvxls_isNo": "false",
    }
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(_URL, data=payload, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            rows = r.json().get("OutBlock_1", [])
            out = []
            for row in rows:
                out.append({
                    "ticker": row.get("ISU_SRT_CD"),
                    "name": row.get("ISU_ABBRV"),
                    "close": _num(row.get("TDD_CLSPRC")),
                    "mktcap": _num(row.get("MKTCAP")),
                    "volume": _num(row.get("ACC_TRDVOL")),
                    "market": row.get("MKT_NM"),
                })
            return out
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"KRX snapshot failed for {trd_dd}: {last_err}")


def is_trading_day(trd_dd: str) -> bool:
    """해당 일자에 시세가 존재하는가 (KRX 휴장일은 평일이어도 스킵)."""
    return len(get_market_snapshot(trd_dd)) > 0


def recent_trading_days(end_date: dt.date, count: int) -> list[str]:
    """end_date부터 거슬러 최근 count개 거래일(YYYYMMDD, 과거→최근 순).

    주말은 건너뛰고, 평일 휴장은 스냅샷 존재 여부로 판정한다.
    """
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


def get_all_eod(days: int = 60, end_date: dt.date | None = None,
                snapshots: dict[str, list[dict]] | None = None) -> dict[str, dict]:
    """전 종목 최근 ~days 거래일 종가 + 당일 시가총액.

    반환: (eod, snapshots)
      eod: {ticker: {"closes": [과거→최근], "mktcap": float, "name": str,
                     "market": str, "halted": bool}}
      snapshots: {일자: 스냅샷 행 목록} — 일별 parquet 적재용
    `snapshots` 인자에 {일자: 스냅샷}을 넘기면 해당 일자는 재조회하지 않는다
    (storage에 적재된 과거 eod parquet 재사용용).
    """
    end_date = end_date or dt.date.today()
    snapshots = dict(snapshots or {})
    trading_days = recent_trading_days(end_date, days)

    for trd in trading_days:
        if trd not in snapshots:
            snapshots[trd] = get_market_snapshot(trd)
            time.sleep(0.3)          # KRX 서버 부하 완화

    out: dict[str, dict] = {}
    last_day = trading_days[-1]
    for trd in trading_days:
        for row in snapshots[trd]:
            t = row["ticker"]
            if not t:
                continue
            rec = out.setdefault(t, {"closes": [], "mktcap": None,
                                     "name": row.get("name"),
                                     "market": row.get("market"),
                                     "halted": False})
            if row["close"] is not None:
                rec["closes"].append(row["close"])
            if trd == last_day:
                rec["mktcap"] = row["mktcap"]
                # 당일 종가 결측 = 거래정지/관리 추정 → 플래그
                if row["close"] is None:
                    rec["halted"] = True

    # 시계열이 지나치게 짧은 종목(신규상장 등)은 결측 플래그
    for rec in out.values():
        if len(rec["closes"]) < days // 2:
            rec["halted"] = rec["halted"] or False
            rec["short_history"] = True
    return out, snapshots
