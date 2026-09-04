"""TweezCVBank — a live, searchable index of the Google Drive CV bank inside the admin.

The bank is the TWEEZ-CV-BANK folder on Drive (one "<Role> CVs" subfolder per role).
scan() walks it with the same Google credentials the app already uses for uploads and
stores a JSON index in the Setting table, so the page loads instantly and still works
when Drive is unreachable (it shows the last scan).

organize() renames legacy files to the established convention
    <CODE><DDMMYYYY> - <Candidate Name>.<ext>
without ever touching the file contents: it only adds the " - " separator, fixes
underscores/dots and strips CV/resume noise tokens.  Files already conforming, and
files whose name cannot be parsed safely, are left untouched.  A dry run lists the
proposed renames first; nothing is renamed until the recruiter confirms.
"""
import json
import logging
import re
import unicodedata

from flask import current_app

from .models import Setting
from .naming import code_for_title

log = logging.getLogger(__name__)

INDEX_KEY = "cvbank_index"
FOLDER_MIME = "application/vnd.google-apps.folder"

# tokens that are packaging noise, not part of a candidate's name
NOISE = re.compile(r"(?ix)\b(cv|resume|résumé|curriculum\s*vitae|updated?|final|new|copy|copie(\s+de)?|"
                   r"eng?|english|anglais|fr|français|francais|en-fr|fr-en|ats|pdf|docx?)\b")
PAREN_JUNK = re.compile(r"\(\s*\d+\s*\)|\[\s*\d+\s*\]")
UUIDISH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
PREFIX = re.compile(r"^\s*([A-Z]{2,5})\s*[-_ ]*\s*(\d{8})\b[ .:_-]*")


def _drive():
    """The GoogleDriveStorage behind the app's storage backend, or None (local/S3 dev)."""
    from .storage import get_storage
    backend = get_storage()
    return backend if backend.__class__.__name__ == "GoogleDriveStorage" else None


# ---------- scanning ----------

def _list_children(svc, folder_id):
    """One Drive folder's direct (files, subfolder refs)."""
    files, subs, token = [], [], None
    while True:
        res = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink)",
            pageSize=200, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in res.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                subs.append((f["id"], f["name"]))
                continue
            files.append({
                "id": f["id"], "name": f["name"], "mime": f.get("mimeType", ""),
                "size": int(f.get("size") or 0), "created": (f.get("createdTime") or "")[:10],
                "link": f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view",
                "ok": bool(conforms(f["name"])),
            })
        token = res.get("nextPageToken")
        if not token:
            break
    files.sort(key=lambda x: x["created"], reverse=True)
    return files, subs


def scan():
    """Walk the CV bank on Drive — position folders and everything inside them, subfolders
    (TESTS, Portfolios, Job ads…) included — and store a fresh index. Returns the index dict."""
    from .models import utcnow
    drive = _drive()
    if drive is None:
        raise RuntimeError("Google Drive is not configured (STORAGE_BACKEND!=gdrive) — showing the cached index only.")
    svc = drive.service
    folders = []
    for fid, name in sorted(drive.list_subfolders(max_age=0), key=lambda x: x[1].lower()):
        files, sub_refs = _list_children(svc, fid)
        subfolders = []
        for sid, sname in sorted(sub_refs, key=lambda x: x[1].lower()):
            sfiles, _deeper = _list_children(svc, sid)  # one level is what the bank uses
            subfolders.append({"id": sid, "name": sname, "count": len(sfiles), "files": sfiles})
        total = len(files) + sum(s["count"] for s in subfolders)
        folders.append({"id": fid, "name": name, "code": code_for_title(name), "count": len(files),
                        "total": total, "last": (files[0]["created"] if files else ""),
                        "files": files, "subfolders": subfolders})
    index = {"scanned_at": utcnow().isoformat(timespec="seconds") + "Z",
             "root": drive.root_folder_id, "total": sum(f["total"] for f in folders), "folders": folders}
    Setting.put(INDEX_KEY, json.dumps(index))
    return index


