#!/usr/bin/env python3
"""מנהל העוזרים (assistant-manager) - stdlib only, runs as root.

* POST /intake            customization requests from hosted tenant bots (per-tenant token in /opt/agents/<slug>/.manager_token)
* health watcher          every 5 min: /opt/agents/_status/*.json -> WhatsApp alerts to the operator on transitions only
* admin page              https://start.bbbai.online/manage/ (Caddy basic_auth + secret header) on AM_BIND_PROXY:AM_PORT
* JSON API (127.0.0.1)    GET /api/summary, GET /api/requests, POST /api/requests/<id>/done|reopen, POST /api/invite
                          (header X-Manager-Token = /opt/agents/_manager/api_token)
Never reads any bot's messages, mail, memories or keys: only registry, health status files and BOT_NAME/OWNER_NAME.
"""
import os, re, sys, json, time, hmac, html, secrets, sqlite3, hashlib, threading, subprocess, pwd, shutil
import urllib.request, urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
AGENTS = E("AM_AGENTS_ROOT", "/opt/agents")
DATA = E("AM_DATA", os.path.join(AGENTS, "_manager"))
STATUS_DIR = os.path.join(AGENTS, "_status")
REG = os.path.join(AGENTS, "registry.json")
ROOT_ENV = E("AM_ROOT_ENV", "/root/.env")
ROOT_DB = E("AM_ROOT_DB", "/root/memory.db")
AGENTSCTL = E("AM_AGENTSCTL", "/usr/local/sbin/agentsctl")
BIND_PROXY = E("AM_BIND_PROXY", "192.0.2.1")     # documentation placeholder; set to the docker bridge. Admin page only, needs X-Manager-Proxy
BIND_LOCAL = E("AM_BIND_LOCAL", "127.0.0.1")      # intake (tenants) + JSON API (token)
PORT = int(E("AM_PORT", "8850"))
PREFIX = E("AM_PREFIX", "/manage").rstrip("/")
ORIGIN = E("AM_PUBLIC_ORIGIN", "https://start.bbbai.online")
DRY = E("AM_DRY_RUN", "0") == "1"                 # 1 = never send WhatsApp; write to DATA/outbox.log
TICK_S = int(E("AM_TICK_S", "300"))
QUIET_FROM, QUIET_TO = int(E("AM_QUIET_FROM", "23")), int(E("AM_QUIET_TO", "7"))
CHOWN = E("AM_CHOWN", "1") == "1"                 # chown tenant token files to agent-<slug> (off in local tests)
PREFIX_MSG = "מנהל העוזרים: "
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
REQ_MAX_CHARS, REQ_PER_DAY = 400, 10
STALL_H = 24

REQ_F, STATE_F = os.path.join(DATA, "requests.json"), os.path.join(DATA, "state.json")
API_TOKEN_F, PROXY_SECRET_F = os.path.join(DATA, "api_token"), os.path.join(DATA, "proxy_secret")
_lock = threading.RLock()
os.umask(0o077)

def log(*a): print("[MGR]", *a, flush=True)
def now(): return datetime.now()
def today(): return now().strftime("%Y-%m-%d")

# ---------------- files ----------------
def _write_json(path, obj, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, mode); os.replace(tmp, path)
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default
def _secret_file(path):
    try:
        v = open(path).read().strip()
        if len(v) >= 24: return v
    except FileNotFoundError: pass
    v = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: f.write(v + "\n")
    return v
def init_data():
    os.makedirs(DATA, exist_ok=True); os.chmod(DATA, 0o700)
    for p, d in ((REQ_F, {"next_id": 1, "items": []}), (STATE_F, {})):
        if not os.path.exists(p): _write_json(p, d)
    for p in os.listdir(DATA):
        try: os.chmod(os.path.join(DATA, p), 0o600)
        except Exception: pass
    return _secret_file(API_TOKEN_F), _secret_file(PROXY_SECRET_F)

def load_reqs(): return _read_json(REQ_F, {"next_id": 1, "items": []})
def save_reqs(d): _write_json(REQ_F, d)
def load_state(): return _read_json(STATE_F, {})
def save_state(d): _write_json(STATE_F, d)

