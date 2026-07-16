"""구글 트렌드 검색량 추세 (비공식 pytrends — best-effort 보조 지표).

성장 추세 훼손 여부 판단 보조: 최근 3개월 검색량 평균 vs 직전 9개월 평균.
비공식 API라 차단(429)·형식 변경이 잦다 — 모든 호출부는 실패를 무시해야 하고,
점수에 직접 반영하지 않는다(LLM 판단 근거·리포트 표기용, tier 3 수준).
"""
from __future__ import annotations


def search_trend(keyword: str, timeout: int = 20) -> dict | None:
    """키워드 12개월 검색량 → {"recent_avg", "prior_avg", "change", "note"} | None.

    change = 최근 3개월 평균 / 직전 9개월 평균 − 1 (양수 = 관심 증가).
    실패(차단·데이터 없음) 시 None — 호출부는 조용히 생략한다.
    """
    from pytrends.request import TrendReq

    # retries 인자 금지 — pytrends가 urllib3 1.x 전용 method_whitelist를 넘겨
    # urllib3 2.x에서 TypeError가 난다 (재시도는 호출부 best-effort로 충분)
    tr = TrendReq(hl="ko", tz=540, timeout=(10, timeout))
    tr.build_payload([keyword], timeframe="today 12-m", geo="KR")
    df = tr.interest_over_time()
    if df is None or df.empty or keyword not in df:
        return None
    s = df[keyword].astype(float)
    if len(s) < 20:                              # 주 단위 52포인트 기대 — 결손 방어
        return None
    recent = s.iloc[-13:].mean()                 # 최근 ~3개월(13주)
    prior = s.iloc[:-13].mean()                  # 직전 ~9개월
    if prior <= 0:
        return None
    change = recent / prior - 1.0
    note = (f"'{keyword}' 구글 검색량: 최근 3개월 평균이 직전 9개월 대비 "
            f"{change:+.0%} ({'관심 증가' if change > 0.1 else '관심 감소' if change < -0.1 else '큰 변화 없음'})")
    return {"recent_avg": round(float(recent), 1),
            "prior_avg": round(float(prior), 1),
            "change": round(float(change), 4), "note": note}
