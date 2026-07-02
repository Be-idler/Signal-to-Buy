"""전역 설정 — 시크릿은 전부 환경변수로만 읽는다(코드·로그에 키를 남기지 않는다).

명세서: 단도투자_스코어링_시스템_v2_명세서.md (docs/) 참조.
임계·가중치는 §13/v1 제안값이며 백테스트 전까지 실매매 판단 근거로 쓰지 않는다.
"""
import os

# ---------------------------------------------------------------- 시크릿(환경변수)
DART_API_KEY = os.environ.get("DART_API_KEY", "")

TELEGRAM_BOT1_TOKEN = os.environ.get("TELEGRAM_BOT1_TOKEN", "")   # 트랙1(일일 단도 신호)
TELEGRAM_BOT1_CHAT_ID = os.environ.get("TELEGRAM_BOT1_CHAT_ID", "")
TELEGRAM_BOT2_TOKEN = os.environ.get("TELEGRAM_BOT2_TOKEN", "")   # 트랙2(격주 다관점 랭킹)
TELEGRAM_BOT2_CHAT_ID = os.environ.get("TELEGRAM_BOT2_CHAT_ID", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Google Drive 저장 백엔드 (운영자 사전 준비 필요 — README 참조)
GDRIVE_CLIENT_SECRETS = os.environ.get("GDRIVE_CLIENT_SECRETS", "client_secret.json")
GDRIVE_TOKEN_FILE = os.environ.get("GDRIVE_TOKEN_FILE", "token.json")
GDRIVE_ROOT_FOLDER_ID = os.environ.get("GDRIVE_ROOT_FOLDER_ID", "")
LOCAL_CACHE_DIR = os.environ.get("LOCAL_CACHE_DIR", ".cache")

# ---------------------------------------------------------------- RSI (v1 검증 로직)
RSI_PERIOD = 14
RSI_THRESHOLD = 30.0
EOD_LOOKBACK_DAYS = 60          # RSI 계산용 최근 거래일 수

# ---------------------------------------------------------------- 게이트 임계 (§13, 제안값)
GATE_A_MIN = 3.0                # 하방보호 게이트
GATE_D_MIN = 3.0                # 밸류트랩 게이트
SCORE_BUY_MIN = 4.0             # 총점 ≥4.0 & 게이트 통과 → 적극/분할 후보
SCORE_WATCH_MIN = 3.0           # 3.0~4.0 → 관심
INSUFFICIENT_CAP = 2.5          # 근거불충분 상한 (§13.0)

# 단도 섹션 가중 (v1 §3~8: 하방 25 / 사업질 20 / 밸류 20 / 밸류트랩 15 / 촉매 10 / 경영진 10)
DHANDHO_SECTION_WEIGHTS = {"A": 0.25, "B": 0.20, "C": 0.20, "D": 0.15, "E": 0.10, "F": 0.10}

# ---------------------------------------------------------------- 마법공식 (§5.4)
MAGIC_FORMULA_MIN_MKTCAP = 50_000_000_000     # 최소 시총 5백억 원 (제안값, 백테스트 보정)
MAGIC_FORMULA_EXCLUDE_SECTORS = ("금융", "은행", "증권", "보험", "유틸리티", "전기가스")

# ---------------------------------------------------------------- LLM (§10)
LLM_EXTRACT_MODEL = os.environ.get("LLM_EXTRACT_MODEL", "claude-haiku-4-5")   # 원문 구절 추출
LLM_SCORE_MODEL = os.environ.get("LLM_SCORE_MODEL", "claude-sonnet-5")        # 정성 채점(Batch)
LLM_SCORE_MAX_TOKENS = 1500
LLM_EXTRACT_MAX_TOKENS = 2000

# ---------------------------------------------------------------- 트랙2
BIWEEKLY_TOP_N = 10             # 관점별 상위 N 다이제스트

# WACC 근사 (§5.2 L2 ROIIC 비교용, 밸류에이션 엔진 보류 중이므로 고정 근사값)
WACC_PROXY = 0.08
