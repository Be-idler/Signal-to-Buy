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

import os

import config

_BASE = "https://data-dbg.krx.co.kr/svc/apis"
_ENDPOINTS = ("sto/stk_bydd_trd", "sto/ksq_bydd_trd")   # KOSPI, KOSDAQ

# KRX OpenAPI 인프라가 간헐적으로 느려지는(read timeout) 구간이 실재한다. 저하
# 구간을 견디도록 재시도·타임아웃을 넉넉히 잡는다(연결 10s / 읽기 45s, 5회 재시도).
_KRX_CONNECT_TIMEOUT = float(os.environ.get("KRX_CONNECT_TIMEOUT", "10"))
_KRX_READ_TIMEOUT = float(os.environ.get("KRX_READ_TIMEOUT", "45"))
_KRX_RETRIES = int(os.environ.get("KRX_RETRIES", "5"))


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


def _call(endpoint: str, bas_dd: str, retries: int | None = None) -> list[dict]:
    headers = {"AUTH_KEY": config.KRX_API_KEY, "Content-Type": "application/json"}
    retries = _KRX_RETRIES if retries is None else retries
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{_BASE}/{endpoint}", json={"basDd": bas_dd},
                              headers=headers,
                              timeout=(_KRX_CONNECT_TIMEOUT, _KRX_READ_TIMEOUT))
            r.raise_for_status()
            return r.json().get("OutBlock_1", []) or []
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 20))   # 지수 백오프(상한 20s)
    raise RuntimeError(f"KRX API failed {endpoint} {bas_dd}: {last_err}")


def get_market_snapshot(trd_dd: str, retries: int | None = None) -> list[dict]:
    """단일 거래일 전 종목(유가+코스닥) 시세 스냅샷. 휴장일이면 빈 리스트.

    반환 행: ticker, name, close, mktcap, volume, value(거래대금), market.
    retries: 필수 아닌(건너뛸 수 있는) 과거일엔 낮게 줘 저하 구간 시간낭비를 줄인다.
    """
    out: list[dict] = []
    for ep in _ENDPOINTS:
        for row in _call(ep, trd_dd, retries=retries):
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


def kst_today() -> dt.date:
    """현재 KST 날짜. 러너가 UTC라도 한국 장 기준일을 정확히 잡는다.

    트랙1 크론은 KST 08:05(=UTC 23:05 전일)에 발화하므로, UTC 날짜를 그대로 쓰면
    기준일이 하루 어긋난다(월→금 대신 금→목 등). 반드시 KST로 환산해 앵커한다.
    """
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).date()


def prev_weekday(d: dt.date) -> dt.date:
    """d 이전(당일 제외) 가장 최근 평일(월~금). 휴장 여부는 보지 않는다."""
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:                       # 토(5)·일(6)
        d -= dt.timedelta(days=1)
    return d


def previous_trading_session(today: dt.date) -> str | None:
    """`today` 기준 **전영업일**(당일 제외) — 시세가 있는 가장 최근 거래일(YYYYMMDD).

    KRX OpenAPI는 시세를 익영업일 오전에 발행하므로, 트랙1은 항상 당일이 아닌
    전영업일 데이터를 쓴다. 월요일이면 금요일, 직전 평일이 휴장이면 그 이전
    시세 보유 거래일로 자동 소급된다(is_trading_day = 데이터 존재 여부).
    """
    days = recent_trading_days(today - dt.timedelta(days=1), 1)
    return days[-1] if days else None


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

    # ① 기준일 스냅샷은 필수 — 없으면 신호를 낼 수 없으므로 명확히 raise한다
    #    (get_market_snapshot 내부 _call이 이미 재시도. 그래도 실패면 발행 지연·저하).
    end_str = end_date.strftime("%Y%m%d")
    if end_str not in snapshots:
        snapshots[end_str] = get_market_snapshot(end_str)
    if not snapshots.get(end_str):
        raise RuntimeError(f"KRX 기준일 {end_str} 시세 없음(휴장·발행 지연·API 저하)")

    # ② 과거 거래일은 RSI 계산용 — best-effort. 빈 응답=휴장(캐시), 조회 실패=건너뜀.
    #    일부 과거일이 빠져도 남은 날로 진행한다(전체 중단 방지). 별도 is_trading_day
    #    프로브 없이 스냅샷 조회로 거래일 판정을 겸해 KRX 호출 수도 줄인다.
    trading_days = [end_str]
    failures: list[str] = []
    d = end_date - dt.timedelta(days=1)
    scanned = 0
    while len(trading_days) < days and scanned < days * 3 + 40:
        if d.weekday() < 5:
            trd = d.strftime("%Y%m%d")
            if trd in snapshots:
                if snapshots[trd]:
                    trading_days.append(trd)
            else:
                try:
                    # 과거일은 건너뛸 수 있으므로 재시도를 낮춰 저하 구간 시간낭비를 줄인다
                    snap = get_market_snapshot(trd, retries=2)
                    snapshots[trd] = snap                  # 빈=휴장도 캐시(재조회 방지)
                    if snap:
                        trading_days.append(trd)
                except RuntimeError as e:                  # noqa: BLE001
                    failures.append(trd)
                    print(f"[krx] 과거 스냅샷 {trd} 조회 실패(건너뜀): {e}")
                time.sleep(0.2)
        d -= dt.timedelta(days=1)
        scanned += 1
    trading_days.reverse()                                 # 과거→최근
    if failures:
        print(f"[krx] 과거 스냅샷 {len(failures)}일 실패(RSI 표본 축소 가능): "
              f"{failures[:8]}")

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
