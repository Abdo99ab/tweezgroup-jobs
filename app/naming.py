"""Role codes, CV file names and Drive folder matching.

File name rule: <ROLE CODE><DDMMYYYY> - <Candidate name>.<ext>   e.g.  HM31082026 - Sara Benali.pdf
Folder rule:    the role's designated subfolder of TWEEZ-CV-BANK (pinned per role, else best name match,
                else a new "<Role title> CVs" folder).
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# Codes agreed with HR. Keys are normalised titles (see normalize()).
ROLE_CODES = {
    "head of marketing": "HM",
    "tiktok shop manager": "TTS",
    "amazon account manager": "AAM",
    "head of accounting": "HA",
    "head accountant": "HA",
    "customer support": "CS",
    "bookkeeper": "BK",
    "data scientist": "DS",
    "graphic designer": "GD",
    "content creator": "CC",
    "sourcing manager": "SM",
    "brand manager": "BM",
    "b2b manager": "BB",
    "b2b prospector": "PROS",
    "tiktok ai video creator": "VC",
    "chief operation officer": "COO",
    "chief operating officer coo": "COO",
    "chief operation officer coo": "COO",
    "chief operating officer": "COO",
}

# TWEEZ-CV-BANK subfolders as they exist today (id -> name), so known roles are pinned exactly.
KNOWN_FOLDERS = {
    "1q5duBJpVTuDR2OFCBVyBifQ6jX77XPuz": "Head of Marketing CVs",
    "17ZKXQx946Ry36vAxincMUOdr7F5I4c1a": "B2B Prospector",
    "1RlyUChm_FW2qtxe87HYqWFB5oojCD-w_": "TikTok Shop Manager CV's",
    "1SRQapu9Ja-04u-3R9wwIiUS1IOp6ajUb": "B2B Manager CVs",
    "1Wt6uw4whY-QiQ1-g4dIdveUqPSFuxaM6": "Sourcing Manager CVs",
    "11jvFiPyd4qKe9kjRKLezkBpy9skiA161": "Bookkeeper CVs",
    "1NTYNRexu0RVPCmIv4MDBtpD6ZohCI9GQ": "Chief Opertation Officer (COO) Cvs",
    "1IdhOJM-j9pDC4R9S4AoIqiSJRYvHQx24": "Graphic Designers CVs",
    "10vtrDPCMMzxyiYHmkqtlsYy_108695F7": "TikTok AI Video Creators",
    "14Wi0iOLPwejsdrZ-0RNq_LpbMjtobXd6": "Amazon Account Manager",
    "1NhcMYTozOHUTjKoCHyEbSzjTCJjqE2fN": "TikTok LiveShopping",
    "1h9pJl-quHFQp5PRlSKBsqs6l4B5Q-0um": "Data Scientists CVs",
    "1tZCG1ADWVJemOZLf-bd7nfQrqCmort89": "Customer Support CVs",
    "1C2BS0cQzgIHzxq0me_EbgAuowjiEckz0": "Brand Manager CVs",
    "1Z14OdyOCIkgEfe_wGwXGDmjkbo_InovG": "Head Acountant CVs",
}

_STOP = {"cvs", "cv", "cv's", "resume", "resumes", "of", "the", "and", "&"}
_ALIASES = {  # spelling variants seen in folder names -> canonical token
    "acountant": "accountant", "accountants": "accountant", "accounting": "accountant",
    "opertation": "operation", "operations": "operation",
    "designers": "designer", "scientists": "scientist", "creators": "creator", "managers": "manager",
}


def normalize(text):
    """'Chief Opertation Officer (COO) Cvs' -> 'chief operation officer coo'"""
    text = text.lower().replace("'", "")
    tokens = re.findall(r"[a-z0-9]+", text)
    out = []
    for t in tokens:
        t = _ALIASES.get(t, t)
        if t in _STOP:
            continue
        out.append(t)
    return " ".join(out)


def code_for_title(title):
    """Agreed code if the title is known (HM, TTS, AAM, HA, CS, BK, DS, GD, CC, SM, BM, BB, PROS, VC, COO), else initials."""
    norm = normalize(title)
    if norm in ROLE_CODES:
        return ROLE_CODES[norm]
    for key, code in ROLE_CODES.items():  # e.g. "Senior Graphic Designer" contains "graphic designer"
        if key in norm:
            return code
    m = re.search(r"\(([A-Za-z]{2,5})\)", title)  # "Chief Operation Officer (COO)" -> COO
    if m:
        return m.group(1).upper()
    initials = "".join(w[0] for w in norm.split() if w).upper()
    return (initials or "XX")[:4]


def cv_filename(role, full_name, ext, when=None, tz="Europe/Paris", label=None):
    """HM03092026 - Sara Benali.pdf — or with a label: HM03092026 - Sara Benali - Portfolio.pdf"""
    when = when or datetime.now(ZoneInfo(tz))
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
    code = role.code or code_for_title(role.title)
    clean_name = re.sub(r"[^\w \-\.]", "", full_name, flags=re.U).strip() or "Candidate"
    suffix = f" - {label}" if label else ""
    return f"{code}{when:%d%m%Y} - {clean_name}{suffix}.{ext.lower()}"


def _score(role_norm, folder_norm):
    a, b = set(role_norm.split()), set(folder_norm.split())
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    inter = len(a & b)
    return inter / max(len(a), len(b))


def best_folder(role_title, folders):
    """folders: iterable of (id, name). Returns (id, name, score) of the best match or None.
    Requires every word of the role title to appear in the folder name (or vice-versa) and a decent overlap."""
    role_norm = normalize(role_title)
    best = None
    for fid, name in folders:
        s = _score(role_norm, normalize(name))
        if s > 0 and (best is None or s > best[2]):
            best = (fid, name, s)
    if best and best[2] >= 0.6:
        return best
    return None
