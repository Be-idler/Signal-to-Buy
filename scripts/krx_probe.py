"""KRX 일별매매정보 발행 시각 측정 프로브 (일회성 운영 도구).

트랙1의 최적 실행 시각을 정하려면 KRX OpenAPI가 당일(T) 데이터를 밤새
언제 발행하는지 알아야 한다. 이 스크립트는 실행 시점의 실제 시각과 함께
'오늘 데이터 존재 여부'·'최신 가용 거래일'을 한 줄로 로그에 남긴다.
krx_probe.yml이 밤~아침 30분 간격으로 호출하면, 로그에서 today_available이
False→True로 바뀌는(또는 latest_available이 하루 전진하는) 시점이 발행 시각이다.

발행 시각을 확정하면 이 프로브(스크립트+워크플로)는 삭제한다.
"""
from __future__ import annotations

import datetime as dt

from dhandho import krx


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    kst = now + dt.timedelta(hours=9)
    today = kst.strftime("%Y%m%d")
    try:
        today_avail = krx.is_trading_day(today)
    except Exception as e:                        # noqa: BLE001
        today_avail = f"ERR({e})"
    try:
        recent = krx.recent_trading_days(kst.date(), 1)
        latest = recent[-1] if recent else None
    except Exception as e:                        # noqa: BLE001
        latest = f"ERR({e})"
    print(f"PROBE utc={now:%Y-%m-%d %H:%M} kst={kst:%Y-%m-%d %H:%M} "
          f"today={today} today_available={today_avail} latest_available={latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
