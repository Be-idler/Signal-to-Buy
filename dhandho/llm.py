"""LLM 정성 분석 (명세서 §10) — finalists 한정.

파이프라인 (2-트리거 구조에 맞춘 설계):
1. 트리거 A: extract_passages — Haiku로 원문(공시·임원약력·IR)에서 관련 구절만
   추출(대량·저렴, 동기 호출 — finalists 소수라 비용 미미)
2. 트리거 A: submit_batch — Sonnet 채점 요청을 비동기 Batch API로 제출(−50%).
   고정 루브릭은 system 블록 + prompt caching(최대 90% 절감).
3. 트리거 B: retrieve_batch — 배치 결과 수신 → {ticker: qual} 반환.

규율(§10): 소스 계층 태깅(1차 DART > 2차 IR > 3차 미디어), 인터뷰는 '주장',
근거 없으면 2.5 상한, 원문 복제 금지(사실·요지만), max_tokens 제한.
"""
from __future__ import annotations

import json

import config

_SCORING_RUBRIC = """당신은 한국 상장사 정성 분석가다. 아래 자료만 근거로 각 항목을 1.0~5.0으로 채점하라.

채점 항목(v1 단도투자 그라운딩 항목 — B4·D2·D3·F1·F3):
- B4 해자: 경쟁우위(브랜드·전환비용·원가우위)가 근거로 입증(5) / 보통(3) / 약함(1)
- D2 급락 원인: 최근 급락/실적 부진이 일회성(5) / 혼재(3) / 영구적 훼손(1)
- D3 산업 사양화: 성장/성숙안정(4~5) / 성숙후기(3) / 사양산업(1~2)
- F1 자본배분: 재투자·인수·환원이 합리적(5) / 보통(3) / 가치파괴(증자 남발·고가 M&A)(1)
- F3 IR 투명성: 정직·일관된 공시와 소통(5) / 보통(3) / 불투명·정정 반복·터널링 정황(1)

규칙:
1. 근거가 있는 항목만 채점하고 grounded=true. 근거 부족이면 score=null, grounded=false(추측 금지).
2. 출처 계층: tier 1=DART 공시, 2=IR 자료, 3=미디어. DART·IR 우선, 미디어는 보조.
3. 인터뷰·기사 내용은 사실이 아니라 '경영진의 주장'으로 취급하라.
4. 원문을 복제하지 말고 요지만. reason은 60자 이내.
5. 터널링·횡령·소수주주 침해가 확인되면 F3에 "tunneling_confirmed": true.
6. 마지막에 drop_reason(하락 사유 한 문장), selection_reason(종목 선정/탈락 관점 한 문장)을 넣어라.

출력은 아래 JSON만 (설명 문장 금지):
{"B4":{"score":n|null,"grounded":true|false,"reason":"≤60자",
       "basis":[{"tier":1,"source":"문서명/rcept_no","date":"YYYYMMDD"}]},
 "D2":{...},"D3":{...},"F1":{...},"F3":{...},
 "drop_reason":"...","selection_reason":"..."}"""

_EXTRACT_PROMPT = """다음 자료에서 아래 주제와 관련된 구절만 추출·요약하라(원문 복제 금지, 요지만):
주제: ①최근 급락/실적 부진의 원인 ②산업 전망·수명주기 ③경쟁우위·해자
④자본배분(재투자·인수·증자·환원) ⑤IR 투명성·공시 품질·지배구조·관계자거래.
각 항목에 출처(문서명, 일자, tier: 1=DART 2=IR 3=미디어)를 붙여라. 관련 내용이 없으면 "없음"."""


def _client():
    import anthropic
    return anthropic.Anthropic()