# ---------------- bots (registry / status / names) ----------------
def registry():
    r = _read_json(REG, {})
    return {s: v for s, v in r.items() if isinstance(v, dict) and SLUG_RE.match(s)} if isinstance(r, dict) else {}

def env_names(slug):
    """Only BOT_NAME and OWNER_NAME from the tenant .env; nothing else is ever read or kept."""
    out = {"BOT_NAME": "", "OWNER_NAME": ""}
    try:
        with open(os.path.join(AGENTS, slug, ".env"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip() in out: out[k.strip()] = v.strip().strip('"').strip("'")[:40]
    except Exception: pass
    return out

EXTRA_KEYS = ("openai_error", "openai_error_at", "google_error", "google_error_at", "updated_at")
def bot_status(slug, idx):
    b = dict(((idx or {}).get("bots") or {}).get(slug) or _read_json(os.path.join(STATUS_DIR, slug + ".json"), {}) or {})
    raw = _read_json(os.path.join(AGENTS, slug, "status.json"), {})   # health-only file written by the bot
    if isinstance(raw, dict):
        for k in EXTRA_KEYS:
            if k in raw and k not in b: b[k] = raw.get(k)
    return b

def status_index(): return _read_json(os.path.join(STATUS_DIR, "index.json"), None)

def _age_min(iso):
    try: return (now() - datetime.fromisoformat(str(iso))).total_seconds() / 60
    except Exception: return None

def label(slug, names=None):
    n = names or env_names(slug)
    bot = n["BOT_NAME"] or slug
    return f"«{bot}»" + (f" של {n['OWNER_NAME']}" if n["OWNER_NAME"] else "") + f" ({slug})"

# ---------------- tokens for tenants ----------------
def token_path(slug): return os.path.join(AGENTS, slug, ".manager_token")
def provision_tokens(reg=None):
    made = []
    for slug, v in (reg or registry()).items():
        home = os.path.join(AGENTS, slug)
        if not os.path.isdir(home): continue
        p = token_path(slug)
        if os.path.exists(p) and os.path.getsize(p) > 20: continue
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f: f.write(secrets.token_urlsafe(32) + "\n")
        os.chmod(p, 0o600)
        if CHOWN:
            try: u = pwd.getpwnam(v.get("user") or f"agent-{slug}"); os.chown(p, u.pw_uid, u.pw_gid)
            except Exception as e: log(f"chown token {slug}: {type(e).__name__}")
        made.append(slug)
    if made: log("tenant tokens created:", ",".join(made))
    return made

def tenant_token_ok(slug, tok):
    if not (slug and SLUG_RE.match(slug) and tok and slug in registry()): return False
    try: real = open(token_path(slug)).read().strip()
    except Exception: return False
    return len(real) > 20 and hmac.compare_digest(real.encode(), tok.strip().encode())

# ---------------- WhatsApp to the operator (via the primary assistant's bridge) ----------------
def _root_env():
    d = {}
    try:
        for line in open(ROOT_ENV, encoding="utf-8"):
            k, _, v = line.partition("=")
            k = k.strip()
            if k in ("WA_BRIDGE_URL", "WA_BRIDGE_TOKEN", "WA_ADMIN_JID", "WA_OWNER_PHONE"): d[k] = v.strip().strip('"').strip("'")
    except Exception: pass
    return d
def _admin_jid(env):
    try:
        c = sqlite3.connect(f"file:{ROOT_DB}?mode=ro", uri=True, timeout=3)
        try:
            r = c.execute("SELECT v FROM kv WHERE k='wa_admin_dm_jid'").fetchone()
            if r and str(r[0]).strip(): return str(r[0]).strip()
        finally: c.close()
    except Exception: pass
    if env.get("WA_ADMIN_JID"): return env["WA_ADMIN_JID"]
    ph = re.sub(r"\D", "", env.get("WA_OWNER_PHONE", ""))
    return f"{ph}@s.whatsapp.net" if ph else ""

def wa_send_now(text):
    msg = PREFIX_MSG + text[:3500]
    if DRY:
        with open(os.path.join(DATA, "outbox.log"), "a", encoding="utf-8") as f: f.write(f"[{now().isoformat(timespec='seconds')}] {msg}\n---\n")
        log("DRY-RUN wa:", msg[:120].replace("\n", " | ")); return True
    env = _root_env(); jid = _admin_jid(env)
    if not (env.get("WA_BRIDGE_URL") and env.get("WA_BRIDGE_TOKEN") and jid):
        log("wa: bridge/jid not configured"); return False
    try:
        req = urllib.request.Request(env["WA_BRIDGE_URL"].rstrip("/") + "/send/text", method="POST",
                                     data=json.dumps({"chat_id": jid, "text": msg}).encode("utf-8"),
                                     headers={"content-type": "application/json", "x-wa-token": env["WA_BRIDGE_TOKEN"]})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(2000)
            try:
                if json.loads(body or b"{}").get("ok") is False: log("wa: bridge said not ok"); return False
            except Exception: pass
        log(f"wa sent ({len(msg)} chars)"); return True
    except Exception as e:
        log(f"wa send failed: {type(e).__name__}"); return False

def _quiet(): h = now().hour; return h >= QUIET_FROM or h < QUIET_TO
def notify(text, st=None, urgent=False):
    """Queue during quiet hours / on failure; flushed by the watcher."""
    with _lock:
        own = st is None
        st = load_state() if own else st
        if (_quiet() and not urgent) or not wa_send_now(text):
            st["pending"] = (st.get("pending") or [])[-19:] + [text]
        if own: save_state(st)
def flush_pending(st):
    p = st.get("pending") or []
    if not p or _quiet(): return
    text = p[0] if len(p) == 1 else "עדכונים על הבוטים:\n" + "\n".join("• " + x for x in p)
    if wa_send_now(text): st["pending"] = []

# ---------------- requests ----------------
def add_request(slug, text):
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text or "")).strip()[:REQ_MAX_CHARS]
    if len(text) < 3: return None, "summary too short"
    n = env_names(slug)
    with _lock:
        d = load_reqs()
        todays = [r for r in d["items"] if r["slug"] == slug and r["created"].startswith(today())]
        if len(todays) >= REQ_PER_DAY: return None, "daily limit"
        rid = int(d.get("next_id") or 1)
        item = {"id": rid, "slug": slug, "bot_name": n["BOT_NAME"], "owner_name": n["OWNER_NAME"], "text": text,
                "created": now().isoformat(timespec="seconds"), "status": "open", "done_at": None}
        d["items"].append(item); d["next_id"] = rid + 1; save_reqs(d)
    notify(f"📝 בקשה חדשה #{rid} מ{label(slug, n)}:\n{text}\nלסימון: «סמן בקשה {rid} כבוצעה»")
    log(f"request #{rid} from {slug} ({len(text)} chars)")
    return item, None

