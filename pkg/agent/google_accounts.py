"""Google OAuth helper (Gmail / Calendar / Drive) for the assistant.
Each owner uses HIS OWN Google Cloud OAuth client (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env).
Tokens are stored only on this server: AGENT_HOME/data/google_accounts.json (chmod 600). Never print secrets."""
import os, json, time, hmac, hashlib, base64, secrets, threading, urllib.parse, re
import requests

AGENT_HOME = os.environ.get("AGENT_HOME") or "/opt/agent"
ENV_PATH = os.path.join(AGENT_HOME, ".env")
DATA_DIR = os.path.join(AGENT_HOME, "data")
STORE_PATH = os.environ.get("GOOGLE_ACCOUNTS_PATH") or os.path.join(DATA_DIR, "google_accounts.json")
KEY_PATH = os.path.join(DATA_DIR, "oauth_state.key")
LOOPBACK_REDIRECT = "http://localhost:8765/"   # Desktop client: browser lands here, the owner pastes the URL back

def _load_env():
    if not os.path.exists(ENV_PATH): return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip("\r")
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()

def public_base():
    return (os.environ.get("PUBLIC_BASE_URL", "") or "").rstrip("/")

ACCOUNTS = {
    "personal": {"email": (os.environ.get("GOOGLE_PERSONAL_EMAIL") or None), "title": "חשבון ראשי"},
    "work":     {"email": (os.environ.get("GOOGLE_WORK_EMAIL") or None), "title": "חשבון עבודה (אופציונלי)"},
}
ALIASES = {"personal": "personal", "private": "personal", "אישי": "personal", "פרטי": "personal", "gmail": "personal",
           "family": "personal", "משפחתי": "personal", "main": "personal", "ראשי": "personal",
           "work": "work", "עבודה": "work", "bot": "personal", "בוט": "personal"}
SCOPES = [
    "openid", "email",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
]
_lock = threading.Lock()

def clients():
    """'web' = redirect back to PUBLIC_BASE_URL/google/callback; 'desktop' = loopback + paste (works without a domain)."""
    cid, sec = os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (cid and sec): return {}
    kind = (os.environ.get("GOOGLE_CLIENT_TYPE", "desktop") or "desktop").lower()
    if kind == "web" and public_base():
        return {"web": {"id": cid, "secret": sec, "redirect": public_base() + "/google/callback"}}
    return {"desktop": {"id": cid, "secret": sec, "redirect": LOOPBACK_REDIRECT}}

def preferred_client():
    c = clients()
    return next(iter(c), None)

def resolve(label):
    if not label: return None
    l = str(label).strip().lower()
    for k, v in ACCOUNTS.items():
        if v.get("email") and l == v["email"].lower(): return k
    return ALIASES.get(l, l if l in ACCOUNTS else None)