def _doc_text(docs: dict) -> str:
    """finalist 1종목의 수집 자료를 프롬프트 텍스트로 직렬화.

    B4(해자)·D3(산업)·F1(자본배분)의 근거는 정기보고서 **본문**에서 나온다 —
    제목 목록만으로는 전 항목 '근거 부재'가 되므로 periodic(사업의 내용·MD&A
    발췌)과 수시공시 본문(disclosure_texts)을 반드시 포함한다.
    """
    parts = []
    if docs.get("periodic"):
        p = docs["periodic"]
        parts.append(f"[정기보고서 원문 발췌 — {p.get('report_nm')} "
                     f"{p.get('rcept_dt')} (tier 1, rcept_no={p.get('rcept_no')})]")
        parts.append((p.get("text") or "")[:16000])
    if docs.get("disclosures"):
        parts.append("[DART 공시 목록 (tier 1)]")
        for d in docs["disclosures"][:30]:
            parts.append(f"- {d.get('rcept_dt')} {d.get('report_nm')} (rcept_no={d.get('rcept_no')})")
    for dt_ in docs.get("disclosure_texts", [])[:3]:
        parts.append(f"[수시공시 본문 — {dt_.get('report_nm')} {dt_.get('rcept_dt')} "
                     f"(tier 1, rcept_no={dt_.get('rcept_no')})]\n"
                     f"{(dt_.get('text') or '')[:1500]}")
    if docs.get("news"):
        parts.append("[뉴스 헤드라인 (tier 3 — 미디어, 보조 근거·주장 취급)]")
        for n in docs["news"][:8]:
            parts.append(f"- {n.get('date') or ''} {n.get('source') or ''}: "
                         f"{n.get('title')}")
    if docs.get("market_note"):
        parts.append(f"[시장 요인 분해 (정량 참고)]\n{docs['market_note']}")
    if docs.get("trend_note"):
        parts.append(f"[검색량 추세 (tier 3 — 보조, 구글 트렌드)]\n{docs['trend_note']}")
    if docs.get("trade_note"):
        parts.append(f"[수출 통계 (tier 1 — 관세청, 품목 단위 참고)]\n{docs['trade_note']}")
    if docs.get("trade_region_note"):
        parts.append(f"[수출 통계·시군구 프록시 (tier 1 — 관세청, 신고지 기준 참고)]\n"
                     f"{docs['trade_region_note']}")
    if docs.get("executives"):
        parts.append("[임원 현황 — DART 사업보고서 (tier 1)]")
        for e in docs["executives"][:20]:
            parts.append(f"- {e.get('name')} / {e.get('position')} / 약력: {e.get('career')} / 재직: {e.get('tenure')}")
    for label, tier in (("ir_texts", 2), ("media_texts", 3)):
        for t in docs.get(label, [])[:5]:
            parts.append(f"[{label} (tier {tier})]\n{t[:4000]}")
    return "\n".join(parts) if parts else "(수집 자료 없음)"


def extract_passages(finalists_docs: dict[str, dict],
                     model: str | None = None) -> dict[str, str]:
    """Haiku로 종목별 관련 구절 추출 (동기). 반환: {ticker: 추출 텍스트}."""
    client = _client()
    model = model or config.LLM_EXTRACT_MODEL
    out: dict[str, str] = {}
    for ticker, docs in finalists_docs.items():
        resp = client.messages.create(
            model=model,
            max_tokens=config.LLM_EXTRACT_MAX_TOKENS,
            system=[{"type": "text", "text": _EXTRACT_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _doc_text(docs)}],
        )
        out[ticker] = "".join(b.text for b in resp.content if b.type == "text")
    return out