def set_status(rid, status):
    with _lock:
        d = load_reqs()
        for r in d["items"]:
            if r["id"] == rid:
                r["status"] = status; r["done_at"] = now().isoformat(timespec="seconds") if status == "done" else None
                save_reqs(d); return r
    return None

def list_requests(status="all"):
    items = load_reqs()["items"]
    if status in ("open", "done"): items = [r for r in items if r["status"] == status]
    return sorted(items, key=lambda r: (r["status"] != "open", -r["id"]))

# ---------------- invites ----------------
def new_invite():
    try:
        p = subprocess.run([AGENTSCTL, "invite", "--count", "1"], capture_output=True, text=True, timeout=30)
    except Exception as e: return None, f"{type(e).__name__}"
    codes = [l.strip() for l in (p.stdout or "").splitlines() if re.fullmatch(r"[A-Za-z0-9]{6,40}", l.strip())]
    if p.returncode != 0 or not codes: return None, (p.stderr or p.stdout or "agentsctl failed").strip()[:200]
    with _lock:
        st = load_state(); st.setdefault("invites", []).append({"at": now().isoformat(timespec="seconds"), "tail": codes[-1][-3:]})
        st["invites"] = st["invites"][-50:]; save_state(st)
    log("invite created"); return codes[-1], None

