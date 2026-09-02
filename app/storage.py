"""CV file storage backends.

  gdrive : Google Drive — TWEEZ-CV-BANK folder, one "<Role title> CVs" subfolder per role (the default)
  s3     : any S3-compatible bucket (Supabase Storage, DigitalOcean Spaces, AWS S3, Cloudflare R2, MinIO)
  local  : local disk (development)

All backends implement:
  put(key, data, content_type=None, display_name=None, role=None) -> {"key": <stored key>, "url": <human link or None>}
  get(key) -> bytes
  delete(key)
  url(key, expires=600) -> link or None
"""
import io
import json
import logging
import threading
from pathlib import Path

from flask import current_app

log = logging.getLogger(__name__)


class _BlockOauth2ClientFinder:
    """Stop googleapiclient from importing abandoned oauth2client.

    oauth2client pulls pyOpenSSL, which crashes on current cryptography
    (AttributeError: module 'lib' has no attribute 'GEN_EMAIL'). This app
    authenticates with google-auth only.
    """

    def find_spec(self, fullname, path, target=None):
        if fullname == "oauth2client" or fullname.startswith("oauth2client."):
            raise ImportError("oauth2client is disabled; using google-auth")
        return None


def _prefer_google_auth():
    import sys

    for name in list(sys.modules):
        if name == "oauth2client" or name.startswith("oauth2client."):
            del sys.modules[name]
    if not any(isinstance(finder, _BlockOauth2ClientFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _BlockOauth2ClientFinder())


_prefer_google_auth()


def _gdrive_configured(cfg):
    if cfg.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return True
    return bool(
        cfg.get("GOOGLE_OAUTH_CLIENT_ID")
        and cfg.get("GOOGLE_OAUTH_CLIENT_SECRET")
        and cfg.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    )


def _safe_ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"


def make_key(role_slug, applicant_public_id, filename):
    prefix = current_app.config["S3_PREFIX"].strip("/")
    return f"{prefix}/{role_slug}/{applicant_public_id}.{_safe_ext(filename)}"


# ----------------------------------------------------------------------------------------- local

class LocalStorage:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("invalid key")
        return p

    def put(self, key, data: bytes, content_type=None, display_name=None, role=None):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"key": key, "url": None}

    def get(self, key) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key):
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def url(self, key, expires=600):
        return None


# -------------------------------------------------------------------------------------------- s3

class S3Storage:
    def __init__(self, bucket, region, endpoint_url, access_key, secret_key, addressing="path", acl=""):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        self.acl = acl
        self.client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": addressing}),
        )

    def put(self, key, data: bytes, content_type=None, display_name=None, role=None):
        extra = {"ACL": self.acl} if self.acl else {}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return {"key": key, "url": None}

    def get(self, key) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def url(self, key, expires=600):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires
        )


# ---------------------------------------------------------------------------------- google drive

FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveStorage:
    """Uploads CVs into the shared CV bank on Google Drive.

    Auth (one of):
      GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_IMPERSONATE_USER  — service account with domain-wide delegation,
          acting as a Workspace user (e.g. mehdi@tweezgroup.com) so files land in *their* Drive quota.
      GOOGLE_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN         — an OAuth token for that user
          (run `python scripts/gdrive_auth.py` once to obtain it).
    A bare service account without impersonation cannot upload into a My Drive folder (no storage quota),
    which is why one of the two user-bound modes is required here.
    """

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self, root_folder_id, service=None, **auth):
        self.root_folder_id = root_folder_id
        self._service = service
        self._auth = auth
        self._folder_cache = {}
        self._lock = threading.Lock()

    # --- auth / client ---
    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            creds = self._credentials()
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _credentials(self):
        a = self._auth
        if a.get("service_account_json"):
            from google.oauth2 import service_account

            raw = a["service_account_json"]
            info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw))
            creds = service_account.Credentials.from_service_account_info(info, scopes=self.SCOPES)
            if a.get("impersonate_user"):
                creds = creds.with_subject(a["impersonate_user"])
            return creds
        if a.get("refresh_token"):
            from google.oauth2.credentials import Credentials

            return Credentials(
                None,
                refresh_token=a["refresh_token"],
                client_id=a["client_id"],
                client_secret=a["client_secret"],
                token_uri="https://oauth2.googleapis.com/token",
                scopes=self.SCOPES,
            )
        raise RuntimeError("Google Drive storage: set GOOGLE_SERVICE_ACCOUNT_JSON (+GOOGLE_IMPERSONATE_USER) "
                           "or GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN")

    # --- folders ---
    def list_subfolders(self, max_age=600):
        """Live list of TWEEZ-CV-BANK subfolders [(id, name)], cached for `max_age` seconds."""
        import time
        with self._lock:
            cached = self._folder_cache.get("__list__")
            if cached and time.time() - cached[0] < max_age:
                return cached[1]
            q = f"'{self.root_folder_id}' in parents and mimeType='{FOLDER_MIME}' and trashed=false"
            out, token = [], None
            while True:
                res = self.service.files().list(q=q, fields="nextPageToken, files(id,name)", pageSize=100,
                                                pageToken=token, supportsAllDrives=True,
                                                includeItemsFromAllDrives=True).execute()
                out += [(f["id"], f["name"]) for f in res.get("files", [])]
                token = res.get("nextPageToken")
                if not token:
                    break
            self._folder_cache["__list__"] = (time.time(), out)
            return out

    def folder_for_role(self, role):
        """Designated Drive folder for a role:
        1. the folder pinned on the role (drive_folder_id), else
        2. the best-matching existing subfolder of TWEEZ-CV-BANK by name ('Head of Accounting' -> 'Head Acountant CVs'), else
        3. a new '<Role title> CVs' subfolder."""
        from .naming import best_folder

        if role is not None and getattr(role, "drive_folder_id", None):
            return role.drive_folder_id
        title = role.title if role is not None else "Unsorted"
        folders = self.list_subfolders()
        match = best_folder(title, folders)
        if match:
            log.info("Drive: role %r -> existing folder %r", title, match[1])
            return match[0]
        name = f"{title} CVs"
        meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [self.root_folder_id]}
        fid = self.service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]
        log.info("Drive: created folder %r (%s)", name, fid)
        with self._lock:
            self._folder_cache.pop("__list__", None)
        return fid

    # --- files ---
    def put(self, key, data: bytes, content_type=None, display_name=None, role=None):
        from googleapiclient.http import MediaIoBaseUpload

        folder_id = self.folder_for_role(role)
        name = display_name or key.rsplit("/", 1)[-1]
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=content_type or "application/octet-stream",
                                  resumable=False)
        meta = {"name": name, "parents": [folder_id]}
        f = self.service.files().create(body=meta, media_body=media, fields="id,webViewLink",
                                        supportsAllDrives=True).execute()
        return {"key": f["id"], "url": f.get("webViewLink")}

    def get(self, key) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        buf = io.BytesIO()
        req = self.service.files().get_media(fileId=key, supportsAllDrives=True)
        dl = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()

    def delete(self, key):
        try:
            self.service.files().delete(fileId=key, supportsAllDrives=True).execute()
        except Exception as exc:  # already gone / no permission: log, don't block the purge
            log.warning("Drive delete failed for %s: %s", key, exc)

    def url(self, key, expires=600):
        return f"https://drive.google.com/file/d/{key}/view"


# ---------------------------------------------------------------------------------------- factory

def get_storage():
    cfg = current_app.config
    if "_storage" not in current_app.extensions:
        backend = cfg["STORAGE_BACKEND"]
        if backend == "s3":
            store = S3Storage(
                cfg["S3_BUCKET"], cfg["S3_REGION"], cfg["S3_ENDPOINT_URL"],
                cfg["S3_ACCESS_KEY"], cfg["S3_SECRET_KEY"],
                addressing=cfg["S3_ADDRESSING_STYLE"], acl=cfg["S3_ACL"],
            )
        elif backend == "gdrive":
            if not _gdrive_configured(cfg):
                log.warning(
                    "STORAGE_BACKEND=gdrive but Google credentials are empty "
                    "(set GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN or "
                    "GOOGLE_SERVICE_ACCOUNT_JSON). CVs will be stored locally in %s. "
                    "Run `python scripts/gdrive_auth.py client_secret.json` to enable Drive.",
                    cfg["LOCAL_UPLOAD_DIR"],
                )
                store = LocalStorage(cfg["LOCAL_UPLOAD_DIR"])
            else:
                store = GoogleDriveStorage(
                    cfg["GDRIVE_ROOT_FOLDER_ID"],
                    service_account_json=cfg["GOOGLE_SERVICE_ACCOUNT_JSON"],
                    impersonate_user=cfg["GOOGLE_IMPERSONATE_USER"],
                    client_id=cfg["GOOGLE_OAUTH_CLIENT_ID"],
                    client_secret=cfg["GOOGLE_OAUTH_CLIENT_SECRET"],
                    refresh_token=cfg["GOOGLE_OAUTH_REFRESH_TOKEN"],
                )
        else:
            store = LocalStorage(cfg["LOCAL_UPLOAD_DIR"])
        current_app.extensions["_storage"] = store
    return current_app.extensions["_storage"]
