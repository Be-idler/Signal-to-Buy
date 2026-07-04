"""텔레그램 인터랙티브 질의응답 수신 루프 (애드온2 §2).

봇 토큰은 봇1(TELEGRAM_BOT_TOKEN)을 재사용한다 — sendMessage(발신)와
getUpdates(수신)는 독립적이라 예약 발신과 상시 수신을 동시에 운영 가능.
자동 발신과의 구분은 헤더 규약(§2.1)으로 해결(HEADER_QUERY).

실행: python bot_listener.py  (맥미니/랩탑 상시 프로세스 — launchd KeepAlive 권장)
보안: TELEGRAM_ALLOWED_CHAT_IDS(콤마 구분) 화이트리스트 외 발신자는 전면 무시.
동시 요청은 순차 처리(단일 사용자 전제).
"""
from __future__ import annotations

import time
import traceback

import requests

import config
from dhandho import query_parser, report_format
import run_query

API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT1_TOKEN}"
_MAX = 4000


def _split(text: str, n: int = _MAX) -> list[str]:
    """4096자 제한 → 섹션(빈 줄) 단위 분할."""
    if len(text) <= n:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) + 2 > n:
            parts.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        parts.append(buf)
    return parts


def _send(chat_id, text: str) -> None:
    for chunk in _split(text):
        requests.post(f"{API}/sendMessage",
                      data={"chat_id": chat_id, "text": chunk,
                            "disable_web_page_preview": True}, timeout=15)


def _handle(msg: dict, universe: dict[str, str]) -> None:
    chat_id = msg["chat"]["id"]
    if str(chat_id) not in config.TELEGRAM_ALLOWED_CHAT_IDS:
        print(f"[무시] 미허가 chat_id={chat_id}")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    try:
        req = query_parser.parse(text, universe)
    except query_parser.ParseError as e:
        _send(chat_id, report_format.usage_error(str(e)))
        return

    date_label = req["date"] or "최근 거래일"
    _send(chat_id, report_format.ack(req["name"], req["scheme_label"], date_label))
    try:
        _send(chat_id, run_query.analyze(req))
    except Exception as e:                       # noqa: BLE001 — 오류 요지만 회신
        _send(chat_id, report_format.error(
            req["name"], req["scheme_label"], date_label,
            f"{type(e).__name__} — 로그를 확인하세요."))
        traceback.print_exc()


def main() -> None:
    if not config.TELEGRAM_ALLOWED_CHAT_IDS:
        print("TELEGRAM_ALLOWED_CHAT_IDS 미설정 — 보안상 기동 거부")
        raise SystemExit(1)
    print("bot_listener 시작 (롱폴링) — 유니버스 로딩 중…")
    universe = run_query.load_universe_names()
    universe_loaded = time.time()
    print(f"유니버스 {len(universe)}종목 로드 완료")

    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 50, "offset": offset}, timeout=60)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    _handle(upd["message"], universe)
            if time.time() - universe_loaded > 86400:   # 유니버스 일 1회 갱신
                universe = run_query.load_universe_names()
                universe_loaded = time.time()
        except requests.RequestException as e:
            print(f"[네트워크 오류] {e} — 10초 후 재시도")
            time.sleep(10)


if __name__ == "__main__":
    main()
