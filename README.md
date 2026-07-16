# Signal to Buy — 단도투자 시그널 봇

한국 상장주식을 파벡 단도의 **단도투자(Dhandho)** 원칙으로 걸러
"신호만" 보내는 자동화 파이프라인. **매매는 하지 않는다 — 판단은 사람.**

## 구조 (v1 최소 파이프라인)

```
KRX OpenAPI (시세·시총)          DART OpenAPI (재무·공시)
        │                                │
        ▼                                ▼
   [트리거 A] RSI<30 + 유동성 필터 → 정량 사전필터(단도 §13.4)
        │            finalists (소수)
        ▼
   [LLM 정성] Haiku 추출 → Sonnet 채점 (Batch, 근거 강제·2.5캡)
        │
        ▼
   [트리거 B] 최종 게이트(§6) → BUY / WATCH / PASS
        │
        ▼
   텔레그램 발송 + Google Drive 적재 (신호 원장·체크포인트)
```

## 실행 흐름 (매 영업일, KST)

| 시각 | 잡 | 내용 |
|---|---|---|
| 08:05 | 트리거 A (`signal-to-buy`) | 전영업일 시세 수집 → 후보 선별 → LLM 배치 제출 |
| 09:20 | 트리거 B (`signal-notify`) | 배치 회수 → 게이트 → 신호 발송·원장 적재 |

스케줄은 외부 크론(cron-job.org)이 `repository_dispatch`로 쏜다
(GitHub schedule은 지연·누락이 잦아 폐기).

## 온디맨드 질의

Actions → **query** 워크플로우: `<종목명|코드> <스킴> [기준일]`
(예: `삼성전자 단도 2026-06-30`) → 텔레그램으로 리포트 회신.
스킴: 단도 · 버핏 · 린치 · 아크만 · 아웃사이더 · LTGG

## 규율 (양보 불가)

- **신호만, 매매 없음** — 주문·API 트레이딩 코드 금지
- **시크릿은 GitHub Secrets로만** — 코드·로그에 노출 금지
- **PIT(시점) 규율** — 기준일에 알 수 있었던 정보만 사용
- **정성 점수는 근거 강제** — 근거 없으면 2.5 상한 (추측 금지)
- **원장 우선** — 발송 여부와 무관하게 채점 결과를 전부 기록

## 로컬 실행

```bash
pip install -r requirements.txt
python run_trigger_a.py                     # 일일 전반부 (수집·선별·배치 제출)
python run_trigger_b.py                     # 일일 후반부 (다음날 개장 전)
python run_query.py 삼성전자 단도            # 온디맨드 분석
python -m pytest tests/ -q                  # 테스트
```

필요 환경변수: `KRX_API_KEY` `DART_API_KEY` `ANTHROPIC_API_KEY`
`TELEGRAM_BOT_TOKEN` `TELEGRAM_CHAT_ID` `GDRIVE_ROOT_FOLDER_ID` (+ Drive `token.json`)
