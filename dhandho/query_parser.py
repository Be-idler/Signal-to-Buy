"""질의 명령 파싱 (애드온2 §1): `<종목명|6자리코드> <스킴> [기준일]`.

예: "삼성전자 단도 2026-06-30" / "005930 버핏 20260630" / "비나텍 린치"
- 동명·복수 매칭 시 AmbiguousError로 후보를 되물어봄(추측 진행 금지).
- 미래 기준일 거부. 생략 시 date=None(호출부가 최근 거래일로 해석).
"""
from __future__ import annotations

import datetime as dt
import re


class ParseError(Exception):
    pass


class AmbiguousError(ParseError):
    def __init__(self, token: str, candidates: list[tuple[str, str]]):
        self.candidates = candidates
        lines = ", ".join(f"{n}({t})" for t, n in candidates[:8])
        super().__init__(f"'{token}'에 해당하는 종목이 여러 개입니다: {lines}\n"
                         f"종목코드 6자리로 다시 요청해 주세요.")


USAGE = "사용법: <종목명|6자리코드> <스킴> [YYYY-MM-DD]\n예: 삼성전자 단도 2026-06-30"

# 스킴 별칭 (대소문자 무시, 애드온2 §1)
_SCHEME_ALIASES = {
    "dhandho": ("단도", "단도투자", "파브라이", "pabrai", "dhandho"),
    "ltgg": ("베일리기포드", "베일리 기포드", "bg", "ltgg"),
    "buffett": ("버핏", "워런버핏", "워런 버핏", "버핏멍거", "buffett"),
    "outsiders": ("아웃사이더", "손다이크", "thorndike", "outsider", "outsiders"),
    "lynch": ("린치", "피터린치", "피터 린치", "peg", "lynch"),
    "ackman": ("애크먼", "빌애크먼", "빌 애크먼", "액티비스트", "ackman"),
}
SCHEME_LABEL = {"dhandho": "단도투자", "ltgg": "LTGG", "buffett": "버핏멍거",
                "outsiders": "아웃사이더", "lynch": "피터 린치", "ackman": "빌 애크먼"}


def _resolve_scheme(token: str) -> str | None:
    low = token.lower()
    for key, aliases in _SCHEME_ALIASES.items():
        if low in (a.lower() for a in aliases):
            return key
    return None


def _resolve_date(token: str, today: dt.date) -> str:
    s = token.replace("-", "")
    if not (len(s) == 8 and s.isdigit()):
        raise ParseError(f"기준일 형식 오류: '{token}' (YYYY-MM-DD 또는 YYYYMMDD)")
    try:
        d = dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        raise ParseError(f"존재하지 않는 날짜: '{token}'")
    if d > today:
        raise ParseError(f"미래 기준일은 분석할 수 없습니다: {d.isoformat()}")
    return s


def resolve_stock(token: str, universe: dict[str, str]) -> tuple[str, str]:
    """token(이름|코드) → (ticker, name). universe: {ticker: name}.

    이름은 정확일치 → 부분일치 순. 복수 매칭 시 AmbiguousError.
    """
    if re.fullmatch(r"\d{6}", token):
        if token not in universe:
            raise ParseError(f"종목코드 {token}: 유니버스에 없음 (상장폐지/오타?)")
        return token, universe[token]
    norm = token.replace(" ", "")
    exact = [(t, n) for t, n in universe.items() if n and n.replace(" ", "") == norm]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousError(token, exact)
    partial = [(t, n) for t, n in universe.items() if n and norm in n.replace(" ", "")]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise AmbiguousError(token, partial)
    raise ParseError(f"종목 '{token}'을(를) 찾지 못했습니다.")


def parse(text: str, universe: dict[str, str],
          today: dt.date | None = None) -> dict:
    """명령 → {"ticker","name","scheme","scheme_label","date"(YYYYMMDD|None)}."""
    today = today or dt.date.today()
    tokens = (text or "").split()
    if len(tokens) < 2:
        raise ParseError(USAGE)

    # 마지막 토큰이 날짜인지 판별
    date = None
    if len(tokens) >= 3 and re.fullmatch(r"[\d-]{8,10}", tokens[-1]):
        date = _resolve_date(tokens[-1], today)
        tokens = tokens[:-1]

    # 뒤에서부터 스킴 탐색(종목명에 공백 허용: "비나텍 린치" / "LG 전자 버핏")
    scheme = _resolve_scheme(tokens[-1])
    if scheme is None and len(tokens) >= 3:
        scheme = _resolve_scheme(" ".join(tokens[-2:]))
        if scheme is not None:
            tokens = tokens[:-2] + ["_"]
    if scheme is None:
        raise ParseError(f"알 수 없는 스킴: '{tokens[-1]}'\n"
                         f"가능: 단도/베일리기포드/버핏/아웃사이더/린치/애크먼\n{USAGE}")
    stock_token = " ".join(tokens[:-1]).strip()
    if not stock_token:
        raise ParseError(USAGE)

    ticker, name = resolve_stock(stock_token, universe)
    return {"ticker": ticker, "name": name, "scheme": scheme,
            "scheme_label": SCHEME_LABEL[scheme], "date": date}
