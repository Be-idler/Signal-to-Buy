"""단도투자 다관점 스코어링 시스템 v2 코어 패키지.

모듈 구성 (명세서 §2·§13 매핑):
- dart:            OpenDART 수집·정규화 (SSOT 원천)
- krx:             KRX 일일 EOD 가격 + 시가총액 (분기 재무와 분리 수집)
- rsi:             Wilder RSI (v1 검증 로직 이식)
- metrics:         공통 파생지표 풀 (§4) — 재무 단독분 + 시총 결합분 + 다년분
- sector_relative: 업종 상대 백분위 점수 (§13.0)
- frameworks:      5개 프레임워크 스코어링 (§5, §13)
- gate:            2단계 게이트 (§13.4)
- llm:             LLM 정성 분석 (§10) — Haiku 추출 → Sonnet 채점(Batch)
- storage:         Google Drive Parquet 저장/조회 + 로컬 DuckDB 캐시
- notify:          텔레그램 봇1(일일)·봇2(격주)
"""
