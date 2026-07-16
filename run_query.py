"""질의응답 분석 파이프라인 (애드온2 §3~§6) — bot_listener·워크플로 공용.

CLI: python run_query.py <종목명|코드> <스킴> [기준일]
     예) python run_query.py 삼성전자 단도 2026-06-30

기준일 시점(PIT) 규율: 기준일에 알 수 있었던 정보만 사용(pit.py).
정성(LLM) 자동 채점 없음 — 정량 산출 + '사람이 확인할 체크리스트' 출력.
"""
from __future__ import annotations

import sys
import traceback

import datetime as dt

import config
from dhandho import (asymmetry, dart, frameworks, frameworks_ackman,
                     frameworks_lynch, gate, krx, llm, market, metrics, news,
                     notify, pit, query_parser, report_format, report_labels,
                     target_price, trade, trends)
from run_trigger_a import _shareholder_summary

# 클로드 채팅 심층분석 프롬프트(저장소 prompts/) — 핸드오프 블록에 링크로 병기
_PROMPT_BASE = "https://github.com/Be-idler/Signal-to-Buy/blob/main/prompts"

# 스킴별 '정량 단계에서 미반영된 정성 항목'(=채팅이 재채점할 대상)
_PENDING_QUAL = {
    "dhandho": "B4 해자 · D2 급락원인 · D3 산업전망 · F1 자본배분 · F2 내부자 · F3 IR투명성",
    "buffett": "moat 해자유형(BB) · capital_allocation 자본배분(BC) · G1 정직성(하드게이트) "
               "· G2 능력범위",
    "ltgg": "L3 해자확장성 · L4 경영진·문화·장기지향 · L6 비대칭 옵셔널리티",
    "outsiders": "A2~A4 인수·매각 규율(OA) · C2~C4 경영진 독립성(OC) · E3~E4 디스카운트 "
                 "해소(OE) · 자사주 취득·소각 자료(OB·OE) · E1 터널링(하드게이트)",
    "lynch": "LA 스토리 명료성 · LD 시장의 무관심 · 재고 증가율 적신호(수동)",
    "ackman": "촉매의 실현 주체·시한·강제력 재평가 (공시 원문 대조)",
}


def _handoff_lines(req: dict, basis: str, scores: dict, total, grade,
                   gates: dict | None, extras: list[str],
                   flags: list[str], pending: str | None = None) -> list[str]:
    """클로드 채팅 핸드오프 블록 — 기계 판독용 정량 요약(프롬프트가 소비).

    사람이 읽는 본문과 달리 하위점수 '코드'를 그대로 노출한다 — 프롬프트의
    재합산 수식이 코드 기준이라 모호성 없이 대응돼야 한다.
    """
    scheme = req["scheme"]
    L = [f"스킴={scheme}({req['scheme_label']}) · 종목={req['name']}({req['ticker']}) "
         f"· 기준일={basis}"]
    if scores:
        line = "정량점수: " + " ".join(
            f"{k}={v:.2f}" for k, v in scores.items() if v is not None)
        if total is not None:
            line += f" → 종합 {total:.2f}"
        if grade:
            line += f" · 등급 '{grade}'"
        L.append(line)
    if gates:
        L.append("게이트: " + " · ".join(
            f"{k}={'통과' if ok else '미달'}" for k, ok in gates.items()))
    L += extras
    L.append("재채점 대상(정성): "
             + (pending if pending is not None else _PENDING_QUAL[scheme]))
    if flags:
        L.append("플래그(원시): " + ";".join(sorted(set(flags))))
    L.append(f"프롬프트: {_PROMPT_BASE}/{scheme}.md")
    return L