def submit_batch(extracted: dict[str, str], model: str | None = None) -> str:
    """Sonnet 채점 요청을 Batch API로 제출. 반환: batch_id (체크포인트 저장용)."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = _client()
    model = model or config.LLM_SCORE_MODEL
    requests = [
        Request(
            custom_id=ticker,
            params=MessageCreateParamsNonStreaming(
                model=model,
                max_tokens=config.LLM_SCORE_MAX_TOKENS,
                system=[{"type": "text", "text": _SCORING_RUBRIC,
                         "cache_control": {"type": "ephemeral"}}],   # 고정 루브릭 캐싱
                messages=[{"role": "user", "content": f"종목 {ticker} 자료:\n{text}"}],
            ),
        )
        for ticker, text in extracted.items()
    ]
    batch = client.messages.batches.create(requests=requests)
    return batch.id


def retrieve_batch(batch_id: str) -> tuple[str, dict[str, dict]]:
    """배치 상태 확인 후 결과 파싱.

    반환: (status, {ticker: qual}) — status가 "ended"가 아니면 qual은 빈 dict
    (트리거 B에서 대기·재시도).
    """
    client = _client()
    batch = client.messages.batches.retrieve(batch_id)
    if batch.processing_status != "ended":
        return batch.processing_status, {}

    qual_by_ticker: dict[str, dict] = {}
    for result in client.messages.batches.results(batch_id):
        ticker = result.custom_id
        if result.result.type != "succeeded":
            qual_by_ticker[ticker] = {"_error": result.result.type}
            continue
        msg = result.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        qual_by_ticker[ticker] = _parse_qual(text)
    return "ended", qual_by_ticker


def score_single(ticker: str, text: str, model: str | None = None) -> dict:
    """단일 종목 동기 채점 (질의응답 경로) — 배치와 동일한 루브릭·파서.

    질의응답은 종목 1개라 Batch 대기(수 분~수십 분)가 부적절해 동기 호출한다.
    루브릭은 system 블록 캐싱으로 배치 경로와 비용 구조 동일.
    """
    client = _client()
    model = model or config.LLM_SCORE_MODEL
    resp = client.messages.create(
        model=model,
        max_tokens=config.LLM_SCORE_MAX_TOKENS,
        system=[{"type": "text", "text": _SCORING_RUBRIC,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"종목 {ticker} 자료:\n{text}"}],
    )
    out = "".join(b.text for b in resp.content if b.type == "text")
    return _parse_qual(out)


def extract_hs(business_text: str, model: str | None = None) -> dict | None:
    """사업의 내용 발췌 → 주력 수출품 HS 6단위 + 주요 생산공장 소재지 추정 (Haiku).

    관세청 품목/시군구 통계 조회용 근사치 — 점수에 직접 반영하지 않는다.
    반환: {"hs"(4단위), "hs6", "product", "sido", "sgg"} | None.
    """
    client = _client()
    model = model or config.LLM_EXTRACT_MODEL
    resp = client.messages.create(
        model=model, max_tokens=250,
        messages=[{"role": "user", "content":
                   "다음 한국 상장사 사업 내용에서 ①주력 '수출' 제품 하나의 HS코드 6단위 "
                   "②주요 생산공장 소재지(시도 2글자 축약: 서울/부산/대구/인천/광주/대전/울산/"
                   "세종/경기/강원/충북/충남/전북/전남/경북/경남/제주, 그리고 시군구명 예: 이천시)를 "
                   "추정하라. 내수 위주이거나 판단이 어려우면 hs는 null, 소재지 불명이면 sido/sgg는 null. "
                   'JSON만 출력: {"hs6":"854232","product":"메모리 반도체","sido":"경기","sgg":"이천시"} '
                   '또는 {"hs6":null}\n\n'
                   + business_text[:4000]}],
    )
    out = "".join(b.text for b in resp.content if b.type == "text")
    start, end = out.find("{"), out.rfind("}")
    if start == -1:
        return None
    try:
        d = json.loads(out[start:end + 1])
    except json.JSONDecodeError:
        return None
    hs6 = d.get("hs6") or d.get("hs")
    if not hs6 or not str(hs6).strip().isdigit():
        return None
    hs6 = str(hs6).strip()[:6]
    return {"hs": hs6[:4], "hs6": hs6, "product": d.get("product"),
            "sido": d.get("sido"), "sgg": d.get("sgg")}


def _parse_qual(text: str) -> dict:
    """모델 출력에서 JSON 추출. 파싱 실패 시 빈 dict(→전 항목 2.5 캡)."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {"_error": "no_json"}
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"_error": "json_decode"}
    out: dict = {}
    for key, item in raw.items():
        if key in ("drop_reason", "selection_reason"):
            out[key] = item                  # v1 폴백 다이제스트용 문장
            continue
        if not isinstance(item, dict):
            continue
        entry = {"score": item.get("score"), "basis": item.get("basis") or [],
                 "reason": item.get("reason")}
        if item.get("grounded") is False:
            entry["basis"] = []              # grounded=false → 2.5 상한+플래그 (v1 §7)
        if item.get("tunneling_confirmed"):
            entry["tunneling_confirmed"] = True
        out[key] = entry
    return out
