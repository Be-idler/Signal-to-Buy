# Dhandho_RSI30

한국 상장기업 중 **RSI 14일 ≤ 30** 으로 1차 필터링한 종목에, 단도투자(Mohnish Pabrai,
*Dhandho*) 관점의 정량·정성·정책(상법) 스코어링(A~F)을 적용해 **BUY/WATCH/PASS 신호를
산출**하고, **LLM 정성 그라운딩까지 통과한 종목만 텔레그램으로 알림**하는 스크리닝 시스템.

> 자동매매 아님. 최종 매수/매도 판단은 사용자가 공식 근거로 수행. 상세 설계는 **구현 명세서** 참조.

## 파이프라인

```
L0 데이터수집 → L1 RSI 1차필터 → L2~L6 단도 스코어링(A~F) → L7 게이트/신호 → L8 알림
```

| 레이어 | 내용 | 상태 |
|---|---|---|
| L0 | KRX 전 종목 EOD OHLCV 일괄 수집 → SQLite + 유니버스 갱신 | ✅ |
| L1 | Wilder 14일 RSI + 유동성/품질 필터 → 후보 추출 | ✅ |
| L2~L6 | 단도 스코어링 A~F (DART 재무 정량 + 정성: 정형 결정론 + 서술형 LLM 그라운딩) | ✅ |
| L7 | 게이트(A≥3, D≥3, 플래그無) + 총점 → BUY/WATCH/PASS | ✅ |
| L8 | 텔레그램 알림: BUY 우선, BUY 0건이면 **그라운딩 숏리스트 폴백** | ✅ |

### 스코어링 섹션 (A~F)

| 섹션 | 라벨 | 가중치 | 하위지표 |
|---|---|---|---|
| A | 질적우위(안전마진) | 0.25 | 순현금 · NCAV · 재무건전성 · FCF안정 |
| B | 수익성 | 0.20 | ROIC · 마진예측 · 추세 · **해자**🔶 |
| C | 저평가 | 0.20 | 트레일링멀티플 · 과거밴드 · 이익추세 · 과도낙폭 |
| D | 안정성 | 0.15 | 매출이익추세 · **급락원인**🔶 · **산업사양화**🔶 · 재무생존력 |
| E | 주주환원 | 0.10 | 주주환원 · 상법수혜 · 촉매근접 |
| F | 내부자 | 0.10 | **자본배분**🔶 · 내부자정렬 · **IR투명성**🔶 |

🔶 = 서술형 LLM 그라운딩 항목(B4·D2·D3·F1·F3).

```
총점 = Σ(섹션가중치 × 섹션점수)
BUY   ⟺ A≥3 AND D≥3 AND A·D 무플래그(게이트) AND 총점 ≥ 4.0
PASS  ⟺ 총점 < 3.0,   WATCH = 그 외
```

> D 게이트는 D2·D3가 LLM으로 그라운딩돼야 통과 가능 → **BUY = 정성 LLM 검증을 거친 고확신 후보**.

### 정성 지표 처리

- **정형 자동화**: `E1`(미소각 자사주비중+배당)·`D4`(감사의견·계속기업)·`F2`(내부자 순증감)는
  DART 정기보고서/소유보고로 **결정론적** 산출. 부하 관리로 가장 과매도된 상위 `QUAL_MAX`(100)개만 적재.
- **서술형 LLM 그라운딩**: `B4`·`D2`·`D3`·`F1`·`F3`은 Claude가 **DART 근거(재무 스냅샷·가격동향·공시)에만**
  기반해 1~5 판정. 근거 부족 항목은 `grounded=false`로 반환 → §8 규칙(2.5 상한+플래그) 유지.
  판정 사유는 `qual_grounding` 테이블에 저장(사람 검토). `ANTHROPIC_API_KEY` 없으면 자동 비활성.

### LLM 토큰 절감 (5축) + 2모델 분리

1. **관련 근거만 입력** — 원문 전체가 아니라 구조화 스냅샷(재무·가격) + 공시 *제목*(절단).
2. **게이트 유망 후보만 호출** — A 통과(≥3·무플래그) + 정량총점 ≥ `LLM_MIN_TOTAL`(3.5) 상위 `LLM_MAX`(8)개만.
3. **계산 시점 동기 자체완결** — 같은 런·같은 거래일에 그라운딩 완료(날짜 키 불일치 방지).
   Batch API(50%↓)는 두 런으로 쪼개면 날짜가 어긋나 **기본 비활성**(`LLM_BATCH=1` 수동 옵션).