_CHECKLIST_COMMON = [
    "최근 주가 부진이 일시적 악재인지, 구조적 문제인지 (공시·뉴스 원문으로 확인)",
    "경영진 이력과 내부자 지분 변동 (사업보고서 임원현황·소유보고)",
    "지배구조 리스크(터널링·관계자거래)가 있는지",
]
_CHECKLIST_SCHEME = {
    "dhandho": ["청산가치(순현금·NCAV)가 소수주주에게 실제로 실현 가능한지 "
                "(지주·터널링 구조라면 할인해서 봐야 함)"],
    "buffett": ["해자의 유형(브랜드·전환비용·네트워크·원가우위)을 특정할 수 있는지",
                "10년 후에도 이 사업의 경제성을 예측할 수 있는지 (능력범위 — 정량 판정 불가)",
                "재투자 이익이 주주에게 귀속되는지, 가격 인하로 소비자에게 넘어가는지 (멍거 체크)",
                "반복매출(면도날) 비중 — 사업보고서 매출 유형 주석으로 확인"],
    "ltgg": ["시장 규모·침투율, 창업자의 장기지향 문화 (한국 종목에는 적용범위 밖일 수 있음)"],
    "outsiders": ["소각 의무화(2026.3) 이전의 자발적 소각 이력 — 규제 전 자발성이 진짜 신호",
                  "자본배분 결정권이 지배주주 일가의 별도 이해관계에 종속되지 않는지",
                  "인수 규율(지불 멀티플·영업권 손상 이력)과 저수익 사업 매각 이력"],
    "lynch": ["성장 스토리의 지속 가능성 (성장이 둔화되면 PEG가 급격히 나빠짐)",
              "재고 증가율이 매출 증가율을 앞지르는지 (린치의 대표 적신호 — 주석 확인)",
              "기관 보유·애널리스트 커버리지(낮을수록 유리)와 종목 검색량 급증 여부"],
    "ackman": ["촉매를 누가 언제 실현할 수 있는지 (소수주주는 촉매를 만들 수 없음)"],
}


def load_universe_names() -> dict[str, str]:
    """종목명 해석용 최신 유니버스 {ticker: name}."""
    basis, _ = pit.resolve_basis_date(None)
    return {t: (r.get("name") or t) for t, r in pit.load_prices(basis).items()}