def find_folder(index, folder_id):
    for fo in index.get("folders", []):
        if fo["id"] == folder_id:
            return fo
    return None


def search(index, q):
    """Global search across every folder and subfolder; returns [(folder, subfolder_name_or_None, file)]."""
    q = q.lower()
    hits = []
    for fo in index.get("folders", []):
        blob = (fo["name"] + " " + fo["code"]).lower()
        for f in fo["files"]:
            if q in f["name"].lower() or q in blob:
                hits.append((fo, None, f))
        for sub in fo.get("subfolders", []):
            for f in sub["files"]:
                if q in f["name"].lower() or q in blob or q in sub["name"].lower():
                    hits.append((fo, sub["name"], f))
    return hits[:300]


def cached_index():
    raw = Setting.get(INDEX_KEY)
    return json.loads(raw) if raw else None


# ---------- naming ----------

def conforms(name):
    """True when the file already follows `CODEDDMMYYYY - Name.ext`."""
    return re.match(r"^[A-Z]{2,5}\d{8} - .+\.[A-Za-z0-9]+$", name) is not None


def clean_person(text):
    """Best-effort candidate name from the free part of a legacy filename."""
    text = UUIDISH.sub(" ", text)
    text = PAREN_JUNK.sub(" ", text)
    text = re.sub(r"[._]+", " ", text)
    text = NOISE.sub(" ", text)
    text = re.sub(r"\s*-\s*", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)             # years, timestamps, version numbers
    text = re.sub(r"\(\s*\)", " ", text)             # parens emptied by the cleanup
    text = re.sub(r"\s{2,}", " ", text).strip(" -_.()")
    if not text:
        return None
    # Title-case ALL-CAPS words, keep mixed case as typed
    words = [w.capitalize() if w.isupper() or w.islower() else w for w in text.split()]
    out = " ".join(words)
    return out if len(out) >= 2 else None


def proposed_name(name, folder_code):
    """The conforming name for a legacy file, or None to leave it alone."""
    if conforms(name):
        return None
    stem, dot, ext = name.rpartition(".")
    if not dot or len(ext) > 6:
        return None
    m = PREFIX.match(stem)
    if not m:
        return None  # no CODE+DATE prefix — needs a human (or the Drive cleanup) to date it
    code, date = m.group(1), m.group(2)  # keep the code the recruiter wrote, even if it differs from the folder's
    person = clean_person(stem[m.end():])
    if not person:
        return None
    new = f"{code}{date} - {person}.{ext.lower()}"
    return new if new != name else None


def organize(apply=False, limit=500):
    """Dry-run (default) or apply the renames for every non-conforming file in the cached index.
    Returns {"renames": [{id, folder, old, new, applied}], "applied": n}."""
    index = cached_index()
    if not index:
        raise RuntimeError("Scan the CV bank first.")
    drive = _drive()
    out, applied = [], 0
    for folder in index["folders"]:
        for f in folder["files"]:
            new = proposed_name(f["name"], folder.get("code"))
            if not new:
                continue
            row = {"id": f["id"], "folder": folder["name"], "old": f["name"], "new": new, "applied": False}
            if apply and drive is not None:
                try:
                    drive.service.files().update(fileId=f["id"], body={"name": new},
                                                 supportsAllDrives=True).execute()
                    row["applied"] = True
                    applied += 1
                except Exception as exc:  # keep going; report per-file
                    row["error"] = str(exc)
                    log.warning("CV bank rename failed for %s: %s", f["name"], exc)
            out.append(row)
            if len(out) >= limit:
                break
    return {"renames": out, "applied": applied}


def stats(index):
    """Small numbers for the page header."""
    if not index:
        return None
    non = sum(1 for fo in index["folders"] for f in fo["files"] if not f.get("ok"))
    return {"total": index.get("total", 0), "folders": len(index["folders"]),
            "nonconforming": non, "scanned_at": index.get("scanned_at", "")[:16].replace("T", " ")}


def fmt_size(n):
    if not n:
        return "–"
    return f"{n/1048576:.1f} MB" if n >= 104858 else f"{n/1024:.0f} KB"