# ---------------- summary ----------------
def summary():
    idx = status_index(); reg = registry(); reqs = load_reqs()["items"]
    bots = []
    for slug, v in sorted(reg.items()):
        b = bot_status(slug, idx); n = env_names(slug)
        bots.append({"slug": slug, "bot_name": n["BOT_NAME"], "owner_name": n["OWNER_NAME"], "created": v.get("created"),
                     "up": bool(b.get("up")), "bot_unit": b.get("bot_unit"), "bridge_unit": b.get("bridge_unit"),
                     "whatsapp_connected": b.get("whatsapp_connected"), "google_connected": b.get("google_connected"),
                     "openai_key_set": b.get("openai_key_set"), "openai_error": b.get("openai_error") or "",
                     "google_error": b.get("google_error") or "",
                     "cost_today_usd": b.get("cost_today_usd"), "cost_month_usd": b.get("cost_month_usd"),
                     "heartbeat_age_s": b.get("heartbeat_age_s"),
                     "open_requests": sum(1 for r in reqs if r["slug"] == slug and r["status"] == "open")})
    age = _age_min((idx or {}).get("checked_at"))
    return {"ok": True, "generated_at": now().isoformat(timespec="seconds"), "status_age_min": round(age, 1) if age is not None else None,
            "bots": bots, "capacity": (idx or {}).get("capacity") or {},
            "requests_open": sum(1 for r in reqs if r["status"] == "open"), "requests_total": len(reqs)}

# ---------------- health watcher (transitions only, daily dedupe) ----------------
def _alert(st, slug, kind, text, sent):
    key = f"{slug}:{kind}:{today()}"
    if key in st.setdefault("sent_keys", {}): return
    st["sent_keys"][key] = now().isoformat(timespec="seconds")
    sent.append(text)

def health_tick():
    with _lock:
        st = load_state(); h = st.setdefault("bots", {}); sent = []
        baseline = not st.get("baseline_done")
        reg = registry()
        try: provision_tokens(reg)
        except Exception as e: log(f"provision: {type(e).__name__}")
        idx = status_index()
        if idx is None:
            if reg: _alert(st, "_host", "noindex", "⚠️ אין קובץ מצב בוטים (/opt/agents/_status/index.json). assistant-status.timer לא רץ?", sent)
        else:
            age = _age_min(idx.get("checked_at"))
            if age is None or age > 15:
                if not st.get("stale"): st["stale"] = True; _alert(st, "_host", "stale", "⚠️ בדיקת המצב של הבוטים לא רצה כבר יותר מרבע שעה (assistant-status.timer). אין לי נתונים עדכניים.", sent)
            else:
                if st.get("stale"): st["stale"] = False
                for slug, v in sorted(reg.items()):
                    _bot_tick(slug, v, bot_status(slug, idx), h.setdefault(slug, {}), st, sent, baseline)
        for slug in [s for s in h if s not in reg]: h.pop(slug, None)
        cut = (now() - timedelta(days=14)).strftime("%Y-%m-%d")
        st["sent_keys"] = {k: v for k, v in (st.get("sent_keys") or {}).items() if k.rsplit(":", 1)[-1] >= cut}
        if idx is not None and not st.get("stale"): st["baseline_done"] = True
        st["last_tick"] = now().isoformat(timespec="seconds")
        if baseline and sent and idx is not None: log(f"baseline: {len(sent)} existing condition(s) recorded silently"); sent = []
        for t in sent: st["pending"] = (st.get("pending") or [])[-19:] + [t]
        flush_pending(st)
        save_state(st)
        return sent