def analyze(req: dict) -> str:
    """파싱된 요청 → §5 리포트 텍스트."""
    ticker = req["ticker"]
    basis, corrected = pit.resolve_basis_date(req["date"])
    prices = pit.load_prices(basis)
    row = prices.get(ticker)
    if row is None or row.get("mktcap") is None:
        raise RuntimeError(f"{ticker}: {basis} 시세/시총 없음(거래정지?)")
    close, mktcap = row.get("close"), row["mktcap"]
    shares = (mktcap / close) if close else None

    fin_meta: dict = {}
    fins, history, fin_as_of = pit.load_financials_asof(basis, require_ticker=ticker,
                                                        meta=fin_meta)
    if ticker not in fins:
        raise RuntimeError(f"{ticker}: 기준일 이전 재무 SSOT 없음")

    metrics_all = {
        t: metrics.compute_derived(f, mktcap=(prices.get(t) or {}).get("mktcap"),
                                   history=history.get(t) or None)
        for t, f in fins.items()
    }
    m = metrics_all[ticker]

    # TTM 손익 결측 자동 보정 — 전년 동기/연간 조각이 SSOT에 없으면 해당 기업만
    # DART에서 재입수해 적재 후 재계산한다 (1회, best-effort).
    ttm_backfilled = False
    _corp0 = fins[ticker].get("corp_code")
    if (config.DART_API_KEY and _corp0 and fin_meta.get("reprt")
            and fin_meta["reprt"] != dart.REPRT_ANNUAL
            and any(f.endswith(("_flow_basis_mismatch", "_ttm_fallback_annual"))
                    for f in (m.get("flags") or []))):
        fixed = 0
        for y, r in ((fin_meta["year"] - 1, fin_meta["reprt"]),
                     (fin_meta["year"] - 1, dart.REPRT_ANNUAL),
                     (fin_meta["year"], fin_meta["reprt"])):
            try:
                fixed += pit.backfill_company(ticker, _corp0, y, r)
            except RuntimeError as e:
                print(f"[query] 재무 백필 실패(무시) {y}_{r}: {e}")
        if fixed:
            fins, history, fin_as_of = pit.load_financials_asof(
                basis, require_ticker=ticker, meta=fin_meta)
            m = metrics.compute_derived(
                fins[ticker], mktcap=mktcap, history=history.get(ticker) or None)
            metrics_all[ticker] = m
            ttm_backfilled = True
            print(f"[query] TTM 재무 백필 완료({fixed}개 보고서) — 재채점")

    peers = {k: [x[k] for x in metrics_all.values() if x.get(k) is not None]
             for k in ("ev_ebit", "per", "pbr", "psr",
                       "net_cash_to_mktcap", "ncav_to_mktcap")}

    # PIT 공시(촉매 근거): 기준일 이전 180일 — DART 키 있을 때만
    disclosures: list[dict] = []
    corp = fins[ticker].get("corp_code")
    if config.DART_API_KEY and corp:
        try:
            bgn = f"{int(basis[:4]) - 1}{basis[4:]}" if basis[4:8] < "0630" else basis[:4] + "0101"
            disclosures = dart.get_recent_disclosures(corp, bgn, basis)
        except RuntimeError:
            pass

    asym = asymmetry.compute(m)
    flags = list(m.get("flags") or [])
    ctx = {"basis": basis, "corrected_from": req["date"] if corrected else None,
           "close": close, "mktcap": mktcap, "fin_as_of": _fin_as_of_kr(fin_as_of)}

    # 하락 요인 분해(시장 vs 개별) — 경량 2점 비교, best-effort (실패 시 생략)
    decline = _market_decline(ticker, close, basis, prices)
    if decline and decline.get("note"):
        ctx["decline_note"] = decline["note"]

    scheme = req["scheme"]
    checklist_override: list[str] | None = None
    if scheme == "dhandho":
        # 정성 입력 수집 (best-effort) — E1 배당·자사주, F2 내부자 소유보고,
        # LLM 그라운딩용 정기보고서 본문·수시공시 본문·임원 현황 (트리거 A와 동일 소스)
        insider: list[dict] | None = None
        shareholder = None
        docs: dict = {"disclosures": disclosures}
        if config.DART_API_KEY and corp:
            year = int(basis[:4]) - 1
            try:
                insider = dart.get_insider_transactions(corp)
                shareholder = _shareholder_summary(
                    dart.get_dividend_info(corp, year),
                    dart.get_treasury_stock(corp, year))
            except RuntimeError as e:
                print(f"[query] 배당·내부자 조회 실패(무시): {e}")
            try:
                docs["executives"] = dart.get_executive_profiles(corp, year)
            except RuntimeError:
                pass
            try:
                per = dart.get_latest_periodic(corp, basis)
                if per:
                    body = dart.get_document_text(per["rcept_no"])
                    docs["periodic"] = {**per,
                                        "text": dart.extract_business_sections(body)}
            except RuntimeError as e:
                print(f"[query] 정기보고서 본문 조회 실패(무시): {e}")
            texts = []
            for d0 in disclosures[:2]:
                try:
                    texts.append({"report_nm": d0.get("report_nm"),
                                  "rcept_dt": d0.get("rcept_dt"),
                                  "rcept_no": d0.get("rcept_no"),
                                  "text": dart.get_document_text(d0["rcept_no"])[:1500]})
                except RuntimeError:
                    pass
            docs["disclosure_texts"] = texts
        # 뉴스 헤드라인 (tier 3) — 급등/급락 사유 그라운딩 보조. 실패해도 무시.
        try:
            docs["news"] = news.search_news(f"{req['name']} 주가")
        except Exception as e:                        # noqa: BLE001
            print(f"[query] 뉴스 검색 실패(무시): {e}")
        # 뉴스가 없으면 시장 요인 분해(β·지수 동반락)라도 근거로 공급
        if not docs.get("news") and decline and decline.get("note"):
            docs["market_note"] = decline["note"]
        # 검색량 추세 (구글 트렌드, best-effort) — 성장 추세 훼손 여부 보조 지표.
        # 비공식 API라 차단이 잦다 — 실패는 조용히 생략(점수 직접 반영 없음).
        trend = None
        try:
            trend = trends.search_trend(req["name"])
        except Exception as e:                        # noqa: BLE001
            print(f"[query] 트렌드 조회 실패(무시): {e}")
        if trend:
            docs["trend_note"] = trend["note"]
        # 수출 추세 (관세청 품목 통계, best-effort) — HS 추정 후 전국 수출 YoY.
        # 품목 단위 근사치라 점수 미반영 — LLM 근거·리포트 '참고' 표기 전용.
        trade_info, trade_region = None, None
        if (config.CUSTOMS_COUNTRY_API_KEY and config.ANTHROPIC_API_KEY
                and (docs.get("periodic") or {}).get("text")):
            try:
                hs = llm.extract_hs(docs["periodic"]["text"])
                if hs:
                    trade_info = trade.export_yoy(hs["hs"], hs.get("product"))
                    # 시군구 프록시 — 공장 소재지가 추출된 경우만 (특이성 ↑)
                    if hs.get("sido") and hs.get("sgg"):
                        trade_region = trade.region_export_yoy(
                            hs["hs6"], hs["sido"], hs["sgg"])
            except Exception as e:                    # noqa: BLE001
                print(f"[query] 수출 통계 조회 실패(무시): {e}")
        if trade_info:
            docs["trade_note"] = trade_info["note"]
        if trade_region:
            docs["trade_region_note"] = trade_region["note"]

        qual, qual_fail = None, None
        if config.ANTHROPIC_API_KEY and (docs.get("periodic") or docs.get("disclosure_texts")
                                         or docs.get("news")):
            try:
                extracted = llm.extract_passages({ticker: docs})
                qual = llm.score_single(ticker, extracted[ticker])
                if qual.get("_error"):
                    qual_fail, qual = f"응답 파싱 실패({qual['_error']})", None
            except Exception as e:                   # noqa: BLE001 — 정성 실패는 정량 리포트를 막지 않음
                qual_fail = str(e)[:60]
                print(f"[query] LLM 정성 채점 실패(무시): {e}")

        result = frameworks.score_dhandho(m, qual=qual, peers=peers,
                                          disclosures=disclosures,
                                          shareholder=shareholder,
                                          insider=insider)
        decision = gate.decide_signal(result)
        secs = result["sections"]
        ctx["headline"] = _dhandho_headline(decision, secs)
        ctx["valuation_bullets"] = _dhandho_valuation(m, asym)
        grounded_items = [k for k in ("B4", "D2", "D3", "F1", "F3")
                          if ((qual or {}).get(k) or {}).get("score") is not None]
        for k in grounded_items:
            item = qual[k]
            ctx["valuation_bullets"].append(
                f"정성(LLM) {report_labels.SUBSCORE_KR.get(k, k)}: "
                f"{float(item['score']):.1f}점 — {item.get('reason') or '근거 요약 없음'}")
        # 자동 반영된 항목은 '사람이 직접 확인할 것'에서 제외 (중복 지시 방지)
        tunneling = bool(((qual or {}).get("F3") or {}).get("tunneling_confirmed"))
        checklist_override = []
        if "D2" not in grounded_items:
            if docs.get("news") or docs.get("disclosure_texts"):
                checklist_override.append(
                    "최근 주가 부진의 원인 — 공시·뉴스 자동 검토에서 확정 근거를 찾지 "
                    "못했습니다 (업계 동향·기사 원문 직접 확인 필요)")
            else:
                checklist_override.append(_CHECKLIST_COMMON[0])
        if insider is None:
            checklist_override.append(_CHECKLIST_COMMON[1])
        if tunneling:
            checklist_override.append("⚠️ 공시에서 터널링 정황 감지(F3) — 관계자거래 원문 대조 필수")
        elif "F3" not in grounded_items:
            checklist_override.append(_CHECKLIST_COMMON[2])
        checklist_override += _CHECKLIST_SCHEME["dhandho"]
        # 재채점 대상: 그라운딩되지 않은 정성 항목만 (F2는 소유보고 확보 시 결정론 반영)
        _order = [("B4", "B4 해자"), ("D2", "D2 급락원인"), ("D3", "D3 산업전망"),
                  ("F1", "F1 자본배분"), ("F2", "F2 내부자"), ("F3", "F3 IR투명성")]
        pending_items = [label for key, label in _order
                         if (key == "F2" and insider is None)
                         or (key != "F2" and key not in grounded_items)]
        pending = (" · ".join(pending_items) if pending_items
                   else "없음 — 전 항목 그라운딩·결정론 반영(재검증은 선택)")
        if trend:
            ctx["valuation_bullets"].append(f"검색 관심도(보조): {trend['note']}")
        if trade_info:
            ctx["valuation_bullets"].append(f"수출 추세(보조): {trade_info['note']}")
        if trade_region:
            ctx["valuation_bullets"].append(
                f"수출 추세(시군구 프록시): {trade_region['note']}")
        ctx["section_title"] = "단도 6개 렌즈 평가"
        ctx["section_scores"] = [
            (report_labels.SECTION_KR[k], secs[k]["total"],
             report_labels.grade_word(secs[k]["total"])) for k in "ABCDEF"]
        ctx["score_caveat"] = _dhandho_caveat(grounded_items, qual_fail)
        qual_subs = " ".join(
            f"{c}={secs[sec]['subscores'][c]['score']:.2f}"
            for sec, c in (("B", "B4"), ("D", "D2"), ("D", "D3"),
                           ("F", "F1"), ("F", "F2"), ("F", "F3")))
        ho = {"scores": {k: secs[k]["total"] for k in "ABCDEF"},
              "total": decision["total"], "grade": decision["verdict"],
              "gates": {"A≥3.0": secs["A"]["total"] >= 3.0,
                        "D≥3.0": secs["D"]["total"] >= 3.0},
              "extras": [f"정성 하위점수(2.5=근거부족 보수값): {qual_subs}",
                         "정성 그라운딩(LLM): "
                         + ("·".join(grounded_items) + " 반영" if grounded_items
                            else "미반영" + (f" — {qual_fail}" if qual_fail else "")),
                         "섹션가중: A=.25 B=.20 C=.20 D=.15 E=.10 F=.10"],
              "pending": pending}
    elif scheme == "lynch":
        r = frameworks_lynch.score_lynch(m)
        ctx["headline"] = (f"피터 린치 관점에서 '{r['category']}' 유형으로 분류되며, "
                           f"평가 등급은 '{r['grade']}'입니다 "
                           f"(종합 {r['score']:.1f}점 / 5점 만점). {r['basis']}.")
        ctx["valuation_bullets"] = [
            f"카테고리: {r['category']}",
            f"PEG(주가수익성장배율): {_num(r.get('peg'))}"
            + ("  — 낮을수록 성장 대비 저평가" if r.get("peg") is not None else ""),
            f"배당조정 린치지수: {_num(r.get('peg_adj'))}"
            + ("  — (성장률+배당)÷PER, 높을수록 유리 (1.0 적정·1.5 이상 우수)"
               if r.get("peg_adj") is not None else ""),
            f"5년 EPS 성장률: {_pct(m.get('eps_cagr_5y'))} · 부채비율: {_pct_ratio(m.get('debt_ratio'))}",
        ]
        ctx["section_title"] = "피터 린치 항목별 평가"
        ctx["section_scores"] = [
            (report_labels.SUBSCORE_KR.get(k, k), v["score"],
             report_labels.grade_word(v["score"])) for k, v in r["subscores"].items()]
        ctx["score_caveat"] = r["limits"]
        flags += r["flags"]
        ho = {"scores": {k: v["score"] for k, v in r["subscores"].items()},
              "total": r["score"], "grade": r["grade"],
              "gates": {"LB≥3.0": r["subscores"]["LB"]["score"] >= 3.0},
              "extras": [f"카테고리={r['category']} · PEG={_num(r.get('peg'))} "
                         f"· 배당조정 린치지수={_num(r.get('peg_adj'))}",
                         "섹션가중: LA=.15 LB=.30 LC=.20 LD=.15 LE=.20"]}
    elif scheme == "ackman":
        r = frameworks_ackman.score_ackman(m, disclosures)
        ctx["headline"] = (f"빌 애크먼 관점 종합 평가는 '{r['grade']}'입니다"
                           + (f" — {r['labels'][0]}." if r["labels"] else "."))
        ev = r["catalyst_evidence"]
        ctx["valuation_bullets"] = [
            f"기업 퀄리티: {r['quality']:.1f}점 · 촉매 실현성: {r['catalyst']:.1f}점",
            f"FCF 마진: {_pct(m.get('fcf_margin'))} · 자본수익성(ROIC): {_pct(m.get('roic'))}",
            ("촉매 근거: " + "; ".join(ev[:3])) if ev else "촉매를 뒷받침할 공시 근거가 없습니다.",
        ]
        ctx["section_scores"] = [("종합 평가", r["total"],
                                  report_labels.grade_word(r["total"]))]
        ctx["score_caveat"] = r["limits"]
        flags += r["flags"]
        ho = {"scores": {"quality": r["quality"], "catalyst": r["catalyst"]},
              "total": r["total"], "grade": r["grade"], "gates": None,
              "extras": [f"촉매 근거 {len(ev)}건 · 결합식: 총점=0.6×quality+0.4×catalyst"]}
    else:                                        # ltgg / outsiders / buffett
        r, bullets = _score_other(scheme, m)
        ctx["headline"] = (f"{req['scheme_label']} 관점 종합 평가는 '{r['grade']}'입니다"
                           f" (종합 {r['total']:.1f}점 / 5점 만점).")
        ctx["valuation_bullets"] = bullets
        subs = r.get("subscores") or {}
        ctx["section_title"] = f"{req['scheme_label']} 항목별 평가"
        ctx["section_scores"] = [
            (report_labels.SUBSCORE_KR.get(k, k), v["score"],
             report_labels.grade_word(v["score"])) for k, v in subs.items()]
        ctx["score_caveat"] = ("정성 하위지표는 미반영되어 보수적으로 처리됐습니다. "
                               "근거 확보 후 재평가가 필요합니다.")
        flags += r.get("flags") or []
        extras = {"ltgg": ["섹션가중: L1=.25 L2=.20 L3=.20 L4=.15 L5=.10 L6=.10"],
                  "outsiders": [
                      f"이원총점: 적용={r['total']:.2f} 해소가정={r.get('total_reform', 0) or 0:.2f}",
                      "섹션가중: OA=.30 OB=.25 OC=.20 OD=.15 OE=.10 "
                      "(해소가정: OA=.3333 OB=.2778 OC=.2222 OD=.1667)"],
                  "buffett": ["섹션가중: BA=.15 BB=.25 BC=.20 BD=.20 BE=.20"]}[scheme]
        if scheme == "buffett":
            pre = r.get("quant_precap") or {}
            extras.append("캡 이전 정량값: "
                          + " ".join(f"{k}={_num(pre.get(k))}" for k in ("BB", "BC")))
            if r.get("mos_discount") is not None:
                extras.append(f"내재가치 할인율(MOS)={r['mos_discount']:.1%}")
        ho = {"scores": {k: v["score"] for k, v in subs.items()},
              "total": r["total"], "grade": r["grade"],
              "gates": r.get("gates"), "extras": extras}

    evidence = (frameworks_ackman.catalyst_score(m, disclosures)[1]
                if scheme in ("dhandho", "ackman") else [])
    tp = target_price.compute(scheme, m, close, shares, asym=asym,
                              catalyst_evidence=evidence)
    ctx["entry"] = tp["entry"]
    ctx["targets"] = tp["targets"]
    ctx["assumptions"] = tp["assumptions"]
    ctx["checklist"] = (checklist_override if checklist_override is not None
                        else _CHECKLIST_COMMON + _CHECKLIST_SCHEME.get(scheme, []))
    ctx["data_status"] = report_labels.translate_flags(
        flags, ttm_backfilled=(scheme == "dhandho" and ttm_backfilled))
    if scheme == "dhandho" and ttm_backfilled:
        ctx["data_status"].append(
            "전년 동기 재무를 DART에서 자동 재입수해 TTM 손익을 보정·재채점했습니다.")
    ctx["handoff"] = _handoff_lines(req, basis, flags=sorted(set(flags)), **ho)
    return report_format.build(req, ctx)


