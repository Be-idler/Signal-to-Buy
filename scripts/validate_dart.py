"""1단계 검증 하네스 — 실제 OpenDART 키로 실행하는 수동 스크립트.

이 세션 환경에는 DART_API_KEY가 없으므로, 운영자가 키를 넣고 직접 실행해
중간값을 눈으로 확인한다(지시문 1단계).

사용: DART_API_KEY=... python scripts/validate_dart.py [ticker ...]
기본 종목: 005930(삼성전자), 000660(SK하이닉스)

확인 포인트:
- operating_income이 dart_OperatingIncomeLoss / ifrs-full_ProfitLossFromOperatingActivities
  중 무엇으로 오는지 → dart.ACCOUNT_MAP 보정
- 차입금 키워드 합산(total_borrowings)이 재무제표와 일치하는지
- 수시공시·임원현황 파싱
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dhandho import dart, metrics  # noqa: E402

DEFAULT_TICKERS = ["005930", "000660"]


def main() -> int:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    year = dt.date.today().year - 1

    print("corp_code 매핑 로드 중...")
    codes = dart.get_corp_codes()
    print(f"  상장사 {len(codes)}개")

    for t in tickers:
        corp = codes.get(t)
        print(f"\n===== {t} (corp_code={corp}) =====")
        if corp is None:
            print("  corp_code 없음 — 스킵")
            continue

        rows = dart.get_financials(corp, year, dart.REPRT_ANNUAL)
        print(f"  재무 행 {len(rows)}개")
        # operating_income 태그 실측
        op_tags = sorted({r.get("account_id") for r in rows
                          if "영업이익" in (r.get("account_nm") or "")})
        print(f"  '영업이익' account_id 실측: {op_tags}")
        borrow_rows = [(r.get("account_nm"), r.get("thstrm_amount"))
                       for r in rows if r.get("sj_div") == "BS"
                       and any(k in (r.get("account_nm") or "")
                               for k in ("차입금", "사채", "리스부채", "유동성장기부채"))]
        print(f"  차입금 후보 계정 {len(borrow_rows)}개:")
        for nm, amt in borrow_rows[:10]:
            print(f"    - {nm}: {amt}")

        fin = dart.normalize_financials(rows)
        print("  정규화 결과:")
        print(json.dumps({k: v for k, v in fin.items() if k != "flags"},
                         ensure_ascii=False, indent=2, default=str))
        print(f"  flags: {fin['flags']}")

        m = metrics.compute_derived(fin, mktcap=None)
        print("  파생지표(재무 단독분):")
        for k in ("op_margin", "roe", "roic", "fcf", "net_cash", "ncav",
                  "debt_ratio", "interest_coverage", "roc_greenblatt"):
            print(f"    {k} = {m.get(k)}")

        end = dt.date.today().strftime("%Y%m%d")
        bgn = (dt.date.today() - dt.timedelta(days=60)).strftime("%Y%m%d")
        disc = dart.get_recent_disclosures(corp, bgn, end)
        print(f"  최근 60일 수시공시(필터 후) {len(disc)}건:")
        for d in disc[:5]:
            print(f"    - {d['rcept_dt']} {d['report_nm']}")

        execs = dart.get_executive_profiles(corp, year)
        print(f"  임원 {len(execs)}명 (앞 3명):")
        for e in execs[:3]:
            print(f"    - {e['name']} / {e['position']} / {str(e['career'])[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
