"""관세청 수출입실적 (공공데이터포털 OpenAPI) — 성장 추세(수출 YoY) 보조 지표.

- 품목별 국가별 (nitemtrade, 검증 완료): CUSTOMS_COUNTRY_API_KEY
- 시군구별 품목별 (sigunguperprlstperacrs, 검증 완료): CUSTOMS_REGION_API_KEY
  파라미터: strtYymm·endYymm·HsSgn(6단위)·sidoCd(행안부 2자리)
  응답: 월별×시군구별 (sggNm, expUsdAmt, korePrlstNm, priodTitle)

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


# ────────────────────────── 시군구별 품목별 (공장 소재지 프록시)

_REGION_URL = ("https://apis.data.go.kr/1220000/sigunguperprlstperacrs"
               "/getSigunguPerPrlstPerAcrs")

# 행안부 시도 코드 (probe로 41=경기·11=서울 실측 확인)
SIDO_CD = {"서울": "11", "부산": "26", "대구": "27", "인천": "28", "광주": "29",
           "대전": "30", "울산": "31", "세종": "36", "경기": "41", "강원": "42",
           "충북": "43", "충남": "44", "전북": "45", "전남": "46", "경북": "47",
           "경남": "48", "제주": "50"}


def _parse_region_items(xml: str) -> list[dict]:
    """시군구별 응답 → [{sggNm, priodTitle, expUsdAmt, impUsdAmt, korePrlstNm}]."""
    if "<resultCode>00</resultCode>" not in xml:
        mm = re.search(r"<resultMsg>(.*?)</resultMsg>", xml, re.S)
        raise RuntimeError(f"customs region API error: "
                           f"{mm.group(1).strip() if mm else xml[:120]}")
    out: list[dict] = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        blk = m.group(1)

        def _tag(t: str) -> str | None:
            mm = re.search(rf"<{t}>(.*?)</{t}>", blk, re.S)
            return mm.group(1).strip() if mm else None

        out.append({"sggNm": _tag("sggNm"), "priodTitle": _tag("priodTitle"),
                    "korePrlstNm": _tag("korePrlstNm"),
                    "expUsdAmt": _num(_tag("expUsdAmt")),
                    "impUsdAmt": _num(_tag("impUsdAmt"))})
    return out


def _region_window_exp(hs6: str, sido_cd: str, sgg_keyword: str,
                       strt: str, end: str) -> tuple[float | None, str | None]:
    """기간 내 시군구(키워드 매칭) 수출액 합계 → (금액, 품목명)."""
    r = requests.get(_REGION_URL,
                     params={"serviceKey": config.CUSTOMS_REGION_API_KEY,
                             "strtYymm": strt, "endYymm": end,
                             "HsSgn": hs6, "sidoCd": sido_cd}, timeout=60)
    r.raise_for_status()
    total, name = 0.0, None
    matched = False
    for it in _parse_region_items(r.text):
        if sgg_keyword in (it.get("sggNm") or ""):
            matched = True
            total += it.get("expUsdAmt") or 0.0
            name = name or it.get("korePrlstNm")
    return (total if matched else None), name


def region_export_yoy(hs6: str, sido: str, sgg: str) -> dict | None:
    """공장 소재지(시도·시군구) × 품목(HS 6단위) 수출 YoY — 시군구 프록시.

    시군구×품목 조합은 전국 품목 통계보다 기업 특이성이 높다(기업도시형에서
    사실상 그 기업의 수출 프록시). 단, 통계는 **신고지(사업장 주소) 기준**이라
    공장≠신고지인 기업에선 어긋난다 — 점수 미반영, '참고' 표기 전용.
    """
    if not config.CUSTOMS_REGION_API_KEY:
        return None
    sido_cd = SIDO_CD.get(sido[:2])
    if not sido_cd or not sgg:
        return None
    today = dt.date.today()
    end = _shift_month(today.strftime("%Y%m"), -2)
    strt = _shift_month(end, -2)
    recent, name = _region_window_exp(hs6, sido_cd, sgg, strt, end)
    prior, _ = _region_window_exp(hs6, sido_cd, sgg,
                                  _shift_month(strt, -12), _shift_month(end, -12))
    if not recent or not prior:
        return None
    yoy = recent / prior - 1.0
    label = f"{sgg} HS {hs6}" + (f"({name})" if name else "")
    note = (f"{label} 수출액(최근 3개월 {strt}~{end}, 시군구 프록시): "
            f"전년 동기 대비 {yoy:+.0%} — "
            f"{'수출 증가' if yoy > 0.05 else '수출 감소' if yoy < -0.05 else '보합'} "
            f"(신고지 기준 통계 — 동일 지역 동종 기업과 혼재 가능, 참고)")
    return {"hs6": hs6, "sido": sido, "sgg": sgg, "window": f"{strt}~{end}",
            "recent_usd": recent, "prior_usd": prior, "yoy": round(yoy, 4),
            "note": note}