def _fin_as_of_kr(s: str) -> str:
    """'2026 1분기보고서(법정기한 …)' → 사람이 읽는 문장으로 다듬기."""
    return (s.replace("법정기한 ", "").replace(" 기준 추정)", " 이전 공시분)")
             .replace("손익 TTM 변환", "손익은 최근 12개월 환산"))


def _josa(word: str, batchim: str = "이", no_batchim: str = "가") -> str:
    """마지막 한글 음절의 받침 유무로 주격조사 선택 (괄호 등 비한글 꼬리 무시)."""
    for ch in reversed(word):
        if "가" <= ch <= "힣":
            return batchim if (ord(ch) - 0xAC00) % 28 else no_batchim
    return batchim


def _dhandho_caveat(grounded_items: list[str], qual_fail: str | None) -> str:
    """단도 점수 주석 — 정성 그라운딩 반영 범위를 정직하게 표기."""
    if grounded_items:
        rest = [k for k in ("B4", "D2", "D3", "F1", "F3") if k not in grounded_items]
        s = ("정성 항목 " + "·".join(grounded_items)
             + "는 DART 공시 원문 기반 LLM 채점이 반영됐습니다")
        if rest:
            s += f" ({'·'.join(rest)}는 근거 부족으로 보수 처리)"
        return s + ". 최종 판단에는 사람의 확인이 필요합니다."
    s = ("정성 항목(해자·급락 원인·산업 전망·자본배분·IR 투명성)은 "
         "근거 미확보로 보수적으로 처리됐습니다")
    if qual_fail:
        s += f" (LLM 채점 실패: {qual_fail})"
    return s + ". 최종 판단에는 사람의 확인이 필요합니다."


