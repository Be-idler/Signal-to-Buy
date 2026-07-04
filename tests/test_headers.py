"""메시지 헤더 규격 테스트 — 봇 1개로 유형 구분 (애드온2 §2.1)."""
from dhandho.notify import (fmt_date, header_biweekly, header_daily,
                            header_system)
from dhandho.report_format import header as header_query_fn


def header_query(name, scheme, date):
    return header_query_fn(name, scheme, date)


def test_fmt_date():
    assert fmt_date("20260704") == "2026-07-04"
    assert fmt_date("2026-07-04") == "2026-07-04"


def test_daily_header():
    assert header_daily("20260704") == "📋 단도투자 RSI<30 스크리닝 2026-07-04"


def test_biweekly_header():
    assert header_biweekly("20260704") == "📋 다관점 프레임워크 랭킹 2026-07-04"


def test_system_header():
    assert header_system("배치 미완료") == "⚠️ [시스템] 배치 미완료"


def test_query_header():
    assert (header_query("삼성전자", "단도투자", "20260704")
            == "🔎 삼성전자 단도투자 방식 분석 (2026-07-04 기준)")
