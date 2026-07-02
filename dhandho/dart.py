"""OpenDART 수집·정규화 — SSOT 원천 (명세서 §3).

- get_corp_codes:          전 상장사 corp_code 매핑
- get_financials:          단일회사 전체 재무제표 (fnlttSinglAcntAll)
- normalize_financials:    XBRL 계정 → 정규화 키 (v1 검증 로직 + 실데이터 보정 대상)
- get_recent_disclosures:  정정·수시공시(자사주·내부자·공급계약) 스캔
- get_executive_profiles:  임원 현황(약력·재직·주식소유)

주의: DART 일일 호출 한도 → 전수 배치는 재시도·체크포인트(run_quarterly 참조).
"""
from __future__ import annotations

import io
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

import config

BASE = "https://opendart.fss.or.kr/api"

# 보고서 코드 (12월 결산 기준)
REPRT_ANNUAL = "11011"   # 사업보고서
REPRT_HALF = "11012"     # 반기보고서
REPRT_Q1 = "11013"       # 1분기보고서
REPRT_Q3 = "11014"       # 3분기보고서


def _get(path: str, retries: int = 3, **params) -> dict:
    params["crtfc_key"] = config.DART_API_KEY
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            status = data.get("status")
            if status == "000":
                return data
            if status == "013":          # 조회 데이터 없음 — 정상 빈 결과
                return {"status": "013", "list": []}
            if status == "020":          # 사용 한도 초과 — 재시도 무의미
                raise RuntimeError("DART API daily limit exceeded (status 020)")
            raise RuntimeError(f"DART API error {status}: {data.get('message')}")
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"DART request failed after {retries} retries: {last_err}")


