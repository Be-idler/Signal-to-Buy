# Signal-to-Buy — 단도투자 다관점 스코어링 시스템 v2

**신호 전용 시스템이다. 자동매매·주문실행 코드는 없으며 앞으로도 만들지 않는다.
시스템은 '후보'를 알릴 뿐, 최종 매수/매도 판단은 사람이 한다.**

v1(Dhandho_RSI30, RSI<30 → 단도 게이트 → 신호)의 검증된 로직을 이식해
영속 데이터층 · 2트랙 · 2봇 구조로 확장한 새 저장소다. v1은 v2가 백테스트·안정성
검증을 통과할 때까지 병행 가동한다.

명세서: [`docs/단도투자_스코어링_시스템_v2_명세서.md`](docs/단도투자_스코어링_시스템_v2_명세서.md)
(특히 §13 코드 매핑) · [`docs/다관점_스코어링_시스템_v2_아키텍처.md`](docs/다관점_스코어링_시스템_v2_아키텍처.md)

## 구조 (2-트랙 · 2-봇)

```
[분기 L-Q]  run_quarterly.py   전 종목 DART 재무 → 정규화(SSOT) → Drive Parquet
                               ⚠️ 시총 없이 재무만 저장 (시총은 매일 변함)

[트랙1·일일·봇1]  ── 단도 단일 관점 ──────────────────────────────
  run_trigger_a.py (UTC 11:00 = KST 20:00)
    ① KRX EOD(가격+시총) 수집 → prices/eod_{date}.parquet
    ② RSI<30 1차 후보
    ③ 분기 재무 + 당일 시총 결합 → metrics.compute_derived 재호출
    ④ 정량 게이트(A_quant·D_quant ≥ 3.0) → finalists
    ⑤ finalists 정성 자료 수집(DART 델타) → ⑥ Haiku 추출 → Sonnet 채점 Batch 제출
  run_trigger_b.py (UTC 21:30 = KST 06:30, 개장 전)
    배치 수신 → LLM 정성 포함 최종 게이트(§13.4) → 텔레그램 봇1

[트랙2·격주 토·봇2]  ── 다관점 랭킹 (RSI 게이트 없음) ─────────────
  run_biweekly.py
    Drive → 로컬 캐시 → DuckDB 전수 조회 → LTGG·아웃사이더·마법공식·버핏멍거
    전수 랭킹 → 관점별 상위 N 다이제스트 → 텔레그램 봇2
```

핵심 원칙: 점수 평균 금지(관점별 독립, §6) · 근거불충분 = 2.5 상한 + 플래그(§13.0) ·
공식 출처 강제(§10) · **모든 임계·가중은 백테스트 전 제안값**.

## 모듈

| 파일 | 역할 |
|---|---|
| `dhandho/dart.py` | OpenDART 수집·정규화(corp_code, 재무, 수시공시, 임원약력) |
| `dhandho/krx.py` | KRX 일일 EOD 가격+시총, 휴장일 판정 |
| `dhandho/rsi.py` | Wilder RSI (v1 검증 로직) |
| `dhandho/metrics.py` | 공통 파생지표 풀 §4 (재무 단독분·시총 결합분·다년분) |
| `dhandho/sector_relative.py` | 업종 상대 백분위 점수 §13.0 |
| `dhandho/frameworks.py` | 단도(§13)·LTGG·아웃사이더·버핏멍거·마법공식 §5 |
| `dhandho/gate.py` | 2단계 게이트 §13.4 (정량 사전필터 / 최종 신호) |
| `dhandho/llm.py` | Haiku 추출 → Sonnet 채점 Batch API + 루브릭 캐싱 §10 |
| `dhandho/storage.py` | Google Drive Parquet SSOT + `sync_prefix_to_local`(DuckDB용) |
| `dhandho/notify.py` | 텔레그램 봇1(일일)·봇2(격주), 실패 통보 |
| `scripts/validate_dart.py` | 1단계 실키 검증 하네스(운영자 수동 실행) |

## 운영자 사전 준비 (사람이 직접)

1. **Google Cloud 프로젝트** 생성 → **Drive API Enable**
2. **OAuth 동의화면 + 데스크톱 앱 클라이언트 ID** 발급 → `client_secret.json`
3. **랩탑에서 1회** 설치형 OAuth 동의 실행(스코프 `drive.file`) → `token.json` 생성
   후 실행 환경에 배치 (서비스 계정은 개인 드라이브 용량이 없어 사용 불가)