def load_store():
    try:
        with open(STORE_PATH, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def save_account(label, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        st = load_store(); st[label] = data
        tmp = STORE_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STORE_PATH); os.chmod(STORE_PATH, 0o600)

def disconnect(label):
    with _lock:
        st = load_store(); st.pop(label, None)
        fd = os.open(STORE_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(STORE_PATH + ".tmp", STORE_PATH)

def connected():
    st = load_store()
    return {k: {"email": v.get("email"), "saved_at": v.get("saved_at"), "client": v.get("client"), "verified": v.get("verified")}
            for k, v in st.items() if v.get("refresh_token")}

def get_credentials(label):
    """Credentials for an account label. Falls back to the main connected account when the asked one is not connected."""
    from google.oauth2.credentials import Credentials
    lab = resolve(label) or label
    st = load_store()
    rec = st.get(lab)
    if not (rec and rec.get("refresh_token")):
        rec = st.get("personal") if (st.get("personal") or {}).get("refresh_token") else next((v for v in st.values() if v.get("refresh_token")), None)
    if not rec or not rec.get("refresh_token"):
        raise RuntimeError(f"חשבון Google לא מחובר עדיין. לחיבור: {start_url(lab)}")
    c = clients().get(rec.get("client") or "desktop") or next(iter(clients().values()), None)
    if not c: raise RuntimeError("לא הוגדר OAuth client של Google בשרת (GOOGLE_CLIENT_ID/SECRET)")
    return Credentials(token=None, refresh_token=rec["refresh_token"], client_id=c["id"], client_secret=c["secret"],
                       token_uri="https://oauth2.googleapis.com/token", scopes=rec.get("scopes") or SCOPES)

# ---------- state / PKCE (stateless, HMAC-signed) ----------
def _key():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(KEY_PATH):
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f: f.write(secrets.token_bytes(32))
    with open(KEY_PATH, "rb") as f: return f.read()

def _b64(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")

def make_state(label, client, ttl=86400):
    payload = f"{label}.{client}.{int(time.time()) + ttl}.{secrets.token_hex(8)}"
    sig = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())[:32]
    return f"{payload}.{sig}"

def check_state(state):
    try: label, client, exp, nonce, sig = state.split(".")
    except Exception: raise ValueError("state לא תקין")
    payload = f"{label}.{client}.{exp}.{nonce}"
    good = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())[:32]
    if not hmac.compare_digest(good, sig): raise ValueError("חתימת state לא תקינה")
    if int(exp) < time.time(): raise ValueError("קישור החיבור פג תוקף — צור חדש")
    return label, client

def _verifier(state):
    return _b64(hmac.new(_key(), ("pkce:" + state).encode(), hashlib.sha256).digest())

def auth_url(label="personal", client=None, ttl=86400):
    label = resolve(label) or "personal"
    client = client or preferred_client()
    if not client: raise RuntimeError("חסרים GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
    c = clients()[client]
    state = make_state(label, client, ttl)
    challenge = _b64(hashlib.sha256(_verifier(state).encode()).digest())
    params = {"client_id": c["id"], "redirect_uri": c["redirect"], "response_type": "code",
              "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    em = ACCOUNTS.get(label, {}).get("email")
    if em: params["login_hint"] = em
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

def start_url(label="personal"):
    b = public_base()
    return f"{b}/google/start?account={resolve(label) or 'personal'}" if b else "(דף החיבורים בשרת: /google)"

def exchange(code, state):
    label, client = check_state(state)
    c = clients().get(client)
    if not c: raise ValueError(f"client {client} לא מוגדר")
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": code, "client_id": c["id"], "client_secret": c["secret"], "redirect_uri": c["redirect"],
        "grant_type": "authorization_code", "code_verifier": _verifier(state)})
    j = r.json()
    if r.status_code != 200:
        raise ValueError(f"החלפת קוד נכשלה: {j.get('error')} {j.get('error_description', '')}")
    if not j.get("refresh_token"):
        raise ValueError("Google לא החזיר refresh_token — נסה שוב מהכפתור")
    info = requests.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=20,
                        headers={"Authorization": f"Bearer {j['access_token']}"}).json()
    email = (info.get("email") or "").lower()
    expected = (ACCOUNTS.get(label, {}).get("email") or "").lower()
    if expected and email != expected:
        raise ValueError(f"נכנסת עם {email} אבל החיבור הזה מיועד ל-{expected}. נסה שוב ובחר את החשבון הנכון.")
    granted = (j.get("scope") or "").split()
    rec = {"email": email, "refresh_token": j["refresh_token"], "scopes": granted, "client": client,
           "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_account(label, rec)
    ver = verify(label)
    rec["verified"] = ver
    save_account(label, rec)
    missing = [s for s in SCOPES if s not in granted and s not in ("openid", "email")]
    return label, email, ver, missing

def verify(label):
    """List calendars (proof the token works)."""
    try:
        from googleapiclient.discovery import build
        srv = build("calendar", "v3", credentials=get_credentials(label), cache_discovery=False)
        items = srv.calendarList().list(maxResults=50).execute().get("items", [])
        return {"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "calendars": [i.get("summary") for i in items][:20]}
    except Exception as e:
        return {"ok": False, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": str(e)[:300]}

def exchange_from_pasted_url(text):
    """Owner pasted the http://localhost:8765/?state=..&code=.. URL (Desktop client flow)."""
    m = re.search(r"https?://(?:localhost|127\.0\.0\.1)[^\s]*", text or "")
    if not m: raise ValueError("לא מצאתי כתובת שמתחילה ב-http://localhost")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(m.group(0)).query)
    if q.get("error"): raise ValueError(f"Google החזיר שגיאה: {q['error'][0]}")
    if not q.get("code") or not q.get("state"): raise ValueError("בכתובת חסרים code/state")
    return exchange(q["code"][0], q["state"][0])