def _dhandho_headline(decision: dict, secs: dict) -> str:
    verdict_kr = report_labels.VERDICT_KR.get(decision["verdict"], decision["verdict"])
    total = decision["total"]
    if decision["verdict"] == "BUY":
        return (f"정량 기준으로 '{verdict_kr}'입니다. 하방보호와 밸류트랩 배제 기준을 "
                f"통과했고 종합 점수도 기준선을 넘었습니다 (종합 {total:.1f}점 / 5점 만점).")
    weak = []
    if secs["A"]["total"] < 3.0:
        weak.append("안전마진(하방보호)")
    if secs["D"]["total"] < 3.0:
        weak.append("안정성(밸류트랩 배제)")
    if secs["C"]["total"] < 3.0:
        weak.append("저평가 매력")
    weak_txt = "·".join(weak) if weak else "일부 항목"
    return (f"정량 기준 '{verdict_kr}' 대상입니다. {weak_txt}{_josa(weak_txt)} 단도 기준선에 못 미쳐, "
            f"현재 가격대에서는 매수 근거가 약합니다 (종합 {total:.1f}점 / 5점 만점).")


def _dhandho_valuation(m: dict, asym: dict) -> list[str]:
    out = []
    nc = m.get("net_cash_to_mktcap")
    if nc is None:
        out.append("순현금 상태: 계산 불가")
    elif nc >= 0:
        out.append(f"순현금 상태: 순현금 우위 — 보유 순현금이 시가총액의 약 {nc:.0%}")
    else:
        out.append(f"순현금 상태: 순부채 우위 — 순차입금이 시가총액의 약 {abs(nc):.0%}")
    nv = m.get("ncav_to_mktcap")
    if nv is None:
        out.append("청산가치(NCAV): 계산 불가")
    elif nv >= 0:
        out.append(f"청산가치(유동자산−총부채): 시가총액의 약 {nv:.0%}")
    else:
        out.append(f"청산가치(유동자산−총부채): 시가총액의 약 {nv:.0%} — "
                   f"유동자산으로 총부채를 감당하지 못하는 구조")
    per, ee = m.get("per"), m.get("ev_ebit")
    if per is None and ee is None:
        out.append("이익 대비 밸류에이션(PER·EV/EBIT): 산출 불가 — 이번 분기 손익 데이터가 "
                   "아직 채워지지 않았습니다")
    else:
        parts = []
        if per is not None:
            parts.append(f"PER {per:.1f}배")
        if ee is not None:
            parts.append(f"EV/EBIT {ee:.1f}배")
        out.append("이익 대비 밸류에이션: " + " · ".join(parts))
    out.append("업사이드/다운사이드 비대칭: "
               + asymmetry.verdict(asym.get("ratio"), asym.get("negative_risk")))
    return out


