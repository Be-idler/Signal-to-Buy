"""저장 백엔드: Google Drive(개인 계정 OAuth) — SSOT Parquet 저장/조회.

- 스코프 drive.file(앱이 만든 파일만, 최소 권한). 서비스 계정 불가(저장 용량 없음).
- token.json(refresh token 포함)은 운영자가 랩탑에서 1회 생성 후 실행 환경에 배치.
- Google Drive는 DuckDB httpfs 원격 직접 쿼리 불가 → 전수 조회는
  sync_prefix_to_local로 LOCAL_CACHE_DIR에 내려받은 뒤 DuckDB 로컬 조회(NCP와 다른 점).

원격 경로 규약: "financials/2025Q4.parquet", "prices/eod_20260701.parquet",
"checkpoints/trigger_a_20260701.json" 등 — 루트 폴더(GDRIVE_ROOT_FOLDER_ID) 하위.
"""
from __future__ import annotations

import io
import json
import os
import socket
import ssl
import threading
import time

import config

_service_cache = None
_folder_cache: dict[str, str] = {}

_RETRY_STATUS = (429, 500, 502, 503, 504)
_HARD_CALL_TIMEOUT_SEC = 90


class _StallTimeout(Exception):
    """request.execute()가 하드 타임아웃 내에 끝나지 않음(네트워크 스톨 의심)."""


