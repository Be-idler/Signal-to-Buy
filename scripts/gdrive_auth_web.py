"""GitHub Actions(웹)에서 실행하는 2단계 Google Drive OAuth — 로컬 설치 불필요.

step=url   : 동의 URL 출력 → 브라우저에서 동의 → 'http://localhost/?code=...'로
             리다이렉트되며 "연결할 수 없음"이 뜨는 게 정상 — 주소창 URL 전체를 복사.
step=token : 복사한 URL(또는 code 값)을 입력받아 refresh token 교환 →
             SSOT 루트 폴더 생성 → gdrive_secrets.txt 파일로 출력(artifact).

필요 환경변수: GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET (step=token만)
주의: authorization code는 발급 후 ~10분 내에 step=token을 실행해야 한다.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse

import requests

SCOPE = "https://www.googleapis.com/auth/drive.file"
REDIRECT = "http://localhost"
TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER = "signal-to-buy-ssot"


def step_url(client_id: str) -> int:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # refresh token 발급
        "prompt": "consent",        # 기존 승인 있어도 refresh token 재발급
    }
    url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    print("아래 URL을 브라우저에 열어 동의하세요:\n")
    print(url)
    print("\n동의 후 'localhost에 연결할 수 없음' 화면이 뜨면 정상입니다.")
    print("그 화면의 주소창 URL 전체(code= 포함)를 복사해 step=token으로 재실행하세요.")
    print("(authorization code 유효시간 ~10분)")
    return 0


def _extract_code(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        qs = urllib.parse.urlparse(raw).query
        code = urllib.parse.parse_qs(qs).get("code", [""])[0]
        if not code:
            raise ValueError("URL에서 code 파라미터를 찾지 못했습니다")
        return code
    return raw


def step_token(client_id: str, client_secret: str, raw_code: str) -> int:
    code = _extract_code(raw_code)
    r = requests.post(TOKEN_URI, data={
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }, timeout=30)
    if not r.ok:
        print(f"토큰 교환 실패({r.status_code}): {r.text}")
        print("code가 만료(10분)됐거나 이미 사용됐을 수 있습니다 — step=url부터 다시.")
        return 1
    tok = r.json()
    if "refresh_token" not in tok:
        print("refresh_token이 없습니다 — step=url부터 다시 실행하세요(prompt=consent 필요).")
        return 1

    # google.oauth2.credentials.Credentials.from_authorized_user_file 호환 포맷
    token_json = json.dumps({
        "token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "token_uri": TOKEN_URI,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": [SCOPE],
        "universe_domain": "googleapis.com",
        "account": "",
    }, ensure_ascii=False)

    # SSOT 루트 폴더 생성/재사용
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    q = (f"name='{FOLDER}' and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false")
    resp = requests.get("https://www.googleapis.com/drive/v3/files",
                        params={"q": q, "fields": "files(id)"},
                        headers=headers, timeout=30)
    resp.raise_for_status()
    files = resp.json().get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        resp = requests.post("https://www.googleapis.com/drive/v3/files",
                             json={"name": FOLDER,
                                   "mimeType": "application/vnd.google-apps.folder"},
                             params={"fields": "id"}, headers=headers, timeout=30)
        resp.raise_for_status()
        folder_id = resp.json()["id"]

    b64 = base64.b64encode(token_json.encode()).decode()
    with open("gdrive_secrets.txt", "w") as fh:
        fh.write("GitHub 저장소 Settings → Secrets and variables → Actions에 등록:\n\n")
        fh.write(f"GDRIVE_ROOT_FOLDER_ID:\n{folder_id}\n\n")
        fh.write(f"GDRIVE_TOKEN_JSON_B64:\n{b64}\n")
    print("✓ 성공 — 결과는 artifact(gdrive-secrets)의 gdrive_secrets.txt에 저장됨.")
    print("  (시크릿 노출 방지를 위해 로그에는 출력하지 않습니다)")
    print(f"✓ SSOT 폴더 ID: {folder_id}")
    return 0


def main() -> int:
    step = (sys.argv[1] if len(sys.argv) > 1 else "url").strip()
    client_id = os.environ.get("GDRIVE_CLIENT_ID", "")
    if not client_id:
        print("GDRIVE_CLIENT_ID 환경변수가 없습니다")
        return 1
    if step == "url":
        return step_url(client_id)
    if step == "token":
        secret = os.environ.get("GDRIVE_CLIENT_SECRET", "")
        raw_code = os.environ.get("GDRIVE_AUTH_CODE", "")
        if not secret:
            print("GDRIVE_CLIENT_SECRET 시크릿이 등록되지 않았습니다")
            return 1
        if not raw_code:
            print("auth_code 입력이 비었습니다")
            return 1
        return step_token(client_id, secret, raw_code)
    print(f"알 수 없는 step: {step} (url | token)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