def get_corp_codes() -> dict[str, str]:
    """{stock_code(6자리): corp_code(8자리)} — 상장사만 반환."""
    r = requests.get(f"{BASE}/corpCode.xml",
                     params={"crtfc_key": config.DART_API_KEY}, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)
    out: dict[str, str] = {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if stock and corp:               # stock_code 있는 것만 = 상장사
            out[stock] = corp
    return out


def get_financials(corp_code: str, year: int, reprt_code: str = REPRT_ANNUAL,
                   fs_div: str = "CFS") -> list[dict]:
    """단일회사 전체 재무제표 원시 행 목록. 연결(CFS) 없으면 별도(OFS) 폴백."""
    data = _get("fnlttSinglAcntAll.json", corp_code=corp_code,
                bsns_year=str(year), reprt_code=reprt_code, fs_div=fs_div)
    rows = data.get("list", [])
    if not rows and fs_div == "CFS":
        data = _get("fnlttSinglAcntAll.json", corp_code=corp_code,
                    bsns_year=str(year), reprt_code=reprt_code, fs_div="OFS")
        rows = data.get("list", [])
        for row in rows:
            row["_fs_div_used"] = "OFS"
    return rows


# ------------------------------------------------------------------ 정규화
# account_id(XBRL 태그) 우선 매핑. operating_income은 표준계정
# dart_OperatingIncomeLoss 와 ifrs-full_ProfitLossFromOperatingActivities
# 둘 다 실데이터에서 관측되므로 둘 다 매핑한다(1단계 실키 검증으로 보정).
ACCOUNT_MAP: dict[str, str] = {
    "ifrs-full_Revenue": "revenue",
    "ifrs_Revenue": "revenue",
    "dart_OperatingIncomeLoss": "operating_income",
    "ifrs-full_ProfitLossFromOperatingActivities": "operating_income",
    "ifrs_ProfitLossFromOperatingActivities": "operating_income",
    "ifrs-full_GrossProfit": "gross_profit",
    "ifrs-full_ProfitLoss": "net_income",
    "ifrs_ProfitLoss": "net_income",
    "ifrs-full_ProfitLossAttributableToOwnersOfParent": "net_income_controlling",
    "ifrs_ProfitLossAttributableToOwnersOfParent": "net_income_controlling",
    "ifrs-full_Assets": "total_assets",
    "ifrs-full_Liabilities": "total_liabilities",
    "ifrs-full_Equity": "total_equity",
    "ifrs-full_EquityAttributableToOwnersOfParent": "equity_controlling",
    "ifrs-full_CurrentAssets": "current_assets",
    "ifrs-full_CurrentLiabilities": "current_liabilities",
    "ifrs-full_CashAndCashEquivalents": "cash_and_equivalents",
    "ifrs-full_PropertyPlantAndEquipment": "ppe",
    "ifrs-full_CashFlowsFromUsedInOperatingActivities": "cfo",
    "ifrs_CashFlowsFromUsedInOperatingActivities": "cfo",
    "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "capex",
    "ifrs-full_InterestExpense": "interest_expense",
}

# account_nm 키워드 폴백 (account_id가 표준계정("-표준계정코드 미사용-")이 아닐 때)
_NAME_FALLBACK: list[tuple[str, str]] = [
    ("매출액", "revenue"), ("영업수익", "revenue"),
    ("영업이익", "operating_income"),
    ("매출총이익", "gross_profit"),
    ("당기순이익", "net_income"),
    ("지배기업 소유주지분", "equity_controlling"),
    ("지배기업의 소유주에게 귀속되는 당기순이익", "net_income_controlling"),
    ("자산총계", "total_assets"), ("부채총계", "total_liabilities"),
    ("자본총계", "total_equity"),
    ("유동자산", "current_assets"), ("유동부채", "current_liabilities"),
    ("현금및현금성자산", "cash_and_equivalents"),
    ("유형자산", "ppe"),
    ("영업활동현금흐름", "cfo"), ("영업활동으로인한현금흐름", "cfo"),
    ("유형자산의 취득", "capex"), ("유형자산의취득", "capex"),
    ("이자비용", "interest_expense"),
    ("감가상각비", "depreciation"),
]

# 차입금: BS 계정명 키워드 합산 (v1 로직 — 실데이터 검증 대상)
_BORROWING_KEYWORDS = ("차입금", "사채", "유동성장기부채", "리스부채")
# 단기금융상품: 현금성에 가산 (순현금 계산용)
_ST_INVEST_KEYWORDS = ("단기금융상품", "단기투자자산")


# 손익·현금흐름(기간) 항목 — 보고서 유형에 따라 기준이 다르다:
#   사업보고서(11011)=연간, 분기/반기=해당 기간(thstrm) + 누적(thstrm_add).
# BS(시점 잔액) 항목은 모든 보고서에서 그대로 비교 가능.
FLOW_KEYS = ("revenue", "operating_income", "gross_profit", "net_income",
             "net_income_controlling", "cfo", "capex", "interest_expense",
             "depreciation")


def _parse_amount(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_financials(rows: list[dict]) -> dict:
    """원시 재무 행 → 정규화 dict (SSOT 한 벌).

    반환 키: revenue, operating_income, gross_profit, net_income,
    net_income_controlling, total_assets, total_liabilities, total_equity,
    equity_controlling, current_assets, current_liabilities,
    cash_and_equivalents, short_term_investments, total_borrowings, ppe,
    cfo, capex, interest_expense, depreciation, fs_div, flags
    """
    out: dict = {"flags": []}
    borrowings = 0.0
    borrowings_found = False
    st_invest = 0.0
    st_invest_found = False

    for row in rows:
        amount = _parse_amount(row.get("thstrm_amount"))
        if amount is None:
            continue
        acc_id = (row.get("account_id") or "").strip()
        acc_nm = (row.get("account_nm") or "").strip()
        sj_div = (row.get("sj_div") or "").strip()

        key = ACCOUNT_MAP.get(acc_id)
        if key is None:
            for kw, mapped in _NAME_FALLBACK:
                if acc_nm == kw or acc_nm.replace(" ", "") == kw.replace(" ", ""):
                    key = mapped
                    break
        # 첫 매칭 우선 (연결 CIS/IS 중복 계정 방지)
        if key and key not in out:
            out[key] = amount
            # 분기/반기 손익·현금흐름의 누적치(thstrm_add_amount) — TTM 계산용
            if key in FLOW_KEYS:
                cum = _parse_amount(row.get("thstrm_add_amount"))
                if cum is not None:
                    out[key + "_cum"] = cum

        if sj_div == "BS":
            if any(kw in acc_nm for kw in _BORROWING_KEYWORDS):
                borrowings += amount
                borrowings_found = True
            if any(kw in acc_nm for kw in _ST_INVEST_KEYWORDS) and "유동" not in acc_nm[:1]:
                st_invest += amount
                st_invest_found = True

        if row.get("_fs_div_used") == "OFS":
            out["fs_div"] = "OFS"

    out.setdefault("fs_div", "CFS")
    out["total_borrowings"] = borrowings if borrowings_found else None
    out["short_term_investments"] = st_invest if st_invest_found else 0.0

    if out["total_borrowings"] is None:
        # 차입금 계정을 하나도 못 찾음: 무차입일 수도, 파싱 실패일 수도 → 플래그
        out["flags"].append("borrowings_not_found")
    if "operating_income" not in out:
        out["flags"].append("operating_income_missing")
    return out


# ------------------------------------------------------------------ 수시공시·임원
_DISCLOSURE_KEYWORDS = ("정정", "자기주식", "자사주", "주식등의대량보유",
                        "임원ㆍ주요주주", "임원·주요주주", "단일판매ㆍ공급계약",
                        "단일판매·공급계약", "공급계약")


def get_recent_disclosures(corp_code: str, bgn_de: str, end_de: str) -> list[dict]:
    """기간 내 공시 중 정정·자사주·내부자·공급계약 관련만 필터해 반환.

    bgn_de/end_de: YYYYMMDD. 반환 행: rcept_no, report_nm, rcept_dt, flr_nm.
    """
    data = _get("list.json", corp_code=corp_code, bgn_de=bgn_de, end_de=end_de,
                page_count="100")
    out = []
    for row in data.get("list", []):
        name = row.get("report_nm", "")
        if any(kw in name for kw in _DISCLOSURE_KEYWORDS):
            out.append({
                "rcept_no": row.get("rcept_no"),
                "report_nm": name,
                "rcept_dt": row.get("rcept_dt"),
                "flr_nm": row.get("flr_nm"),
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row.get('rcept_no')}",
            })
    return out


def get_dividend_info(corp_code: str, year: int,
                      reprt_code: str = REPRT_ANNUAL) -> list[dict]:
    """배당에 관한 사항 (alotMatter) — E1 주주환원 결정론 산출용 (v1 §4)."""
    data = _get("alotMatter.json", corp_code=corp_code,
                bsns_year=str(year), reprt_code=reprt_code)
    return [{"item": r.get("se"), "current": r.get("thstrm"),
             "prior": r.get("frmtrm")} for r in data.get("list", [])]


def get_treasury_stock(corp_code: str, year: int,
                       reprt_code: str = REPRT_ANNUAL) -> list[dict]:
    """자기주식 취득·처분 현황 (tesstkAcqsDspsSttus) — E1 미소각 자사주 판정용."""
    data = _get("tesstkAcqsDspsSttus.json", corp_code=corp_code,
                bsns_year=str(year), reprt_code=reprt_code)
    return [{"method": r.get("acqs_mth2"), "kind": r.get("stock_knd"),
             "begin": r.get("bsis_qy"), "acquired": r.get("change_qy_acqs"),
             "disposed": r.get("change_qy_dsps"),
             "retired": r.get("change_qy_incnr"),      # 소각
             "end": r.get("trmend_qy")} for r in data.get("list", [])]


def get_insider_transactions(corp_code: str) -> list[dict]:
    """임원·주요주주 소유보고 (elestock) — F2 내부자 정렬(순증감) 결정론 산출용."""
    data = _get("elestock.json", corp_code=corp_code)
    out = []
    for r in data.get("list", []):
        out.append({"name": r.get("repror"), "date": r.get("rcept_dt"),
                    "relation": r.get("isu_exctv_ofcps"),
                    "shares": _parse_amount(r.get("sp_stock_lmp_cnt")),
                    "change": _parse_amount(r.get("sp_stock_lmp_irds_cnt"))})
    return out


def get_executive_profiles(corp_code: str, year: int,
                           reprt_code: str = REPRT_ANNUAL) -> list[dict]:
    """사업보고서 '임원 현황' — 이름·직위·약력·재직기간."""
    data = _get("exctvSttus.json", corp_code=corp_code,
                bsns_year=str(year), reprt_code=reprt_code)
    out = []
    for row in data.get("list", []):
        out.append({
            "name": row.get("nm"),
            "position": row.get("ofcps"),
            "career": row.get("main_career"),
            "tenure": row.get("hffc_pd"),
            "registered": row.get("rgist_exctv_at"),
            "fulltime": row.get("fte_at"),
        })
    return out
