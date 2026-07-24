"""KIS(한국투자증권) API 0단계 프로브 — 시세 배치 도입 전 실측용(주문 미호출).

검증 항목(모두 시세 조회 전용, 계좌·주문 절대 안 건드림):
  1) 접근토큰 발급 성공 여부 = GitHub Actions 러너(해외 IP) 차단 여부
  2) inquire-price(현재가=마감 후 확정 종가) 응답 형식·필드 확인
  3) 순차 호출 처리량(req/s) 측정 — 전 종목(~2,700) 수집 소요시간 추정

비밀키는 절대 출력하지 않는다(환경변수로만 읽고, 실패 시 상태코드·요약만 표기).
"""
from __future__ import annotations

import json
import os
import time

import requests

APP_KEY = os.environ.get("KIS_APP_KEY", "")
APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
ENV = os.environ.get("KIS_ENV", "prod").strip().lower()

# 실전/모의 도메인 — 시세는 실전 도메인에서만 정상 유량. paper는 참고용.
BASE = ("https://openapivts.koreainvestment.com:29443" if ENV in ("paper", "vts")
        else "https://openapi.koreainvestment.com:9443")

# 프로브용 소수 종목 (삼성전자·SK하이닉스·NAVER·카카오·현대차)
TEST_TICKERS = ["005930", "000660", "035420", "035720", "005380"]


def _fail(msg: str) -> int:
    print(f"❌ {msg}")
    return 1


def get_token() -> str | None:
    """접근토큰 발급 — 성공하면 해외 IP 차단 아님. 실패 시 원인 요약."""
    if not APP_KEY or not APP_SECRET:
        _fail("KIS_APP_KEY/KIS_APP_SECRET 환경변수 없음 — Secrets 등록 확인")
        return None
    t0 = time.time()
    try:
        r = requests.post(f"{BASE}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": APP_KEY, "appsecret": APP_SECRET},
                          timeout=(10, 30))
    except requests.RequestException as e:
        _fail(f"토큰 요청 네트워크 실패(차단·타임아웃 의심): {type(e).__name__}: {e}")
        return None
    dt = time.time() - t0
    if r.status_code != 200:
        # 본문에 키가 담기지 않음 — 상태코드·에러코드만 노출
        body = r.text[:300]
        _fail(f"토큰 발급 실패 HTTP {r.status_code} ({dt:.1f}s): {body}")
        if r.status_code in (401, 403):
            print("   → 401/403: 앱키/시크릿 불일치 또는 IP 접근제한 가능성")
        return None
    data = r.json()
    tok = data.get("access_token")
    print(f"✅ 토큰 발급 성공 ({dt:.1f}s) — 해외 IP 차단 아님, "
          f"만료 {data.get('expires_in')}초(~24h)")
    return tok


def inquire_price(token: str, ticker: str) -> dict | None:
    """현재가(마감 후 = 확정 종가) 단건 조회."""
    r = requests.get(
        f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers={"authorization": f"Bearer {token}",
                 "appkey": APP_KEY, "appsecret": APP_SECRET,
                 "tr_id": "FHKST01010100", "custtype": "P"},
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        timeout=(10, 30))
    if r.status_code != 200:
        print(f"   {ticker} HTTP {r.status_code}: {r.text[:150]}")
        return None
    data = r.json()
    if data.get("rt_cd") != "0":
        print(f"   {ticker} rt_cd={data.get('rt_cd')} "
              f"msg_cd={data.get('msg_cd')} {data.get('msg1')}")
        return None
    return data.get("output") or {}


def main() -> int:
    print(f"[kis_probe] ENV={ENV} BASE={BASE}")
    token = get_token()
    if not token:
        return 1

    # 2) 응답 형식 확인 — 대표 종목 몇 개
    print("\n── 응답 형식 확인 (현재가=확정 종가) ──")
    ok = 0
    for t in TEST_TICKERS:
        out = inquire_price(token, t)
        if out:
            ok += 1
            print(f"   {t} {out.get('hts_kor_isnm')}: 종가 "
                  f"{out.get('stck_prpr')}원, 전일대비 {out.get('prdy_ctrt')}%, "
                  f"거래량 {out.get('acml_vol')}, 시총 {out.get('hts_avls')}(억)")
        time.sleep(0.1)
    if not ok:
        return _fail("현재가 조회 전건 실패 — tr_id·파라미터·권한 확인 필요")
    print(f"   → {ok}/{len(TEST_TICKERS)}종목 정상")

    # 3) 처리량 측정 — 30회 순차 호출(초당 유량·차단 여부 실측)
    print("\n── 처리량 측정 (30회 순차) ──")
    n, t0, errs = 30, time.time(), 0
    for i in range(n):
        out = inquire_price(token, TEST_TICKERS[i % len(TEST_TICKERS)])
        if out is None:
            errs += 1
    elapsed = time.time() - t0
    rps = n / elapsed if elapsed else 0
    print(f"   {n}회 / {elapsed:.1f}s = {rps:.1f} req/s (실패 {errs})")
    est = 2700 / rps if rps else float("inf")
    print(f"   → 전 종목(~2,700) 추정 소요: {est:.0f}s (~{est/60:.1f}분)")
    if errs > n * 0.2:
        print("   ⚠️ 실패율 높음 — 유량 초과(EGW00201)·간헐 차단 가능, 간격 조정 필요")
    else:
        print("   ✅ 처리량 안정 — 30분 배치 창 내 전 종목 수집 가능")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