def _bot_tick(slug, v, b, s, st, sent, baseline):
    L = label(slug); up = bool(b.get("up"))
    if up:
        if s.get("down_alerted"): s["down_alerted"] = False; _alert(st, slug, "up", f"🟢 {L} חזר לפעול.", sent)
        s["seen_up"] = True; s["down_n"] = 0
        wa = b.get("whatsapp_connected")
        if wa is True:
            if s.get("wa_alerted"): s["wa_alerted"] = False; _alert(st, slug, "wa_back", f"✅ {L}: וואטסאפ חובר מחדש.", sent)
            s["wa_seen"] = True; s["wa_n"] = 0
        elif wa is False and s.get("wa_seen"):
            s["wa_n"] = s.get("wa_n", 0) + 1
            if s["wa_n"] >= 2 and not s.get("wa_alerted"):
                s["wa_alerted"] = True
                _alert(st, slug, "wa_down", f"📵 {L}: הוואטסאפ התנתק. החבר צריך לחבר מחדש בדף ההגדרות (קוד חיבור / QR).", sent)
        g, ge = b.get("google_connected"), str(b.get("google_error") or "")
        if g is True and not ge: s["g_seen"] = True
        g_bad = bool(ge) or (g is False and s.get("g_seen"))
        if g_bad and not s.get("g_alerted"):
            s["g_alerted"] = True
            _alert(st, slug, "google", f"🔑 {L}: החיבור לגוגל {'פג' if ge else 'נותק'} (יומן/מייל לא יעבדו). החבר צריך לחבר מחדש את Google בדף ההגדרות.", sent)
        elif not g_bad and s.get("g_alerted") and g is True:
            s["g_alerted"] = False; _alert(st, slug, "google_back", f"✅ {L}: גוגל חובר מחדש.", sent)
        oe, ks = str(b.get("openai_error") or ""), b.get("openai_key_set")
        if ks is True: s["oa_seen"] = True
        oa_bad = oe in ("auth", "quota", "ceiling") or (ks is False and s.get("oa_seen"))
        if oa_bad and s.get("oa_alerted") != (oe or "nokey"):
            s["oa_alerted"] = oe or "nokey"
            why = {"auth": "המפתח נדחה (בוטל או שגוי)", "quota": "נגמר הקרדיט בחשבון OpenAI", "ceiling": "הגיע לתקרת העלות החודשית שהוגדרה",
                   "nokey": "חסר מפתח"}.get(oe or "nokey", oe)
            _alert(st, slug, "openai_" + (oe or "nokey"), f"💳 {L}: OpenAI לא עובד, {why}. הבוט לא יענה עד שזה יטופל.", sent)
        elif not oa_bad and s.get("oa_alerted"):
            s["oa_alerted"] = ""; _alert(st, slug, "openai_back", f"✅ {L}: OpenAI עובד שוב.", sent)
        return
    if s.get("seen_up"):
        s["down_n"] = s.get("down_n", 0) + 1
        if s["down_n"] >= 2 and not s.get("down_alerted"):   # ~10 minutes: ignores a quick restart
            s["down_alerted"] = True
            _alert(st, slug, "down", f"🔴 {L} הפסיק לפעול (בוט {b.get('bot_unit')}, וואטסאפ-גשר {b.get('bridge_unit')}). "
                                     f"לבדיקה: journalctl -u assistant-bot@{slug} -n 50", sent)
        return
    try: created = datetime.strptime(str(v.get("created")), "%Y-%m-%d %H:%M:%S")
    except Exception: return
    if now() - created > timedelta(hours=STALL_H):
        last = s.get("stall_alerted_at")
        if not last or now() - datetime.fromisoformat(last) > timedelta(days=7):   # once, then a weekly reminder
            s["stall_alerted_at"] = now().isoformat(timespec="seconds")
            hrs = int((now() - created).total_seconds() // 3600)
            _alert(st, slug, "stalled", f"⏳ {L}: ההקמה נתקעה. נוצר לפני {hrs} שעות ועדיין לא עלה אף פעם. כדאי לבדוק עם החבר באיזה שלב הוא עצר.", sent)

def watcher_loop():
    time.sleep(int(E("AM_FIRST_TICK_S", "20")))
    while True:
        try: health_tick()
        except Exception as e: log(f"tick: {type(e).__name__}: {e}")
        time.sleep(TICK_S)

# ---------------- admin page ----------------
CSS = """body{font-family:system-ui,-apple-system,'Segoe UI',Arial,sans-serif;margin:0;background:#f4f6f8;color:#1b1f24}
header{background:#1f3b57;color:#fff;padding:14px 16px}header h1{margin:0;font-size:20px}header small{opacity:.8}
main{max-width:760px;margin:0 auto;padding:12px}section{background:#fff;border-radius:12px;padding:12px 14px;margin:12px 0;box-shadow:0 1px 3px #0001}
h2{font-size:17px;margin:4px 0 10px}.bot,.req{border-top:1px solid #eef0f2;padding:10px 0}.bot:first-of-type,.req:first-of-type{border-top:0}
.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.pill{font-size:13px;padding:2px 8px;border-radius:999px;background:#eef1f4}
.ok{background:#e3f6e8;color:#14662f}.bad{background:#fde7e7;color:#9b1c1c}.mid{background:#fff4d6;color:#7a5300}
.name{font-weight:600}.muted{color:#68707a;font-size:13px}.txt{white-space:pre-wrap;margin:6px 0}
button{font:inherit;border:0;border-radius:10px;padding:10px 14px;background:#1f6feb;color:#fff;cursor:pointer}
button.sec{background:#e9edf2;color:#1b1f24}.code{font-size:28px;letter-spacing:2px;font-family:monospace;background:#f0f4ff;padding:10px;border-radius:10px;text-align:center;direction:ltr}
details summary{cursor:pointer;color:#1f6feb}form{display:inline}"""

def _e(x): return html.escape(str(x if x is not None else ""))
def _yn(v, ok="מחובר", bad="מנותק", none="לא ידוע"):
    if v is True: return f'<span class="pill ok">{ok}</span>'
    if v is False: return f'<span class="pill bad">{bad}</span>'
    return f'<span class="pill">{none}</span>'

def render_page(csrf, invite_code=None, flash=""):
    s = summary(); P = PREFIX + "/"
    out = [f'<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta name="robots" content="noindex"><title>מנהל העוזרים</title><style>{CSS}</style></head><body>'
           f'<header><h1>מנהל העוזרים</h1><small>עודכן {_e(s["generated_at"][11:16])}'
           + (f' · בדיקת מצב לפני {s["status_age_min"]:.0f} דק׳' if s["status_age_min"] is not None else ' · אין נתוני מצב')
           + '</small></header><main>']
    if flash: out.append(f'<section><b>{_e(flash)}</b></section>')
    if invite_code:
        msg = f"היי! הנה קוד ההזמנה שלך לעוזר האישי: {invite_code}\nנכנסים לכאן ומתחילים: {ORIGIN}/"
        out.append(f'<section><h2>הזמנה חדשה לחבר</h2><div class="code" id="c">{_e(invite_code)}</div>'
                   f'<p class="muted">הקוד חד-פעמי. שלח לחבר יחד עם הקישור {_e(ORIGIN)}/</p><div class="row">'
                   f'<button onclick="navigator.clipboard.writeText(document.getElementById(\'c\').textContent)">העתק קוד</button>'
                   f'<a href="https://wa.me/?text={urllib.parse.quote(msg)}"><button class="sec" type="button">שלח בוואטסאפ</button></a></div></section>')
    out.append(f'<section><h2>הזמנות</h2><form method="post" action="{P}invite"><input type="hidden" name="csrf" value="{csrf}">'
               f'<button>צור הזמנה לחבר</button></form>')
    cap = s.get("capacity") or {}
    if cap: out.append(f'<p class="muted">יש מקום בשרת לעוד בערך {_e(cap.get("more_bots_fit"))} בוטים.</p>')
    out.append('</section><section><h2>הבוטים</h2>')
    if not s["bots"]: out.append('<p class="muted">אין עדיין בוטים.</p>')
    for b in s["bots"]:
        up = '<span class="pill ok">פועל</span>' if b["up"] else ('<span class="pill bad">לא פועל</span>' if b["bot_unit"] == "active" or b.get("heartbeat_age_s") is not None else '<span class="pill mid">בהקמה / כבוי</span>')
        oa = '<span class="pill bad">OpenAI: ' + _e({"auth": "מפתח נדחה", "quota": "נגמר קרדיט", "ceiling": "תקרת עלות"}.get(b["openai_error"], b["openai_error"])) + '</span>' if b["openai_error"] else _yn(b["openai_key_set"], "OpenAI תקין", "OpenAI חסר", "OpenAI ?")
        cost = f'${float(b["cost_month_usd"] or 0):.2f} החודש' if b.get("cost_month_usd") is not None else ""
        nq = b["open_requests"]; oq = "" if not nq else (" · בקשה פתוחה אחת" if nq == 1 else " · %d בקשות פתוחות" % nq)
        out.append(f'<div class="bot"><div class="row"><span class="name">{_e(b["bot_name"] or b["slug"])}</span>'
                   f'<span class="muted">{_e(b["owner_name"])} · {_e(b["slug"])}</span></div>'
                   f'<div class="row">{up}{_yn(b["whatsapp_connected"], "וואטסאפ מחובר", "וואטסאפ מנותק", "וואטסאפ ?")}'
                   f'{_yn(b["google_connected"] and not b["google_error"], "Google מחובר", "Google לא מחובר", "Google ?") if b["google_connected"] is not None else _yn(None, none="Google ?")}{oa}</div>'
                   f'<div class="muted">נוצר {_e(b["created"])}{" · " + cost if cost else ""}{oq}</div></div>')
    out.append('</section><section><h2>בקשות מחברים</h2>')
    reqs = list_requests("all"); opn = [r for r in reqs if r["status"] == "open"]; done = [r for r in reqs if r["status"] == "done"]
    if not opn: out.append('<p class="muted">אין בקשות פתוחות 🎉</p>')
    for r in opn:
        out.append(f'<div class="req"><div class="row"><span class="name">#{r["id"]} · {_e(r["owner_name"] or r["slug"])}</span>'
                   f'<span class="muted">{_e(r["bot_name"])} · {_e(r["created"][:16].replace("T", " "))}</span></div>'
                   f'<div class="txt">{_e(r["text"])}</div><form method="post" action="{P}done/{r["id"]}"><input type="hidden" name="csrf" value="{csrf}">'
                   f'<button>סמן כבוצע</button></form></div>')
    if done:
        out.append(f'<details><summary>בוצעו ({len(done)})</summary>')
        for r in done[:100]:
            out.append(f'<div class="req"><div class="row"><span class="name">#{r["id"]} · {_e(r["owner_name"] or r["slug"])}</span>'
                       f'<span class="pill ok">בוצע {_e((r.get("done_at") or "")[:10])}</span></div><div class="txt">{_e(r["text"])}</div>'
                       f'<form method="post" action="{P}reopen/{r["id"]}"><input type="hidden" name="csrf" value="{csrf}"><button class="sec">פתח מחדש</button></form></div>')
        out.append('</details>')
    out.append('</section><p class="muted" style="text-align:center">רק מצב ובקשות שהחברים אישרו להעביר. אין כאן גישה לשיחות, מיילים או זיכרונות שלהם.</p></main></body></html>')
    return "".join(out)

# ---------------- HTTP ----------------
API_TOKEN = PROXY_SECRET = ""
def csrf_token(): return hmac.new(PROXY_SECRET.encode(), b"am-csrf-v1", hashlib.sha256).hexdigest()[:32]

class H(BaseHTTPRequestHandler):
    server_version = "assistant-manager"; sys_version = ""
    def log_message(self, fmt, *a): pass
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        b = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(code); self.send_header("content-type", ctype); self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-store"); self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("x-frame-options", "DENY")
            self.send_header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def _body(self, limit=8192):
        n = int(self.headers.get("content-length") or 0)
        if n > limit: raise ValueError("too large")
        return self.rfile.read(n) if n else b""
    @property
    def kind(self): return self.server.kind
    def _proxy_ok(self): return hmac.compare_digest((self.headers.get("x-manager-proxy") or "").encode(), PROXY_SECRET.encode())
    def _api_ok(self): return hmac.compare_digest((self.headers.get("x-manager-token") or "").encode(), API_TOKEN.encode())
    def _path(self): return urllib.parse.urlsplit(self.path).path

    def do_GET(self):
        p = self._path()
        if p == "/healthz": return self._send(200, {"ok": True})
        if self.kind == "proxy":
            if not self._proxy_ok(): return self._send(403, {"ok": False})
            if p in ("/", ""): return self._send(200, render_page(csrf_token()), "text/html; charset=utf-8")
            return self._send(404, {"ok": False})
        if not p.startswith("/api/") or not self._api_ok(): return self._send(401 if p.startswith("/api/") else 404, {"ok": False})
        if p == "/api/summary": return self._send(200, summary())
        if p == "/api/requests":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self._send(200, {"ok": True, "items": list_requests((q.get("status") or ["all"])[0])})
        return self._send(404, {"ok": False})

    def do_POST(self):
        p = self._path()
        try: raw = self._body()
        except Exception: return self._send(413, {"ok": False})
        if p == "/intake" and self.kind == "local": return self._intake(raw)
        if self.kind == "proxy": return self._admin_post(p, raw)
        if not p.startswith("/api/") or not self._api_ok(): return self._send(401, {"ok": False})
        m = re.fullmatch(r"/api/requests/(\d+)/(done|reopen)", p)
        if m:
            r = set_status(int(m.group(1)), "done" if m.group(2) == "done" else "open")
            return self._send(200 if r else 404, {"ok": bool(r), "item": r})
        if p == "/api/invite":
            code, err = new_invite()
            return self._send(200 if code else 500, {"ok": bool(code), "code": code, "signup_url": ORIGIN + "/", "error": err})
        return self._send(404, {"ok": False})

    def _intake(self, raw):
        try: d = json.loads(raw.decode("utf-8") or "{}")
        except Exception: return self._send(400, {"ok": False, "error": "bad json"})
        slug = str(d.get("slug") or "")
        if not tenant_token_ok(slug, self.headers.get("x-manager-token") or ""):
            time.sleep(0.5); log(f"intake rejected (slug={slug[:22]!r})"); return self._send(403, {"ok": False, "error": "unauthorized"})
        item, err = add_request(slug, d.get("summary"))
        if not item: return self._send(429 if err == "daily limit" else 400, {"ok": False, "error": err})
        return self._send(200, {"ok": True, "id": item["id"]})

    def _admin_post(self, p, raw):
        if not self._proxy_ok(): return self._send(403, {"ok": False})
        org = self.headers.get("origin")
        if org and org.rstrip("/") != ORIGIN.rstrip("/"): return self._send(403, {"ok": False, "error": "origin"})
        f = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        if not hmac.compare_digest((f.get("csrf") or [""])[0].encode(), csrf_token().encode()): return self._send(403, {"ok": False, "error": "csrf"})
        m = re.fullmatch(r"/(done|reopen)/(\d+)", p)
        if m:
            set_status(int(m.group(2)), "done" if m.group(1) == "done" else "open")
            return self._send(303, b"", "text/plain", {"location": PREFIX + "/"})
        if p == "/invite":
            code, err = new_invite()
            return self._send(200, render_page(csrf_token(), invite_code=code, flash="" if code else f"יצירת ההזמנה נכשלה: {err}"), "text/html; charset=utf-8")
        return self._send(404, {"ok": False})

class Srv(ThreadingHTTPServer):
    daemon_threads = True; allow_reuse_address = True
    def __init__(self, addr, kind): self.kind = kind; super().__init__(addr, H)

def _serve(host, kind):
    while True:
        try:
            s = Srv((host, PORT), kind); log(f"listening {host}:{PORT} ({kind})"); s.serve_forever()
        except OSError as e:
            log(f"bind {host}:{PORT} failed ({e.strerror}); retry in 15s"); time.sleep(15)

def serve():
    global API_TOKEN, PROXY_SECRET
    API_TOKEN, PROXY_SECRET = init_data()
    log(f"start dry_run={DRY} data={DATA} agents={AGENTS}")
    threading.Thread(target=watcher_loop, daemon=True, name="watcher").start()
    if BIND_PROXY and BIND_PROXY != BIND_LOCAL:
        threading.Thread(target=_serve, args=(BIND_PROXY, "proxy"), daemon=True, name="proxy").start()
    _serve(BIND_LOCAL, "local")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve": serve()
    elif cmd == "provision":
        init_data(); print(json.dumps({"created": provision_tokens()}))
    elif cmd == "tick":
        API_TOKEN, PROXY_SECRET = init_data(); print(json.dumps({"alerts": health_tick()}, ensure_ascii=False))
    elif cmd == "render":
        API_TOKEN, PROXY_SECRET = init_data(); print(render_page(csrf_token()))
    else: sys.exit("usage: assistant_manager.py serve|provision|tick|render")
