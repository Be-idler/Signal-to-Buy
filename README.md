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
5. 환경변수: `DART_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT1_TOKEN/CHAT_ID`,
   `TELEGRAM_BOT2_TOKEN/CHAT_ID`, `GDRIVE_CLIENT_SECRETS`, `GDRIVE_TOKEN_FILE`,
   `GDRIVE_ROOT_FOLDER_ID`, `LOCAL_CACHE_DIR`
6. GitHub Actions 사용 시 위 값들을 Secrets로 등록(+ `GDRIVE_TOKEN_JSON_B64` =
   token.json의 base64)

### token.json 최초 생성 (랩탑, 1회)

```python
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", scopes=["https://www.googleapis.com/auth/drive.file"])
creds = flow.run_local_server(port=0)
open("token.json", "w").write(creds.to_json())
```

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

## 미해결 · 검증 필요 (플래그로 표시됨)

- **1단계 실키 검증 미수행**: 이 환경에는 DART 키가 없어 `scripts/validate_dart.py`를
  운영자가 실행해 `operating_income` 태그·차입금 합산을 실측 확인해야 한다.
- **단도 B(사업질)·E(촉매)·F(경영진) 하위 정의**: v1 구현명세서(Dhandho_RSI30
  `docs/`) 접근 불가로 v2 §10 매핑 + 보수적 proxy로 구성 → 결과에
  `v1_spec_unverified` 플래그. v1 명세서 §3~8 대조 후 보정할 것.
- **C2 자기 5년 밴드**: 과거 시총 시계열 축적 전까지 2.5 캡(적재가 쌓이면 자동 개선).
- **업종 세분류**: 현재 전체시장 백분위 폴백 — KRX 업종 매핑 추가 시 §13.0 완전 충족.
- **백테스트**: 모든 임계·가중은 제안값. 검증 전 실매매 판단 금지.