4. 드라이브에 SSOT 루트 폴더 생성 → 폴더 ID를 `GDRIVE_ROOT_FOLDER_ID`로
5. 환경변수(현재 저장소 Secrets에 등록 완료): `DART_API_KEY`, `KRX_API_KEY`,
   `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   — 봇1/봇2 전용 변수(`TELEGRAM_BOT1_*`/`TELEGRAM_BOT2_*`)가 없으면 공용 봇으로 폴백.
   Drive용은 추가 등록 필요: `GDRIVE_ROOT_FOLDER_ID`, `GDRIVE_TOKEN_JSON_B64`
   (token.json의 base64. 로컬 실행 시엔 `GDRIVE_TOKEN_FILE` 경로 사용)
6. KRX Open API는 인증키 발급과 **별개로 데이터셋별 이용신청** 필요
   (유가증권/코스닥 일별매매정보 — v1 README 참조)

### token.json 최초 생성 (랩탑, 1회)

client_secret.json을 저장소 루트에 두고(커밋 금지 — .gitignore로 제외됨) 실행:

```bash
pip install google-auth-oauthlib google-api-python-client
python scripts/gdrive_auth.py        # 브라우저 동의 → token.json 생성
```

스크립트가 ① OAuth 동의(drive.file) ② SSOT 루트 폴더 생성 ③ GitHub Secrets에
넣을 `GDRIVE_ROOT_FOLDER_ID`·`GDRIVE_TOKEN_JSON_B64` 값까지 한 번에 출력한다.

## 실행 순서 (최초 가동)

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                 # 산식·게이트 단위 테스트

# 1단계 검증: 실키로 삼성전자·SK하이닉스 중간값 확인 → ACCOUNT_MAP 보정
DART_API_KEY=... python scripts/validate_dart.py 005930 000660

python run_quarterly.py 2025 11011          # 분기 SSOT 적재(+과거 5년 백필)
python run_trigger_a.py                     # 일일 전반부 (장 마감 후)
python run_trigger_b.py                     # 일일 후반부 (다음날 개장 전)
python run_biweekly.py                      # 격주 다관점 랭킹
```

## v1 정렬 현황

v1 구현명세서(`docs/v1_구현명세서.md`)·README(`docs/v1_README.md`) 기준으로 정렬됨:

- **섹션 구성 = v1 §4 그대로**: B 수익성(B1 ROIC .35 / B2 마진예측 .25 / B3 추세 .15 /
  B4 해자 .25), E 주주환원(E1 .40 / E2 상법수혜 .35 / E3 촉매근접 .25),
  F 내부자(F1 자본배분 .35 / F2 내부자정렬 .30 / F3 IR투명성 .35)
- **LLM 그라운딩 항목 = v1 §7**: B4·D2·D3·F1·F3, grounded=false → 2.5 상한+플래그,
  압축 출력(reason ≤60자) + drop_reason/selection_reason, `LLM_MAX`(8) 상한
- **신호 = v1 §5**: BUY(게이트+총점≥4.0) / WATCH / PASS(총점<3.0),
  A·D 근거불충분 플래그 시 BUY 불가
- **알림 = v1 §6**: BUY 우선, BUY 0건이면 그라운딩 숏리스트 폴백(3줄/종목)
- **KRX = v1 L0**: 공식 Open API(data-dbg.krx.co.kr, AUTH_KEY) + L1 유동성 필터
  (보통주만 · 20일 평균 거래대금 ≥1억 · 거래정지 제외)
- **E1·F2 결정론 산출 = v1 §4**: DART alotMatter(배당)·tesstkAcqsSttus(자기주식)·
  elestock(내부자 소유보고) 연동

## 미해결 · 검증 필요 (플래그로 표시됨)

- **1단계 실키 검증**: `scripts/validate_dart.py`를 실키로 실행해
  `operating_income` 태그·차입금 합산을 실측 확인 → `ACCOUNT_MAP` 보정.
- **E1 세부 매핑**: v1 명세서는 '미소각 자사주비중+배당 결정론'까지만 정의 —
  정확한 점수 매핑은 v1 코드 대조 필요(`E1_heuristic` 플래그).
- **E2·E3 상법 타임라인**: v1 `policy_timeline` 테이블 데이터 미이식 — 확보 전까지
  2.5 캡(E3는 확정 공시 proxy 폴백).
- **C2 자기 5년 밴드**: 과거 시총 시계열 축적 전까지 2.5 캡(적재가 쌓이면 자동 개선).
- **업종 세분류**: 현재 전체시장 백분위 폴백 — v1 universe 태깅(금융/지주/리츠) 이식 시
  §13.0 완전 충족.
- **백테스트**: 모든 임계·가중은 제안값. 검증 전 실매매 판단 금지.