4. **프롬프트 캐싱** — 고정 채점 루브릭을 스코어링 system에 캐시. 최소 프리픽스 Sonnet/Haiku=2048·Opus=4096.
5. **압축 출력** — 점수 + 짧은 사유(≤60자)만, `LLM_MAX_TOKENS`(1500)·`extract_max_tokens`(700) 하향.

> **2모델**: 비싼 토큰(사업의 내용 원문·공시목록)은 **추출(Haiku 4.5, `CLAUDE_EXTRACT_MODEL`)** 이
> 항목별 단서로 압축하고, **스코어링(Sonnet 4.6, `CLAUDE_MODEL`)** 은 정형 스냅샷 + 압축 단서만 받아
> 1~5 판정. 원문은 best-effort 발췌(실패 시 공시목록 폴백).

### 알림 정책 (L8)

> **LLM 정성 그라운딩을 거친 종목만 알림에 나간다. 단순 필터링 결과는 알림 금지.**

| 상황 | 발송 |
|---|---|
| BUY ≥ 1건 | BUY 알림만 (종목별 상세) |
| **BUY 0건** | **그라운딩 숏리스트 폴백** — `selection_reason`/`drop_reason`이 있는 그라운딩 종목만 다이제스트 |
| `SEND_WATCH_DIGEST=1` | (점검용 옵트인) 전체 결과 다이제스트 동봉 |

폴백 다이제스트(3줄/종목): `종목명(코드) 총점·신호·RSI` / 하락사유(LLM D2 우선) / 종목선정사유(LLM 정성 문장).
중복 방지: BUY는 `signals_log`, 다이제스트는 `__DIGEST__` 센티넬로 거래일 1회.

## 무서버 운영 (MVP) — 계산/전송 2단계 분리

GitHub cron의 발화 지연·날짜 불일치를 피하기 위해 **계산과 전송을 분리**한다(일자 하드코딩 없음):

- **계산** `daily_screen.yml` — 평일 **08:30 UTC**(17:30 KST, KRX 마감 후). 수집+채점+정성 그라운딩까지
  끝내고 *렌더된 알림*을 `notify_queue`에 저장(발송 X).
- **전송** `daily_notify.yml` — **+13h = 21:30 UTC**(다음날 06:30 KST, 비서머타임 미국 마감+30분).
  저장된 **최신 미발송** 알림을 그대로 발송(즉시).

두 실행이 같은 KRX 거래일을 보므로 날짜 키 불일치가 없고, 무거운 채점/LLM은 저녁에 끝나 아침 전송은
지연 0. **채점 지연 폴백**: 계산이 전송창을 넘겨 끝나면 계산 런이 즉시 자체 발송(dedup). 실제 발송 여부는
`market_clock.py`가 **미국 동부 실시각**(DST 자동)으로 '마감+30분' 창에서만 통과시킨다(수동 실행은 우회).
SQLite는 `actions/cache`로 두 런 사이를 잇고(멱등), 누락 거래일은 다음 계산에서 catch-up.

기타: `dump_grounding`(저장 그라운딩 점검·수동) · `llm_check`(LLM 라이브 점검) · `llm_batch`(선택 배치·수동) ·
`connectivity_check`(연결 점검) · `ci`(테스트). Phase 4에서 LS증권 실시간 투입 시 로컬 호스트+Tailscale 이전 예정.

## 모듈 구조

