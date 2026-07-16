"""관세청 수출입실적 (공공데이터포털 OpenAPI) — 성장 추세(수출 YoY) 보조 지표.

- 품목별 국가별 (nitemtrade, 검증 완료): CUSTOMS_COUNTRY_API_KEY
- 시군구별 품목별: CUSTOMS_REGION_API_KEY — 엔드포인트 확정 시
  CUSTOMS_REGION_ENDPOINT(전체 URL)로 주입해 활성화한다.

규율: 기업↔HS코드 매핑은 근사치(주력 품목 추정)라 점수에 직접 반영하지 않고
LLM 판단 근거·리포트 '참고' 표기로만 쓴다. 월 통계는 1~2개월 시차가 있다.
"""
from __future__ import annotations

import datetime as dt
import re

import requests

import config

_BASE = "https://apis.data.go.kr/1220000"
_NUM_RE = re.compile(r"-?[\d,]+")


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    m = _NUM_RE.search(v)
    return float(m.group(0).replace(",", "")) if m else None


def _parse_items(xml: str) -> list[dict]:
    """응답 XML → [{year, statKor, expDlr, impDlr, balPayments}] (최소 파싱)."""
    if "<resultCode>00</resultCode>" not in xml:
        mm = re.search(r"<resultMsg>(.*?)</resultMsg>", xml, re.S)
        raise RuntimeError(f"customs API error: {mm.group(1).strip() if mm else xml[:120]}")
    items: list[dict] = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        blk = m.group(1)

        def _tag(t: str) -> str | None:
            mm = re.search(rf"<{t}>(.*?)</{t}>", blk, re.S)
            return mm.group(1).strip() if mm else None

        items.append({"year": _tag("year"), "statKor": _tag("statKor"),
                      "expDlr": _num(_tag("expDlr")), "impDlr": _num(_tag("impDlr")),
                      "balPayments": _num(_tag("balPayments"))})
    return items


def _window_total_exp(hs: str, strt: str, end: str) -> float | None:
    """기간(YYYYMM~YYYYMM) 품목 수출액 합계 — '총계' 행 사용."""
    r = requests.get(f"{_BASE}/nitemtrade/getNitemtradeList",
                     params={"serviceKey": config.CUSTOMS_COUNTRY_API_KEY,
                             "strtYymm": strt, "endYymm": end, "hsSgn": hs},
                     timeout=60)
    r.raise_for_status()
    for it in _parse_items(r.text):
        if it.get("year") == "총계":
            return it.get("expDlr")
    return None


def _shift_month(ym: str, delta: int) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}{total % 12 + 1:02d}"


def export_yoy(hs: str, product: str | None = None) -> dict | None:
    """품목(HS) 최근 3개월 수출액 vs 전년 동기 3개월 — YoY 증감.

    통계 발표 시차를 감안해 '전전월'까지의 3개월 창을 쓴다.
    반환: {"hs", "window", "recent_usd", "prior_usd", "yoy", "note"} | None.
    """
    if not config.CUSTOMS_COUNTRY_API_KEY:
        return None
    today = dt.date.today()
    end = _shift_month(today.strftime("%Y%m"), -2)     # 전전월
    strt = _shift_month(end, -2)                       # 3개월 창
    recent = _window_total_exp(hs, strt, end)
    prior = _window_total_exp(hs, _shift_month(strt, -12), _shift_month(end, -12))
    if not recent or not prior:
        return None
    yoy = recent / prior - 1.0
    label = f"HS {hs}" + (f"({product})" if product else "")
    note = (f"{label} 전국 수출액(최근 3개월 {strt}~{end}): 전년 동기 대비 {yoy:+.0%} "
            f"— {'수출 증가' if yoy > 0.05 else '수출 감소' if yoy < -0.05 else '보합'} "
            f"(품목 단위 통계 — 기업 실적과 1:1 아님, 참고)")
    return {"hs": hs, "window": f"{strt}~{end}", "recent_usd": recent,
            "prior_usd": prior, "yoy": round(yoy, 4), "note": note}
