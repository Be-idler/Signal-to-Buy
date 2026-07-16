"""종목 뉴스 검색 (Google News RSS — API 키 불필요) — 급등/급락 사유 그라운딩 보조.

LLM 루브릭 규율(§10)상 뉴스는 tier 3(미디어)로 '주장' 취급 — D2(급락 원인) 등의
보조 근거로만 쓰이고, DART 공시(tier 1)가 항상 우선한다.
"""
from __future__ import annotations

import html
import re
import urllib.parse

import requests

_RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; SignalToBuy/1.0)"}


def _parse_rss(raw: str, max_items: int = 8) -> list[dict]:
    """RSS XML → [{title, date, source}] (외부 파서 의존 없이 최소 파싱)."""
    items: list[dict] = []
    for m in re.finditer(r"<item>(.*?)</item>", raw, re.S):
        blk = m.group(1)

        def _tag(t: str) -> str | None:
            mm = re.search(rf"<{t}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>",
                           blk, re.S)
            return html.unescape(mm.group(1).strip()) if mm else None

        title = _tag("title")
        if not title:
            continue
        items.append({"title": title, "date": _tag("pubDate"),
                      "source": _tag("source")})
        if len(items) >= max_items:
            break
    return items


def search_news(query: str, max_items: int = 8, timeout: int = 15) -> list[dict]:
    """구글 뉴스 검색 (한국어) — 최신 헤드라인 목록. 실패 시 예외(호출부 best-effort)."""
    url = _RSS.format(q=urllib.parse.quote(query))
    r = requests.get(url, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return _parse_rss(r.text, max_items=max_items)