```
config.py                 # 환경변수/파라미터 중앙 관리 (임계값 하드코딩 금지)
run_daily.py              # L0~L8 엔트리포인트(멱등) + 계산/전송 2단계
market_clock.py           # 미국 마감+30분 실행 게이트(DST 자동)
data/
  krx_eod.py              # KRX 공식 API 전 종목 EOD 일괄 수집
  universe.py             # 유니버스 구성 + 금융/지주/리츠 태깅
  dart_client.py          # DART OpenAPI 클라이언트
  dart_financials.py      # 재무제표 로드
  dart_qualitative.py     # 정성 정형(E1·D4·F2) 결정론 적재
  dart_disclosures.py     # 최근 공시목록(그라운딩 근거)
  dart_document.py        # '사업의 내용' 원문 발췌(추출 입력, best-effort)
indicators/rsi.py         # Wilder 14일 RSI
scoring/
  metrics.py              # 정량 재무지표(순현금·NCAV·ROIC·EV/EBIT·FCF 등)
  sector_relative.py      # 업종 백분위 점수화
  sections.py             # A~F 섹션 스코어링
  policy_timeline.py      # 상법 타임라인(E2/E3 촉매)
  gate.py                 # 총점·게이트·신호
  llm_grounding.py        # 서술형 LLM 그라운딩(2모델·캐싱·압축·동기/배치)
  engine.py               # L2~L7 오케스트레이션(2-pass + 그라운딩 sync/db/batch)
storage/db.py             # SQLite: prices·universe·scores·signals_log·qual_grounding·
                          #   llm_batch·notify_queue·policy_timeline …
notify/telegram.py        # BUY 알림 + 그라운딩 숏리스트 다이제스트
tests/                    # RSI·스코어링·게이트·LLM·시간게이트·계산/전송 테스트
.github/workflows/        # ci, daily_screen(계산), daily_notify(전송), dump_grounding,
                          #   llm_check, llm_batch, connectivity_check
```

## 설정 (환경변수 / GitHub Secrets·Vars)

**Secrets**: `KRX_API_KEY`, `DART_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
**Vars**: `CLAUDE_MODEL`(스코어링), `CLAUDE_EXTRACT_MODEL`(추출)

| 키 | 기본값 | 용도 |
|---|---|---|
| `RSI_PERIOD` / `RSI_THRESHOLD` / `RSI_MIN_PERIODS` / `RSI_LOOKBACK` | 14 / 30 / 30 / 120 | RSI |
| `LIQ_COMMON_ONLY` / `LIQ_MIN_VALUE` / `LIQ_WINDOW` | 1 / 1억 / 20 | 유동성 필터 |
| `LLM_GROUNDING` | 1 | LLM 그라운딩 on/off |
| `LLM_MAX` / `LLM_MIN_TOTAL` | 8 / 3.5 | 게이트 유망 후보 상한·임계 |
| `LLM_MAX_TOKENS` / `LLM_EXTRACT_MAX_TOKENS` | 1500 / 700 | 압축 출력 |
| `LLM_BATCH` / `LLM_SYNC_FALLBACK` | 0 / 1 | 배치(선택)·동기 보완 |
| `QUAL_MAX` | 100 | 정성 정형 적재 상한 |
| `SEND_WATCH_DIGEST` | 0 | 점검용 전체 다이제스트 옵트인 |
| `COMPUTE_UTC` / `NOTIFY_UTC` | 08:30 / 21:30 | 채점 지연 폴백 판정 기준 |
| `SCREEN_DELAY_MIN` / `SCREEN_WINDOW_MIN` | 30 / 60 | 미국 마감 기준 실행창 |
| `DB_PATH` | ./dhandho.sqlite | SQLite 경로 |

> KRX Open API는 `https://data-dbg.krx.co.kr/svc/apis` 호스트에 **POST(JSON 바디)** 로 호출하며
> `AUTH_KEY` 헤더로 인증한다. 인증키 발급과 **별개로 데이터셋별 이용신청**이 필요하다(유가증권/코스닥
> 일별매매정보, 종목기본정보 등). 엔드포인트/필드는 `config.KRXConfig`에서 조정 가능.

## 실행 (CLI)

```bash
pip install -r requirements.txt
cp .env.example .env                 # 키 입력

python run_daily.py                  # L0 수집 + L1~L8 (시간 게이트 적용)
python run_daily.py --collect-only   # 수집만
python run_daily.py --no-collect     # 기존 DB로 L1~L8
python run_daily.py --no-score       # L0~L1만
python run_daily.py --compute        # [1·2단계] 수집+채점+그라운딩 → notify_queue 저장(발송 X)
python run_daily.py --notify-only    # [전송] 최신 미발송 페이로드 발송
python run_daily.py --submit-batch   # [야간] LLM 그라운딩 배치 제출만
python run_daily.py --dump-grounding # 저장된 그라운딩(최신 거래일) 출력(점검)
python -m pytest -q                  # 테스트
```
