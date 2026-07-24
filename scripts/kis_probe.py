"""KIS 처리량 정밀 프로브 — 동시성별 유효 req/s·유량초과(EGW00201) 실측.

목적: 전 종목(~2,000 보통주) 당일 종가를 30분 창 안에 안전히 수집할
최적 동시 스레드 수와 초당 상한을 찾는다. 시세 조회 전용(주문 미호출),
비밀키는 출력하지 않는다.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

APP_KEY = os.environ.get("KIS_APP_KEY", "")
APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
ENV = os.environ.get("KIS_ENV", "prod").strip().lower()
BASE = ("https://openapivts.koreainvestment.com:29443" if ENV in ("paper", "vts")
        else "https://openapi.koreainvestment.com:9443")

# 조회 반복용 대표 종목(멱등 — 반복 조회 무해). 실제 배치는 전 종목.
TICKERS = ["005930", "000660", "035420", "035720", "005380",
           "051910", "006400", "005490", "000270", "012330"]


def get_token() -> str | None:
    if not APP_KEY or not APP_SECRET:
        print("❌ KIS_APP_KEY/SECRET 없음")
        return None
    try:
        r = requests.post(f"{BASE}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": APP_KEY, "appsecret": APP_SECRET},
                          timeout=(10, 30))
    except requests.RequestException as e:
        print(f"❌ 토큰 네트워크 실패: {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        print(f"❌ 토큰 실패 HTTP {r.status_code}: {r.text[:200]}")
        return None
    print(f"✅ 토큰 발급 성공 (만료 {r.json().get('expires_in')}초)")
    return r.json().get("access_token")


def _one(token: str, ticker: str) -> tuple[bool, bool]:
    """(성공, 유량초과) 반환. 유량초과(EGW00201)와 기타 오류를 구분."""
    try:
        r = requests.get(
            f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={"authorization": f"Bearer {token}",
                     "appkey": APP_KEY, "appsecret": APP_SECRET,
                     "tr_id": "FHKST01010100", "custtype": "P"},
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
            timeout=(10, 30))
    except requests.RequestException:
        return False, False
    if r.status_code == 200 and r.json().get("rt_cd") == "0":
        return True, False
    rate = "EGW00201" in r.text        # 초당 거래건수 초과
    return False, rate


def measure(token: str, workers: int, total: int = 40) -> dict:
    """동시성 workers로 total건 요청 — 유효 req/s·유량초과율 측정."""
    t0 = time.time()
    ok = over = err = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_one, token, TICKERS[i % len(TICKERS)])
                for i in range(total)]
        for f in futs:
            success, rate = f.result()
            if success:
                ok += 1
            elif rate:
                over += 1
            else:
                err += 1
    elapsed = time.time() - t0
    rps = ok / elapsed if elapsed else 0
    return {"workers": workers, "ok": ok, "over": over, "err": err,
            "elapsed": elapsed, "rps": rps}


def main() -> int:
    print(f"[kis_probe v2] ENV={ENV}")
    token = get_token()
    if not token:
        return 1

    print("\n── 동시성별 처리량·유량초과 실측 (각 40건) ──")
    print("workers | 성공 | 유량초과 | 기타 | 경과   | 유효 req/s")
    results = []
    for w in (1, 2, 3, 5, 8, 12):
        r = measure(token, w)
        results.append(r)
        print(f"  {w:>4}  |  {r['ok']:>2}  |   {r['over']:>2}    | {r['err']:>2}  "
              f"| {r['elapsed']:>5.1f}s | {r['rps']:>5.1f}")
        time.sleep(2)               # 레벨 간 유량 리셋 여유

    # 유량초과 0(또는 극소)인 가장 높은 처리량 선택
    safe = [r for r in results if r["over"] == 0 and r["err"] == 0]
    best = max(safe or results, key=lambda r: r["rps"])
    print(f"\n권장 동시성: {best['workers']} 스레드 "
          f"(유효 {best['rps']:.1f} req/s, 유량초과 {best['over']})")
    for target in (2000, 2700):
        est = target / best["rps"] if best["rps"] else float("inf")
        print(f"   {target}종목 추정: {est:.0f}s (~{est/60:.1f}분)")
    if best["rps"] >= 2000 / (28 * 60):     # 28분 내 2000종목 = ~1.2 req/s
        print("   ✅ 30분 배치 창 내 수집 가능")
    else:
        print("   ⚠️ 처리량 부족 — 대상 축소·재시도 설계 필요")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