def _score_other(scheme: str, m: dict) -> tuple[dict, list[str]]:
    scorer = {"ltgg": frameworks.score_ltgg, "outsiders": frameworks.score_outsiders,
              "buffett": frameworks.score_buffett}[scheme]
    r = scorer(m)
    bullets = {
        "ltgg": [f"5년 매출 성장률(CAGR): {_pct(m.get('revenue_cagr_5y'))}",
                 f"증분 자본수익(ROIIC): {_pct(m.get('roiic'))}"],
        "outsiders": [f"FCF 수익률: {_pct(m.get('fcf_yield'))} · "
                      f"5년 FCF 성장률: {_pct(m.get('fcf_cagr_5y'))}",
                      f"증분 자본수익(ROIIC): {_pct(m.get('roiic'))} · "
                      f"자본수익성(ROIC): {_pct(m.get('roic'))}"],
        "buffett": [f"오너어닝스 수익률(≈FCF/시총): {_pct(m.get('fcf_yield'))}",
                    f"평균 자기자본이익률(ROE): {_pct(m.get('roe_mean'))} · "
                    f"EV/EBIT: {_num(m.get('ev_ebit'))}배"],
    }[scheme]
    if scheme == "buffett" and r.get("mos_discount") is not None:
        bullets.append(f"내재가치 대비 할인율: {_pct(r['mos_discount'])} "
                       f"(오너어닝스 간이 산정 — 30% 미만이면 매수급 판정 불가)")
    if scheme == "outsiders" and r.get("total_reform") is not None:
        bullets.append(f"이원 배점 — 디스카운트 존속 {r['total']:.1f}점 / "
                       f"해소 가정 {r['total_reform']:.1f}점 "
                       f"(스프레드 {r['spread']:+.1f}): {r['spread_note']}")
    gates = r.get("gates") or {}
    if gates:
        failed = [k for k, ok in gates.items() if not ok]
        bullets.append("핵심 관문(게이트): "
                       + ("전부 통과" if not failed else "미통과 — " + ", ".join(failed)))
    return r, bullets


