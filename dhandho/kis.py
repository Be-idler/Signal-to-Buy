"""KIS(한국투자증권) OpenAPI — 당일 종가 병렬 수집 (시세 전용, 주문 미호출).

KRX가 시세를 익영업일 오전에 발행하는 한계 때문에, 당일 장 종료(15:30) 직후
종가를 KIS로 받아 KRX 60일 히스토리에 이어붙여 **당일 기준** RSI·스코어링을
가능하게 한다(하이브리드). KRX는 여전히 60일 히스토리·시총의 SSOT.

프로브 실측(2026-07): 개인 실전 조회 유량 상한 ~2~3 req/s(EGW00201). 동시성 2~3
+ 글로벌 레이트리밋 + 유량초과 재시도가 최적 — 2,000종목 ~13분(30분 창 충족).

보안: APP_KEY/SECRET은 os.environ으로만, 로그 미출력. 주문/계좌 엔드포인트는
절대 호출하지 않는다(신호 전용 원칙).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import config

_PROD = "https://openapi.koreainvestment.com:9443"
_PAPER = "https://openapivts.koreainvestment.com:29443"


def _base() -> str:
    return _PAPER if config.KIS_ENV.strip().lower() in ("paper", "vts") else _PROD


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


class _RateLimiter:
    """글로벌 최소 간격 제한(스레드 안전) — 초당 rate건으로 상한."""

    def __init__(self, rate: float):
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self._interval


def get_token() -> str:
    """접근토큰 발급(24h 유효). 실행당 1회 발급 — 발급 유량(분당 1건) 여유.

    실패 시 RuntimeError. 토큰 문자열만 반환(로그에 남기지 않는다).
    """
    if not config.KIS_APP_KEY or not config.KIS_APP_SECRET:
        raise RuntimeError("KIS_APP_KEY/KIS_APP_SECRET 미설정")
    r = requests.post(f"{_base()}/oauth2/tokenP",
                      json={"grant_type": "client_credentials",
                            "appkey": config.KIS_APP_KEY,
                            "appsecret": config.KIS_APP_SECRET},
                      timeout=(10, 30))
    if r.status_code != 200:
        raise RuntimeError(f"KIS 토큰 발급 실패 HTTP {r.status_code}: {r.text[:150]}")
    tok = r.json().get("access_token")
    if not tok:
        raise RuntimeError("KIS 토큰 응답에 access_token 없음")
    return tok


def _inquire(token: str, ticker: str, limiter: _RateLimiter,
             retries: int = 5) -> dict | None:
    """단건 현재가(마감 후 = 확정 종가). EGW00201(유량초과)은 백오프 재시도.

    반환: krx.get_market_snapshot 행과 동일 스키마
      {ticker, name, close, mktcap(원), volume, value, market} | None(실패).
    """
    headers = {"authorization": f"Bearer {token}",
               "appkey": config.KIS_APP_KEY, "appsecret": config.KIS_APP_SECRET,
               "tr_id": "FHKST01010100", "custtype": "P"}
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    for attempt in range(retries):
        limiter.wait()
        try:
            r = requests.get(
                f"{_base()}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=headers, params=params, timeout=(10, 30))
        except requests.RequestException:
            time.sleep(min(2 ** attempt * 0.3, 5))
            continue
        if r.status_code == 200:
            data = r.json()
            if data.get("rt_cd") == "0":
                o = data.get("output") or {}
                avls = _num(o.get("hts_avls"))          # 시총 억원 → 원
                return {
                    "ticker": ticker, "name": o.get("hts_kor_isnm") or None,
                    "close": _num(o.get("stck_prpr")),
                    "mktcap": avls * 1e8 if avls is not None else None,
                    "volume": _num(o.get("acml_vol")),
                    "value": _num(o.get("acml_tr_pbmn")),
                    "market": None,
                }
            # 유량초과면 백오프 후 재시도, 그 외 rt_cd 오류는 즉시 중단
            if "EGW00201" not in r.text:
                return None
        time.sleep(min(2 ** attempt * 0.3, 5))          # EGW00201·5xx 백오프
    return None


def fetch_snapshot(tickers: list[str], token: str | None = None,
                   workers: int | None = None,
                   rate: float | None = None) -> list[dict]:
    """전 종목 당일 종가·시총 병렬 수집 → get_market_snapshot 호환 행 목록.

    workers/rate 미지정 시 config 기본값(프로브 실측 최적: 동시성 3, ~2.5 req/s).
    실패 종목은 결과에서 빠진다(호출부가 KRX 히스토리로 보완·판정).
    """
    if not tickers:
        return []
    token = token or get_token()
    workers = workers or config.KIS_MAX_WORKERS
    rate = rate or config.KIS_RATE_LIMIT
    limiter = _RateLimiter(rate)
    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for row in ex.map(lambda t: _inquire(token, t, limiter), tickers):
            if row and row.get("close") is not None:
                out.append(row)
    return out
