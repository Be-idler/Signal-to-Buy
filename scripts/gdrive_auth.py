"""Google Drive OAuth 1회 설정 헬퍼 — 브라우저가 있는 랩탑에서 실행한다.

하는 일:
1. client_secret.json으로 설치형 OAuth 동의(스코프 drive.file, 최소 권한) 실행
   → refresh token 포함 token.json 생성
2. SSOT 루트 폴더를 드라이브에 생성(이미 있으면 재사용) → 폴더 ID 출력
3. GitHub Actions Secrets에 넣을 값 출력:
   - GDRIVE_TOKEN_JSON_B64  (token.json의 base64)
   - GDRIVE_ROOT_FOLDER_ID

사용:
  pip install google-auth-oauthlib google-api-python-client
  python scripts/gdrive_auth.py [--secrets client_secret.json] [--folder signal-to-buy-ssot]

주의: client_secret.json·token.json은 절대 커밋하지 않는다(.gitignore에 제외됨).
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", default=os.environ.get("GDRIVE_CLIENT_SECRETS",
                                                        "client_secret.json"))
    ap.add_argument("--token", default=os.environ.get("GDRIVE_TOKEN_FILE", "token.json"))
    ap.add_argument("--folder", default="signal-to-buy-ssot",
                    help="드라이브 SSOT 루트 폴더 이름")
    args = ap.parse_args()

    if not os.path.exists(args.secrets):
        print(f"client secrets 파일이 없습니다: {args.secrets}")
        return 1

    # ① OAuth 동의 (브라우저 필요 — 헤드리스 서버에서는 실행 불가)
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(args.secrets, scopes=SCOPES)
    creds = flow.run_local_server(port=0)
    with open(args.token, "w") as fh:
        fh.write(creds.to_json())
    print(f"✓ token.json 저장: {args.token}")
    if not creds.refresh_token:
        print("⚠ refresh_token이 없습니다 — 동의 화면에서 기존 승인을 철회 후 재실행하세요.")

    # ② SSOT 루트 폴더 생성/재사용
    from googleapiclient.discovery import build
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    q = (f"name='{args.folder}' and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false")
    found = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    if found:
        folder_id = found[0]["id"]
        print(f"✓ 기존 폴더 재사용: {args.folder}")
    else:
        meta = {"name": args.folder, "mimeType": "application/vnd.google-apps.folder"}
        folder_id = svc.files().create(body=meta, fields="id").execute()["id"]
        print(f"✓ 폴더 생성: {args.folder}")

    # ③ GitHub Secrets용 값 출력
    with open(args.token, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    print("\n───────── GitHub Actions Secrets에 등록할 값 ─────────")
    print(f"GDRIVE_ROOT_FOLDER_ID = {folder_id}")
    print(f"GDRIVE_TOKEN_JSON_B64 = (아래 한 줄 전체)\n{b64}")
    print("──────────────────────────────────────────────────────")
    print("로컬 실행 시에는 환경변수만 지정하면 됩니다:")
    print(f"  export GDRIVE_ROOT_FOLDER_ID={folder_id}")
    print(f"  export GDRIVE_TOKEN_FILE={args.token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