def _num(x) -> str:
    return f"{x:.1f}" if isinstance(x, (int, float)) else "산출 불가"


def _pct_ratio(x) -> str:
    return f"{x:.0%}" if isinstance(x, (int, float)) else "—"


def _market_decline(ticker: str, close, basis: str, end_prices: dict,
                    lookback_days: int = 30) -> dict | None:
    """기준일과 약 lookback_days일 전 스냅샷 2점으로 시장 요인 분해.

    지수 데이터를 따로 받지 않고 전 종목 시총 합 변화를 시장 프록시로 쓴다.
    KRX 조회 ≤5회(휴장일 보정)로 가볍게. 실패하면 None(리포트에서 생략).
    """
    try:
        d = dt.date(int(basis[:4]), int(basis[4:6]), int(basis[6:8]))
        start_d = d - dt.timedelta(days=lookback_days)
        start_rows = None
        for _ in range(5):                      # 휴장일이면 하루씩 당겨 재시도
            rows = krx.get_market_snapshot(start_d.strftime("%Y%m%d"))
            if rows:
                start_rows = rows
                break
            start_d -= dt.timedelta(days=1)
        if not start_rows or not close:
            return None
        srow = next((r for r in start_rows if r.get("ticker") == ticker), None)
        if not srow or not srow.get("close"):
            return None
        stock_change = close / srow["close"] - 1.0
        mkt_change = market.two_point_change(start_rows, list(end_prices.values()))
        weeks = max(1, (d - start_d).days // 7)
        return market.assess_decline(stock_change, mkt_change, None,
                                     window_label=f"최근 약 {weeks}주")
    except Exception:                            # noqa: BLE001 — 부가정보라 실패 무시
        return None


def analyze_text(text: str) -> str:
    universe = load_universe_names()
    req = query_parser.parse(text, universe)
    return analyze(req)


def _f(x) -> str:
    return f"{x:.2f}" if isinstance(x, (int, float)) else "—"


def _pct(x) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "—"


def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print(query_parser.USAGE)
        return 1
    try:
        report = analyze_text(text)
        notify.send_bot1(report)
        print(report)
        return 0
    except query_parser.ParseError as e:
        msg = report_format.usage_error(str(e))
        notify.send_bot1(msg)
        print(msg)
        return 1
    except Exception:
        notify.notify_failure("run_query", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
