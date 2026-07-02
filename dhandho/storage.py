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

import config

_service_cache = None
_folder_cache: dict[str, str] = {}


def _service():
    """Drive API 서비스 (lazy). 토큰 만료 시 refresh."""
    global _service_cache
    if _service_cache is not None:
        return _service_cache
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(
        config.GDRIVE_TOKEN_FILE, scopes=["https://www.googleapis.com/auth/drive.file"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    _service_cache = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service_cache


def _escape(name: str) -> str:
    return name.replace("'", "\\'")


def _find_child(parent_id: str, name: str, folder: bool | None = None) -> str | None:
    q = f"'{parent_id}' in parents and name='{_escape(name)}' and trashed=false"
    if folder is True:
        q += " and mimeType='application/vnd.google-apps.folder'"
    resp = _service().files().list(q=q, fields="files(id,mimeType)",
                                   pageSize=5).execute()
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
            found = _service().files().create(body=meta, fields="id").execute()["id"]
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
        _service().files().update(fileId=existing, media_body=media).execute()
        return existing
    meta = {"name": name, "parents": [parent]}
    return _service().files().create(body=meta, media_body=media,
                                     fields="id").execute()["id"]


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
        _, done = downloader.next_chunk()
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
        resp = _service().files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken,files(id,name,mimeType)",
            pageSize=200, pageToken=page_token).execute()
        for f in resp.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                continue
            local_path = os.path.join(target, f["name"])
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, _service().files().get_media(fileId=f["id"]))
            done = False
            while not done:
                _, done = dl.next_chunk()
            with open(local_path, "wb") as fh:
                fh.write(buf.getvalue())
            paths.append(local_path)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return paths