def _with_hard_timeout(fn, *args, **kwargs):
    """fn(*args, **kwargs)을 데몬 스레드에서 실행하고 하드 타임아웃으로 감싼다.

    httplib2.Http(timeout=...)만으로는 부족했다 — 미디어 업/다운로드
    (MediaIoBaseUpload/next_chunk) 경로가 소켓 timeout을 항상 존중한다는
    보장이 없어, 실측으로 특정 종목 보강(업로드) 중 수십 분간 멈추는
    현상이 재현됐다. 스레드 join 타임아웃은 전송 계층과 무관하게
    호출부를 반드시 정해진 시간 안에 돌려준다 — 데몬 스레드라 멈춰버린
    호출이 있어도 프로세스 종료를 막지 않는다(다음 재시도는 새 스레드로).
    """
    box: dict = {}

    def _run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:                            # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=_HARD_CALL_TIMEOUT_SEC)
    if th.is_alive():
        raise _StallTimeout(f"Drive API call stalled past {_HARD_CALL_TIMEOUT_SEC}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _retry_call(fn, *args, retries: int = 5, **kwargs):
    """fn(*args, **kwargs)을 하드 타임아웃 + 지수 백오프로 재시도.

    장시간 배치(분기 적재) 중 발생하는 일시 오류에 대비한다:
    - 429/5xx, 403 rate limit → 지수 백오프 재시도
    - SSL/소켓 끊김·스톨 → 재시도
    - 401(토큰 문제) → 서비스 재생성(토큰 refresh) 후 재시도
    비일시적 오류(권한 등)는 즉시 raise.
    """
    global _service_cache
    try:
        from googleapiclient.errors import HttpError
    except ImportError:                          # 테스트 환경(미설치) 폴백
        class HttpError(Exception):
            resp = None
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return _with_hard_timeout(fn, *args, **kwargs)
        except HttpError as e:
            status = e.resp.status if e.resp is not None else None
            reason = str(e).lower()
            if status in _RETRY_STATUS or (status == 403 and "ratelimit" in reason):
                last_err = e
            elif status == 401:
                _service_cache = None               # 토큰 만료 → 서비스 재생성
                last_err = e
            else:
                raise
        except (ssl.SSLError, socket.timeout, ConnectionError, OSError,
                _StallTimeout) as e:
            last_err = e
        time.sleep(min(2.0 * (2 ** attempt), 60.0))
    raise RuntimeError(f"Drive API failed after {retries} retries: {last_err}") from last_err


def _execute(request, retries: int = 5):
    return _retry_call(request.execute, num_retries=2, retries=retries)


_HTTP_TIMEOUT_SEC = 60


def _service():
    """Drive API 서비스 (lazy). 토큰 만료 시 refresh.

    httplib2.Http에 명시적 timeout을 준다 — 기본값(무제한)이면 네트워크가
    연결 후 응답 없이 멈출 때(silent stall) 소켓 읽기가 영원히 블록되어
    _execute()의 재시도 로직이 아예 발동하지 못한다(예외가 나야 재시도함).
    """
    global _service_cache
    if _service_cache is not None:
        return _service_cache
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(
        config.GDRIVE_TOKEN_FILE, scopes=["https://www.googleapis.com/auth/drive.file"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SEC))
    _service_cache = build("drive", "v3", http=http, cache_discovery=False)
    return _service_cache


def _escape(name: str) -> str:
    return name.replace("'", "\\'")


def _find_child(parent_id: str, name: str, folder: bool | None = None) -> str | None:
    q = f"'{parent_id}' in parents and name='{_escape(name)}' and trashed=false"
    if folder is True:
        q += " and mimeType='application/vnd.google-apps.folder'"
    resp = _execute(_service().files().list(q=q, fields="files(id,mimeType)",
                                            pageSize=5))
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _resolve_folder(path_parts: list[str], create: bool = True) -> str | None:
    """루트 하위 폴더 경로 → 폴더 ID (없으면 생성)."""
    parent = config.GDRIVE_ROOT_FOLDER_ID
    key = "/".join(path_parts)
    if key in _folder_cache:
        return _folder_cache[key]
    for part in path_parts:
        found = _find_child(parent, part, folder=True)
        if found is None:
            if not create:
                return None
            meta = {"name": part, "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent]}
            found = _execute(_service().files().create(body=meta, fields="id"))["id"]
        parent = found
    _folder_cache[key] = parent
    return parent


def _split(remote_path: str) -> tuple[list[str], str]:
    parts = remote_path.strip("/").split("/")
    return parts[:-1], parts[-1]


def upload_bytes(data: bytes, remote_path: str,
                 mime: str = "application/octet-stream") -> str:
    """바이트를 원격 경로에 업로드(동명 파일은 새 버전으로 갱신 → 멱등)."""
    from googleapiclient.http import MediaIoBaseUpload
    folders, name = _split(remote_path)
    parent = _resolve_folder(folders) if folders else config.GDRIVE_ROOT_FOLDER_ID
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    existing = _find_child(parent, name)
    if existing:
        _execute(_service().files().update(fileId=existing, media_body=media))
        return existing
    meta = {"name": name, "parents": [parent]}
    return _execute(_service().files().create(body=meta, media_body=media,
                                              fields="id"))["id"]


def download_bytes(remote_path: str) -> bytes | None:
    from googleapiclient.http import MediaIoBaseDownload
    folders, name = _split(remote_path)
    parent = (_resolve_folder(folders, create=False) if folders
              else config.GDRIVE_ROOT_FOLDER_ID)
    if parent is None:
        return None
    fid = _find_child(parent, name)
    if fid is None:
        return None
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, _service().files().get_media(fileId=fid))
    done = False
    while not done:
        _, done = _retry_call(downloader.next_chunk, num_retries=3)
    return buf.getvalue()


def exists(remote_path: str) -> bool:
    folders, name = _split(remote_path)
    parent = (_resolve_folder(folders, create=False) if folders
              else config.GDRIVE_ROOT_FOLDER_ID)
    return parent is not None and _find_child(parent, name) is not None


# ------------------------------------------------------------------ Parquet/JSON 헬퍼

def upload_parquet(df, remote_path: str) -> str:
    import pandas as pd  # noqa: F401  (호출자가 DataFrame을 넘긴다는 계약 명시)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return upload_bytes(buf.getvalue(), remote_path, "application/octet-stream")


def read_parquet(remote_path: str):
    import pandas as pd
    data = download_bytes(remote_path)
    if data is None:
        return None
    return pd.read_parquet(io.BytesIO(data))


def save_json(obj, remote_path: str) -> str:
    return upload_bytes(json.dumps(obj, ensure_ascii=False).encode(),
                        remote_path, "application/json")


def load_json(remote_path: str):
    data = download_bytes(remote_path)
    return json.loads(data.decode()) if data else None


def list_prefix(prefix: str) -> list[str]:
    """원격 폴더(prefix) 하위 파일명 목록(폴더 제외). 없으면 빈 목록.

    스코어카드가 prices/ 스냅샷 일자를 열거해 forward return 창을 잡는 데 쓴다.
    """
    folder_id = _resolve_folder(prefix.strip("/").split("/"), create=False)
    if folder_id is None:
        return []
    names: list[str] = []
    page_token = None
    while True:
        resp = _execute(_service().files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(name,mimeType)",
            pageSize=200, pageToken=page_token))
        for f in resp.get("files", []):
            if f["mimeType"] != "application/vnd.google-apps.folder":
                names.append(f["name"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return names


def auth_status() -> tuple[bool, str | None]:
    """Drive 인증 헬스체크 — (정상?, 진단문구).

    Google OAuth 앱이 Testing 상태면 refresh token이 7일 만에 만료될 수 있어
    (Google 공식), 조용히 죽는 대신 즉시 원인을 짚어 경보하기 위한 프리플라이트.
    토큰 만료/철회(invalid_grant)는 재발급 필요로 명시 분류한다.
    """
    global _service_cache
    _service_cache = None                        # 캐시 우회 — 실제 자격 갱신을 시험
    try:
        # _execute로 감싸 일시 오류(503·타임아웃)는 재시도 — 프리플라이트가 순간
        # 네트워크 blip에 오판해 하루 파이프라인을 통째로 중단시키지 않도록.
        _execute(_service().files().list(pageSize=1, fields="files(id)"))
        return True, None
    except Exception as e:                        # noqa: BLE001
        reason = str(e).lower()
        if any(k in reason for k in ("invalid_grant", "token has been expired",
                                     "refresh", "invalid_scope", "unauthorized")):
            return False, ("Google Drive 인증 만료·철회 추정 — token.json 재발급 필요. "
                           "(앱이 Testing 상태면 refresh token 7일 만료; drive.file "
                           "스코프 앱을 Production 게시하면 만료 방지)")
        return False, f"Google Drive 접근 실패: {str(e)[:200]}"


def sync_prefix_to_local(prefix: str, local_dir: str | None = None) -> list[str]:
    """원격 폴더(prefix) 전체를 LOCAL_CACHE_DIR로 내려받아 로컬 경로 목록 반환.

    트랙2 전수 조회용: 내려받은 뒤 DuckDB로 로컬 parquet을 쿼리한다.
    이미 받은 파일은 (같은 이름이면) 건너뛰지 않고 덮어쓴다 — 정정 반영.
    """
    from googleapiclient.http import MediaIoBaseDownload
    local_dir = local_dir or config.LOCAL_CACHE_DIR
    folder_id = _resolve_folder(prefix.strip("/").split("/"), create=False)
    if folder_id is None:
        return []
    target = os.path.join(local_dir, prefix.strip("/"))
    os.makedirs(target, exist_ok=True)

    paths: list[str] = []
    page_token = None
    while True:
        resp = _execute(_service().files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType)",
            pageSize=200, pageToken=page_token))
        for f in resp.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                continue
            local_path = os.path.join(target, f["name"])
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, _service().files().get_media(fileId=f["id"]))
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=3)
            with open(local_path, "wb") as fh:
                fh.write(buf.getvalue())
            paths.append(local_path)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return paths
