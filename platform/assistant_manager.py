#!/usr/bin/env python3
"""מנהל העוזרים (assistant-manager). Stdlib only.

AM_DATA is required. There is no default data directory.
AM_AGENTS_ROOT defaults to /opt/agents.

* POST /intake            requests from a hosted bot (per-tenant token)
* POST /cost              OpenAI spend for a pilot row (per-tenant token)
* POST /status            health snapshot for a pilot row (per-tenant token)
* health watcher          transition alerts; operator bridge via AM_WA_BRIDGE_URL,
                          AM_WA_BRIDGE_TOKEN, and AM_WA_ADMIN_JID when set
* admin page              proxy bind with X-Manager-Proxy
* JSON API                local bind with X-Manager-Token

Pilot ledger: up to 5 rows. Postpone only moves release_at later.
A row is released when release_at has passed, or when own server, own line, and own key are all set.
Release runs the same delete as an explicit delete: the row, status history, costs, requests, and invite codes.

Host invite lines are appended to <agents root>/_shared/invites.txt.
Signup handling lives outside this tree.

Never reads a bot's messages, mail, memories, or keys.
Line and key slots stay empty until the operator fills them.
"""
import os, re, sys, json, time, hmac, html, secrets, hashlib, threading, pwd
import urllib.request, urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

E = os.environ.get
MAX_PILOTS = 5
RELEASE_DAYS = 30
CAP_USD = Decimal("20.00")
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PREFIX_MSG = "מנהל העוזרים: "
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
CLIENT_RE = re.compile(r"^c[1-9][0-9]{0,5}$")
REQ_MAX_CHARS, REQ_PER_DAY = 400, 10
STALL_H = 24
_lock = threading.RLock()
os.umask(0o077)

AGENTS = DATA = STATUS_DIR = REG = ""
BIND_PROXY = BIND_LOCAL = ""
PORT = 8850
PREFIX = "/manage"
ORIGIN = ""
DRY = False
TICK_S = 300
QUIET_FROM, QUIET_TO = 23, 7
CHOWN = True
REQ_F = STATE_F = API_TOKEN_F = PROXY_SECRET_F = ""
LEDGER_F = INVITES_F = COSTS_F = HISTORY_F = ""

ERR_HE = {
    "label required": "צריך שם תצוגה.",
    "pilot full": "הפיילוט מלא.",
    "only later": "אפשר לדחות רק למועד מאוחר יותר.",
    "missing": "הרשומה לא נמצאה.",
    "bad time": "המועד לא תקין.",
    "bad amount": "הסכום לא תקין.",
    "cap": "החיוב נחסם. התקרה היא 20 דולר.",
    "bad slug": "השם הפנימי לא תקין.",
    "slug used": "השם הפנימי כבר בשימוש.",
    "bad status": "מצב לא תקין.",
    "bad flag": "סימון לא תקין.",
    "invite store": "לא הצלחתי לשמור את קוד ההזמנה.",
}


def log(*a):
    print("[MGR]", *a, flush=True)


def now():
    raw = os.environ.get("AM_NOW", "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now()


def today():
    return now().strftime("%Y-%m-%d")


def _forbidden_path(path):
    raw = os.path.normpath(os.path.abspath(str(path)))
    parts = raw.split(os.sep)
    return len(parts) >= 2 and parts[1] == "root"


def _rejected_path(path):
    if not str(path or "").strip():
        return True
    if _forbidden_path(path):
        return True
    abs_path = os.path.abspath(path)
    if os.path.islink(abs_path):
        target = os.readlink(abs_path)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(abs_path), target)
        if _forbidden_path(target):
            return True
    return _forbidden_path(os.path.realpath(abs_path))


def configure():
    global AGENTS, DATA, STATUS_DIR, REG, BIND_PROXY, BIND_LOCAL, PORT, PREFIX, ORIGIN
    global DRY, TICK_S, QUIET_FROM, QUIET_TO, CHOWN
    global REQ_F, STATE_F, API_TOKEN_F, PROXY_SECRET_F
    global LEDGER_F, INVITES_F, COSTS_F, HISTORY_F
    data = os.environ.get("AM_DATA", "").strip()
    if not data:
        raise SystemExit("AM_DATA is required")
    if _rejected_path(data):
        raise SystemExit("AM_DATA is not allowed")
    agents = os.environ.get("AM_AGENTS_ROOT", "").strip() or "/opt/agents"
    if _rejected_path(agents):
        raise SystemExit("AM_AGENTS_ROOT is not allowed")
    try:
        port = int(os.environ.get("AM_PORT", "").strip() or "8850")
        tick_s = int(os.environ.get("AM_TICK_S", "").strip() or "300")
        quiet_from = int(os.environ.get("AM_QUIET_FROM", "").strip() or "23")
        quiet_to = int(os.environ.get("AM_QUIET_TO", "").strip() or "7")
    except ValueError:
        raise SystemExit("invalid numeric setting")
    DATA = data
    AGENTS = agents
    STATUS_DIR = os.path.join(AGENTS, "_status")
    REG = os.path.join(AGENTS, "registry.json")
    BIND_PROXY = os.environ.get("AM_BIND_PROXY", "").strip() or "192.0.2.1"
    BIND_LOCAL = os.environ.get("AM_BIND_LOCAL", "").strip() or "127.0.0.1"
    PORT = port
    PREFIX = (os.environ.get("AM_PREFIX", "/manage") or "/manage").rstrip("/")
    ORIGIN = os.environ.get("AM_PUBLIC_ORIGIN", "").strip() or "https://start.bbbai.online"
    DRY = os.environ.get("AM_DRY_RUN", "0").strip() == "1"
    TICK_S = tick_s
    QUIET_FROM, QUIET_TO = quiet_from, quiet_to
    CHOWN = os.environ.get("AM_CHOWN", "1").strip() == "1"
    REQ_F = os.path.join(DATA, "requests.json")
    STATE_F = os.path.join(DATA, "state.json")
    API_TOKEN_F = os.path.join(DATA, "api_token")
    PROXY_SECRET_F = os.path.join(DATA, "proxy_secret")
    LEDGER_F = os.path.join(DATA, "ledger.json")
    INVITES_F = os.path.join(DATA, "invites.json")
    COSTS_F = os.path.join(DATA, "costs.json")
    HISTORY_F = os.path.join(DATA, "status_history.json")


def _flash(err):
    return ERR_HE.get(err or "", "הפעולה נכשלה.")


def _write_json(path, obj, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _secret_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        if len(v) >= 24:
            return v
    except FileNotFoundError:
        pass
    v = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(v + "\n")
    return v


def init_data():
    configure()
    os.makedirs(DATA, exist_ok=True)
    os.chmod(DATA, 0o700)
    defaults = (
        (REQ_F, {"next_id": 1, "items": []}),
        (STATE_F, {}),
        (LEDGER_F, {"next_id": 1, "clients": []}),
        (INVITES_F, {"codes": {}}),
        (COSTS_F, {}),
        (HISTORY_F, {}),
    )
    for path, default in defaults:
        if not os.path.exists(path):
            _write_json(path, default)
    for name in os.listdir(DATA):
        fp = os.path.join(DATA, name)
        if os.path.islink(fp) or not os.path.isfile(fp):
            continue
        try:
            os.chmod(fp, 0o600)
        except OSError:
            pass
    return _secret_file(API_TOKEN_F), _secret_file(PROXY_SECRET_F)


def load_reqs():
    return _read_json(REQ_F, {"next_id": 1, "items": []})


def save_reqs(d):
    _write_json(REQ_F, d)


def load_state():
    return _read_json(STATE_F, {})


def save_state(d):
    _write_json(STATE_F, d)


def load_ledger():
    d = _read_json(LEDGER_F, {"next_id": 1, "clients": []})
    if not isinstance(d, dict):
        d = {"next_id": 1, "clients": []}
    if not isinstance(d.get("clients"), list):
        d["clients"] = []
    return d


def save_ledger(d):
    _write_json(LEDGER_F, d)


def load_invites():
    d = _read_json(INVITES_F, {"codes": {}})
    if not isinstance(d, dict):
        d = {"codes": {}}
    if not isinstance(d.get("codes"), dict):
        d["codes"] = {}
    return d


def save_invites(d):
    _write_json(INVITES_F, d)


def load_costs():
    d = _read_json(COSTS_F, {})
    return d if isinstance(d, dict) else {}


def save_costs(d):
    _write_json(COSTS_F, d)


def load_history():
    d = _read_json(HISTORY_F, {})
    return d if isinstance(d, dict) else {}


def save_history(d):
    _write_json(HISTORY_F, d)


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
                if k.strip() in out:
                    out[k.strip()] = v.strip().strip('"').strip("'")[:40]
    except Exception:
        pass
    return out


EXTRA_KEYS = ("openai_error", "openai_error_at", "google_error", "google_error_at", "updated_at")


def bot_status(slug, idx):
    b = dict(((idx or {}).get("bots") or {}).get(slug) or _read_json(os.path.join(STATUS_DIR, slug + ".json"), {}) or {})
    raw = _read_json(os.path.join(AGENTS, slug, "status.json"), {})
    if isinstance(raw, dict):
        for k in EXTRA_KEYS:
            if k in raw and k not in b:
                b[k] = raw.get(k)
    return b


def status_index():
    return _read_json(os.path.join(STATUS_DIR, "index.json"), None)


def _age_min(iso):
    try:
        return (now() - datetime.fromisoformat(str(iso))).total_seconds() / 60
    except Exception:
        return None


def label(slug, names=None):
    n = names or env_names(slug)
    bot = n["BOT_NAME"] or slug
    return f"«{bot}»" + (f" של {n['OWNER_NAME']}" if n["OWNER_NAME"] else "") + f" ({slug})"


def token_path(slug):
    return os.path.join(AGENTS, slug, ".manager_token")


def provision_tokens(reg=None):
    made = []
    for slug, v in (reg or registry()).items():
        home = os.path.join(AGENTS, slug)
        if not os.path.isdir(home):
            continue
        p = token_path(slug)
        if os.path.exists(p) and os.path.getsize(p) > 20:
            continue
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
        os.chmod(p, 0o600)
        if CHOWN:
            try:
                u = pwd.getpwnam(v.get("user") or f"agent-{slug}")
                os.chown(p, u.pw_uid, u.pw_gid)
            except Exception as e:
                log(f"chown token {slug}: {type(e).__name__}")
        made.append(slug)
    if made:
        log("tenant tokens created:", ",".join(made))
    return made


def _digest_eq(a, b):
    a = a if isinstance(a, bytes) else str(a).encode()
    b = b if isinstance(b, bytes) else str(b).encode()
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


def tenant_token_ok(slug, tok):
    if not (slug and SLUG_RE.match(slug) and tok and slug in registry()):
        return False
    try:
        with open(token_path(slug), encoding="utf-8") as f:
            real = f.read().strip()
    except Exception:
        return False
    return len(real) > 20 and _digest_eq(real, tok.strip())


def wa_send_now(text):
    msg = PREFIX_MSG + text[:3500]
    if DRY:
        with open(os.path.join(DATA, "outbox.log"), "a", encoding="utf-8") as f:
            f.write(f"[{now().isoformat(timespec='seconds')}] {msg}\n---\n")
        log("DRY-RUN wa:", msg[:120].replace("\n", " | "))
        return True
    url = E("AM_WA_BRIDGE_URL", "").strip()
    token = E("AM_WA_BRIDGE_TOKEN", "").strip()
    jid = E("AM_WA_ADMIN_JID", "").strip()
    if not (url and token and jid):
        log("wa: bridge not configured")
        return False
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/send/text",
            method="POST",
            data=json.dumps({"chat_id": jid, "text": msg}).encode("utf-8"),
            headers={"content-type": "application/json", "x-wa-token": token},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(2000)
            try:
                if json.loads(body or b"{}").get("ok") is False:
                    log("wa: bridge said not ok")
                    return False
            except Exception:
                pass
        log(f"wa sent ({len(msg)} chars)")
        return True
    except Exception as e:
        log(f"wa send failed: {type(e).__name__}")
        return False


def _quiet():
    h = now().hour
    return h >= QUIET_FROM or h < QUIET_TO


def notify(text, st=None, urgent=False):
    """Queue during quiet hours / on failure; flushed by the watcher."""
    with _lock:
        own = st is None
        st = load_state() if own else st
        if (_quiet() and not urgent) or not wa_send_now(text):
            st["pending"] = (st.get("pending") or [])[-19:] + [text]
        if own:
            save_state(st)


def flush_pending(st):
    p = st.get("pending") or []
    if not p or _quiet():
        return
    text = p[0] if len(p) == 1 else "עדכונים על הבוטים:\n" + "\n".join("• " + x for x in p)
    if wa_send_now(text):
        st["pending"] = []


def _client_for_slug(slug):
    if not slug or not SLUG_RE.fullmatch(slug):
        return None
    for c in load_ledger().get("clients") or []:
        if c.get("slug") == slug:
            return c
    return None


def add_request(slug, text):
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(text or "")).strip()[:REQ_MAX_CHARS]
    if len(text) < 3:
        return None, "summary too short"
    n = env_names(slug)
    with _lock:
        d = load_reqs()
        todays = [r for r in d["items"] if r["slug"] == slug and r["created"].startswith(today())]
        if len(todays) >= REQ_PER_DAY:
            return None, "daily limit"
        rid = int(d.get("next_id") or 1)
        linked = _client_for_slug(slug)
        item = {
            "id": rid, "slug": slug, "bot_name": n["BOT_NAME"], "owner_name": n["OWNER_NAME"], "text": text,
            "created": now().isoformat(timespec="seconds"), "status": "open", "done_at": None,
            "client_id": (linked or {}).get("id") or "",
        }
        d["items"].append(item)
        d["next_id"] = rid + 1
        save_reqs(d)
    notify(f"📝 בקשה חדשה #{rid} מ{label(slug, n)}:\n{text}\nלסימון: «סמן בקשה {rid} כבוצעה»")
    log(f"request #{rid} from {slug} ({len(text)} chars)")
    return item, None


def set_status(rid, status):
    with _lock:
        d = load_reqs()
        for r in d["items"]:
            if r["id"] == rid:
                r["status"] = status
                r["done_at"] = now().isoformat(timespec="seconds") if status == "done" else None
                save_reqs(d)
                return r
    return None


def list_requests(status="all"):
    items = load_reqs()["items"]
    if status in ("open", "done"):
        items = [r for r in items if r["status"] == status]
    return sorted(items, key=lambda r: (r["status"] != "open", -r["id"]))


def _usd(v):
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _parse_dt(value):
    text = str(value or "").strip().replace("Z", "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _clean_text(value, limit):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(value or "")).strip()[:limit]


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return None


def _find_client(led, cid):
    for c in led.get("clients") or []:
        if c.get("id") == cid:
            return c
    return None


def get_client(cid):
    if not CLIENT_RE.fullmatch(cid or ""):
        return None
    return _find_client(load_ledger(), cid)


def _all_own(c):
    return bool(c.get("own_server")) and bool(c.get("own_line")) and bool(c.get("own_key"))


def is_release_due(c, at=None):
    if _all_own(c):
        return True
    rel = _parse_dt(c.get("release_at"))
    if rel is None:
        return False
    return (at or now()) >= rel


def client_view(c):
    spend = _usd(c.get("openai_spend_usd")) or Decimal("0")
    return {
        "id": c.get("id"),
        "label": c.get("label") or "",
        "invite_code": c.get("invite_code") or "",
        "started_at": c.get("started_at") or "",
        "release_at": c.get("release_at") or "",
        "own_server": bool(c.get("own_server")),
        "own_line": bool(c.get("own_line")),
        "own_key": bool(c.get("own_key")),
        "line_slot": c.get("line_slot") or "",
        "line_slot_set": bool(c.get("line_slot")),
        "key_slot_set": bool(c.get("key_slot")),
        "openai_cap_usd": format(CAP_USD, ".2f"),
        "openai_spend_usd": format(spend, ".4f"),
        "release_due": is_release_due(c),
        "slug": c.get("slug") or "",
    }


def host_invites_path():
    return os.path.join(AGENTS, "_shared", "invites.txt")


def _read_invite_lines():
    path = host_invites_path()
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def _write_invite_lines(lines):
    path = host_invites_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    data = "".join(line + "\n" for line in lines)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _publish_invite(code):
    lines = _read_invite_lines()
    if code not in lines:
        lines.append(code)
    _write_invite_lines(lines)


def _revoke_invite(code):
    path = host_invites_path()
    if not code or not os.path.isfile(path):
        return
    _write_invite_lines([ln for ln in _read_invite_lines() if ln != code])


def _remember_invite(code, cid):
    inv = load_invites()
    inv["codes"][code] = {"client_id": cid, "at": now().isoformat(timespec="seconds")}
    save_invites(inv)


def _forget_invite(code):
    inv = load_invites()
    inv["codes"].pop(code, None)
    save_invites(inv)


def _mint_invite_code():
    chars = [secrets.choice(INVITE_ALPHABET) for _ in range(10)]
    if not any(ch.isalpha() for ch in chars):
        chars[0] = "A"
    return "".join(chars)


def _unique_code(led):
    used = {c.get("invite_code") for c in led.get("clients") or []}
    existing = set(_read_invite_lines())
    for _ in range(8):
        code = _mint_invite_code()
        if code not in used and code not in existing:
            return code
    return None


def create_client(label, slug=""):
    label = _clean_text(label, 40)
    if not label:
        return None, "label required"
    slug = str(slug or "").strip()
    if slug and not SLUG_RE.fullmatch(slug):
        return None, "bad slug"
    with _lock:
        led = load_ledger()
        clients = led["clients"]
        if len(clients) >= MAX_PILOTS:
            return None, "pilot full"
        if slug and any(c.get("slug") == slug for c in clients):
            return None, "slug used"
        try:
            code = _unique_code(led)
        except OSError:
            return None, "invite store"
        if not code:
            return None, "invite store"
        n = int(led.get("next_id") or 1)
        if n < 1:
            n = 1
        while _find_client(led, "c%d" % n):
            n += 1
        started = now()
        client = {
            "id": "c%d" % n,
            "label": label,
            "invite_code": code,
            "started_at": started.isoformat(timespec="seconds"),
            "release_at": (started + timedelta(days=RELEASE_DAYS)).isoformat(timespec="seconds"),
            "own_server": False,
            "own_line": False,
            "own_key": False,
            "line_slot": "",
            "key_slot": "",
            "openai_cap_usd": format(CAP_USD, ".2f"),
            "openai_spend_usd": format(Decimal("0"), ".4f"),
            "slug": slug,
        }
        try:
            _publish_invite(code)
            _remember_invite(code, client["id"])
            clients.append(client)
            led["next_id"] = n + 1
            save_ledger(led)
        except OSError:
            try:
                _revoke_invite(code)
            except OSError:
                pass
            try:
                _forget_invite(code)
            except OSError:
                pass
            return None, "invite store"
    notify(f"נוסף חבר לפיילוט: {label}.")
    log("pilot client created")
    return client, None


def postpone_client(cid, release_at):
    new = _parse_dt(release_at)
    if new is None:
        return None, "bad time"
    label_s = ""
    shown = ""
    with _lock:
        led = load_ledger()
        c = _find_client(led, cid)
        if not c:
            return None, "missing"
        cur = _parse_dt(c.get("release_at"))
        if cur is None or new <= cur:
            return None, "only later"
        c["release_at"] = new.isoformat(timespec="seconds")
        save_ledger(led)
        label_s = c.get("label") or ""
        shown = c["release_at"]
        saved = c
    notify(f"{label_s}: השחרור נדחה ל-{shown[:16].replace('T', ' ')}.")
    return saved, None


def set_flags(cid, own_server=None, own_line=None, own_key=None):
    parsed = []
    for value in (own_server, own_line, own_key):
        if value is None:
            parsed.append(None)
            continue
        flag = _as_bool(value)
        if flag is None:
            return None, "bad flag"
        parsed.append(flag)
    with _lock:
        led = load_ledger()
        c = _find_client(led, cid)
        if not c:
            return None, "missing"
        for key, val in zip(("own_server", "own_line", "own_key"), parsed):
            if val is not None:
                c[key] = val
        save_ledger(led)
        saved = c
    if is_release_due(saved):
        enforce_due_releases()
        return None, "released"
    return saved, None


def set_slots(cid, payload):
    if not isinstance(payload, dict):
        return None, "bad status"
    with _lock:
        led = load_ledger()
        c = _find_client(led, cid)
        if not c:
            return None, "missing"
        if _as_bool(payload.get("clear_line")) is True:
            c["line_slot"] = ""
        elif "line_slot" in payload:
            c["line_slot"] = _clean_text(payload.get("line_slot"), 200)
        if _as_bool(payload.get("clear_key")) is True:
            c["key_slot"] = ""
        elif "key_slot" in payload:
            c["key_slot"] = _clean_text(payload.get("key_slot"), 200)
        save_ledger(led)
        return c, None


def set_link(cid, slug):
    slug = str(slug or "").strip()
    if slug and not SLUG_RE.fullmatch(slug):
        return None, "bad slug"
    with _lock:
        led = load_ledger()
        if slug and any(c.get("id") != cid and c.get("slug") == slug for c in led["clients"]):
            return None, "slug used"
        c = _find_client(led, cid)
        if not c:
            return None, "missing"
        c["slug"] = slug
        save_ledger(led)
        return c, None


def add_spend(cid, usd, source="operator"):
    add = _usd(usd)
    if add is None or add <= 0:
        return None, "bad amount"
    if source not in ("operator", "tenant"):
        source = "operator"
    blocked = False
    reached = False
    label_s = ""
    saved = None
    with _lock:
        led = load_ledger()
        c = _find_client(led, cid)
        if not c:
            return None, "missing"
        spent = _usd(c.get("openai_spend_usd")) or Decimal("0")
        label_s = c.get("label") or ""
        if spent + add > CAP_USD:
            blocked = True
        else:
            spent = (spent + add).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            c["openai_spend_usd"] = format(spent, ".4f")
            c["openai_cap_usd"] = format(CAP_USD, ".2f")
            save_ledger(led)
            costs = load_costs()
            rows = costs.get(cid)
            if not isinstance(rows, list):
                rows = []
            rows.append({"at": now().isoformat(timespec="seconds"), "usd": format(add, ".4f"), "source": source})
            costs[cid] = rows[-200:]
            save_costs(costs)
            reached = spent >= CAP_USD
            saved = c
    if blocked:
        notify(f"{label_s}: חיוב נחסם כי הוא עובר את התקרה של 20 דולר.", urgent=True)
        return None, "cap"
    if reached:
        notify(f"{label_s}: ההוצאה על OpenAI הגיעה לתקרה של 20 דולר.", urgent=True)
    return saved, None


def _append_history(cid, snap):
    with _lock:
        hist = load_history()
        rows = hist.get(cid)
        if not isinstance(rows, list):
            rows = []
        rows.append(snap)
        hist[cid] = rows[-200:]
        save_history(hist)


def record_status(cid, snap):
    if not isinstance(snap, dict):
        return None, "bad status"
    if not get_client(cid):
        return None, "missing"
    clean = {"at": now().isoformat(timespec="seconds")}
    for key in ("up", "whatsapp_connected", "google_connected", "openai_key_set"):
        if key in snap and snap[key] is not None:
            flag = _as_bool(snap[key])
            if flag is None:
                return None, "bad status"
            clean[key] = flag
    if "note" in snap and snap["note"] is not None:
        clean["note"] = _clean_text(snap.get("note"), 120)
    _append_history(cid, clean)
    return clean, None


def _contains_any(text, needles):
    blob = str(text or "")
    return any(n and n in blob for n in needles)


def _inside(root, *parts):
    if not root:
        return None
    base = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root, *parts))
    if path.startswith(base + os.sep):
        return path
    return None


def _drop_registry_slug(slug):
    if not slug or not SLUG_RE.fullmatch(slug) or not os.path.isfile(REG):
        return
    reg = _read_json(REG, {})
    if not isinstance(reg, dict) or slug not in reg:
        return
    reg.pop(slug, None)
    _write_json(REG, reg)


def _drop_status_files(slug):
    if not slug or not SLUG_RE.fullmatch(slug):
        return
    path = _inside(STATUS_DIR, slug + ".json")
    if path and os.path.lexists(path) and (os.path.islink(path) or os.path.isfile(path)):
        os.remove(path)
    idx_path = _inside(STATUS_DIR, "index.json")
    if not idx_path or not os.path.isfile(idx_path) or os.path.islink(idx_path):
        return
    idx = _read_json(idx_path, None)
    if not isinstance(idx, dict) or not isinstance(idx.get("bots"), dict) or slug not in idx["bots"]:
        return
    idx["bots"].pop(slug, None)
    _write_json(idx_path, idx)


def _drop_manager_token(slug):
    if not slug or not SLUG_RE.fullmatch(slug):
        return
    path = _inside(AGENTS, slug, ".manager_token")
    if not path or not os.path.lexists(path):
        return
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)


def _scrub_outbox(needles):
    path = os.path.join(DATA, "outbox.log")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    kept = []
    for part in raw.split("---\n"):
        if part.strip() and not _contains_any(part, needles):
            kept.append(part.strip("\n") + "\n---\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(kept))
    os.chmod(path, 0o600)


def _request_belongs(item, cid, slug):
    if item.get("client_id") == cid:
        return True
    return bool(slug) and item.get("slug") == slug


def delete_client(cid):
    if not CLIENT_RE.fullmatch(cid or ""):
        return False
    with _lock:
        led = load_ledger()
        c = _find_client(led, cid)
        if not c:
            return False
        slug = c.get("slug") or ""
        code = c.get("invite_code") or ""
        needles = []
        for value in (c.get("label"), code, slug, c.get("line_slot"), c.get("key_slot")):
            text = str(value or "")
            if len(text) >= 4:
                needles.append(text)
        led["clients"] = [row for row in led["clients"] if row.get("id") != cid]
        save_ledger(led)
        inv = load_invites()
        inv["codes"] = {
            k: v for k, v in inv["codes"].items()
            if k != code and (v or {}).get("client_id") != cid
        }
        save_invites(inv)
        try:
            _revoke_invite(code)
        except OSError as e:
            log(f"invite revoke: {type(e).__name__}")
        costs = load_costs()
        if cid in costs:
            costs.pop(cid, None)
            save_costs(costs)
        hist = load_history()
        for ev in hist.get(cid) or []:
            if isinstance(ev, dict):
                note = str(ev.get("note") or "")
                if len(note) >= 4:
                    needles.append(note)
        if cid in hist:
            hist.pop(cid, None)
            save_history(hist)
        reqs = load_reqs()
        kept = []
        for item in reqs.get("items") or []:
            if _request_belongs(item, cid, slug):
                text = str(item.get("text") or "")
                if len(text) >= 4:
                    needles.append(text)
                continue
            kept.append(item)
        reqs["items"] = kept
        save_reqs(reqs)
        st = load_state()
        bots = st.get("bots") if isinstance(st.get("bots"), dict) else {}
        if slug:
            bots.pop(slug, None)
        st["bots"] = bots
        sent_keys = st.get("sent_keys") if isinstance(st.get("sent_keys"), dict) else {}
        drop = {cid}
        if slug:
            drop.add(slug)
        st["sent_keys"] = {k: v for k, v in sent_keys.items() if k.split(":", 1)[0] not in drop}
        pending = st.get("pending") if isinstance(st.get("pending"), list) else []
        st["pending"] = [t for t in pending if not _contains_any(t, needles)]
        save_state(st)
        _drop_registry_slug(slug)
        _drop_status_files(slug)
        _drop_manager_token(slug)
        _scrub_outbox(needles)
    notify("נמחקה רשומת פיילוט.")
    log("pilot client deleted")
    return True


def costs_view():
    costs = load_costs()
    items = {}
    for c in load_ledger().get("clients") or []:
        cid = c.get("id")
        rows = costs.get(cid)
        if not isinstance(rows, list):
            rows = []
        view = client_view(c)
        items[cid] = {"spend": view["openai_spend_usd"], "cap": view["openai_cap_usd"], "events": rows}
    return items


def invites_view():
    inv = load_invites()
    live = {c.get("id"): c for c in load_ledger().get("clients") or []}
    items = []
    for code, rec in inv["codes"].items():
        cid = (rec or {}).get("client_id")
        if cid in live:
            items.append({"code": code, "client_id": cid, "label": live[cid].get("label") or ""})
    return items


def lookup_invite(code):
    if not re.fullmatch(r"[A-Za-z0-9]{6,40}", code or ""):
        return None
    rec = load_invites()["codes"].get(code)
    if not rec:
        return None
    return get_client(rec.get("client_id"))


def summary():
    enforce_due_releases()
    idx = status_index()
    reg = registry()
    reqs = load_reqs()["items"]
    bots = []
    for slug, v in sorted(reg.items()):
        b = bot_status(slug, idx)
        n = env_names(slug)
        bots.append({
            "slug": slug, "bot_name": n["BOT_NAME"], "owner_name": n["OWNER_NAME"], "created": v.get("created"),
            "up": bool(b.get("up")), "bot_unit": b.get("bot_unit"), "bridge_unit": b.get("bridge_unit"),
            "whatsapp_connected": b.get("whatsapp_connected"), "google_connected": b.get("google_connected"),
            "openai_key_set": b.get("openai_key_set"), "openai_error": b.get("openai_error") or "",
            "google_error": b.get("google_error") or "",
            "cost_today_usd": b.get("cost_today_usd"), "cost_month_usd": b.get("cost_month_usd"),
            "heartbeat_age_s": b.get("heartbeat_age_s"),
            "open_requests": sum(1 for r in reqs if r["slug"] == slug and r["status"] == "open"),
        })
    age = _age_min((idx or {}).get("checked_at"))
    pilots = [client_view(c) for c in load_ledger().get("clients") or []]
    return {
        "ok": True, "generated_at": now().isoformat(timespec="seconds"),
        "status_age_min": round(age, 1) if age is not None else None,
        "bots": bots, "capacity": (idx or {}).get("capacity") or {},
        "requests_open": sum(1 for r in reqs if r["status"] == "open"), "requests_total": len(reqs),
        "pilots": pilots, "pilot_limit": MAX_PILOTS,
    }


def _alert(st, slug, kind, text, sent):
    key = f"{slug}:{kind}:{today()}"
    if key in st.setdefault("sent_keys", {}):
        return
    st["sent_keys"][key] = now().isoformat(timespec="seconds")
    sent.append(text)


def enforce_due_releases():
    """Delete every pilot row that is due. Same path as an explicit delete."""
    with _lock:
        due = [c.get("id") for c in (load_ledger().get("clients") or []) if c.get("id") and is_release_due(c)]
    released = []
    for cid in due:
        if delete_client(cid):
            released.append(cid)
    return released


def _pilot_alerts(st, sent):
    for c in load_ledger().get("clients") or []:
        if is_release_due(c):
            continue
        label_s = c.get("label") or ""
        spent = _usd(c.get("openai_spend_usd")) or Decimal("0")
        if spent >= CAP_USD:
            _alert(st, c["id"], "cap", f"{label_s}: ההוצאה על OpenAI הגיעה לתקרה של 20 דולר.", sent)


def _archive_status(slug, b):
    c = _client_for_slug(slug)
    if not c or not isinstance(b, dict):
        return
    snap = {"at": now().isoformat(timespec="seconds"), "up": bool(b.get("up"))}
    for key in ("whatsapp_connected", "google_connected", "openai_key_set"):
        if isinstance(b.get(key), bool):
            snap[key] = b[key]
    _append_history(c["id"], snap)


def health_tick():
    with _lock:
        st = load_state()
        h = st.setdefault("bots", {})
        sent = []
        baseline = not st.get("baseline_done")
        reg = registry()
        try:
            provision_tokens(reg)
        except Exception as e:
            log(f"provision: {type(e).__name__}")
        idx = status_index()
        if idx is None:
            if reg:
                _alert(st, "_host", "noindex", "⚠️ אין קובץ מצב בוטים (/opt/agents/_status/index.json). assistant-status.timer לא רץ?", sent)
        else:
            age = _age_min(idx.get("checked_at"))
            if age is None or age > 15:
                if not st.get("stale"):
                    st["stale"] = True
                    _alert(st, "_host", "stale", "⚠️ בדיקת המצב של הבוטים לא רצה כבר יותר מרבע שעה (assistant-status.timer). אין לי נתונים עדכניים.", sent)
            else:
                if st.get("stale"):
                    st["stale"] = False
                for slug, v in sorted(reg.items()):
                    _bot_tick(slug, v, bot_status(slug, idx), h.setdefault(slug, {}), st, sent, baseline)
        for slug in [s for s in h if s not in reg]:
            h.pop(slug, None)
        cut = (now() - timedelta(days=14)).strftime("%Y-%m-%d")
        st["sent_keys"] = {k: v for k, v in (st.get("sent_keys") or {}).items() if k.rsplit(":", 1)[-1] >= cut}
        if idx is not None and not st.get("stale"):
            st["baseline_done"] = True
        st["last_tick"] = now().isoformat(timespec="seconds")
        if baseline and sent and idx is not None:
            log(f"baseline: {len(sent)} existing condition(s) recorded silently")
            sent = []
        pilot_sent = []
        _pilot_alerts(st, pilot_sent)
        for text in pilot_sent:
            sent.append(text)
        for text in sent:
            st["pending"] = (st.get("pending") or [])[-19:] + [text]
        flush_pending(st)
        save_state(st)
        result = sent
    enforce_due_releases()
    return result


def _bot_tick(slug, v, b, s, st, sent, baseline):
    try:
        _archive_status(slug, b)
    except Exception as e:
        log(f"status archive: {type(e).__name__}")
    L = label(slug)
    up = bool(b.get("up"))
    if up:
        if s.get("down_alerted"):
            s["down_alerted"] = False
            _alert(st, slug, "up", f"🟢 {L} חזר לפעול.", sent)
        s["seen_up"] = True
        s["down_n"] = 0
        wa = b.get("whatsapp_connected")
        if wa is True:
            if s.get("wa_alerted"):
                s["wa_alerted"] = False
                _alert(st, slug, "wa_back", f"✅ {L}: וואטסאפ חובר מחדש.", sent)
            s["wa_seen"] = True
            s["wa_n"] = 0
        elif wa is False and s.get("wa_seen"):
            s["wa_n"] = s.get("wa_n", 0) + 1
            if s["wa_n"] >= 2 and not s.get("wa_alerted"):
                s["wa_alerted"] = True
                _alert(st, slug, "wa_down", f"📵 {L}: הוואטסאפ התנתק. החבר צריך לחבר מחדש בדף ההגדרות (קוד חיבור / QR).", sent)
        g, ge = b.get("google_connected"), str(b.get("google_error") or "")
        if g is True and not ge:
            s["g_seen"] = True
        g_bad = bool(ge) or (g is False and s.get("g_seen"))
        if g_bad and not s.get("g_alerted"):
            s["g_alerted"] = True
            _alert(st, slug, "google", f"🔑 {L}: החיבור לגוגל {'פג' if ge else 'נותק'} (יומן/מייל לא יעבדו). החבר צריך לחבר מחדש את Google בדף ההגדרות.", sent)
        elif not g_bad and s.get("g_alerted") and g is True:
            s["g_alerted"] = False
            _alert(st, slug, "google_back", f"✅ {L}: גוגל חובר מחדש.", sent)
        oe, ks = str(b.get("openai_error") or ""), b.get("openai_key_set")
        if ks is True:
            s["oa_seen"] = True
        oa_bad = oe in ("auth", "quota", "ceiling") or (ks is False and s.get("oa_seen"))
        if oa_bad and s.get("oa_alerted") != (oe or "nokey"):
            s["oa_alerted"] = oe or "nokey"
            why = {"auth": "המפתח נדחה (בוטל או שגוי)", "quota": "נגמר הקרדיט בחשבון OpenAI", "ceiling": "הגיע לתקרת העלות החודשית שהוגדרה",
                   "nokey": "חסר מפתח"}.get(oe or "nokey", oe)
            _alert(st, slug, "openai_" + (oe or "nokey"), f"💳 {L}: OpenAI לא עובד, {why}. הבוט לא יענה עד שזה יטופל.", sent)
        elif not oa_bad and s.get("oa_alerted"):
            s["oa_alerted"] = ""
            _alert(st, slug, "openai_back", f"✅ {L}: OpenAI עובד שוב.", sent)
        return
    if s.get("seen_up"):
        s["down_n"] = s.get("down_n", 0) + 1
        if s["down_n"] >= 2 and not s.get("down_alerted"):
            s["down_alerted"] = True
            _alert(st, slug, "down", f"🔴 {L} הפסיק לפעול (בוט {b.get('bot_unit')}, וואטסאפ-גשר {b.get('bridge_unit')}). "
                                     f"לבדיקה: journalctl -u assistant-bot@{slug} -n 50", sent)
        return
    try:
        created = datetime.strptime(str(v.get("created")), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return
    if now() - created > timedelta(hours=STALL_H):
        last = s.get("stall_alerted_at")
        if not last or now() - datetime.fromisoformat(last) > timedelta(days=7):
            s["stall_alerted_at"] = now().isoformat(timespec="seconds")
            hrs = int((now() - created).total_seconds() // 3600)
            _alert(st, slug, "stalled", f"⏳ {L}: ההקמה נתקעה. נוצר לפני {hrs} שעות ועדיין לא עלה אף פעם. כדאי לבדוק עם החבר באיזה שלב הוא עצר.", sent)


def watcher_loop():
    time.sleep(int(E("AM_FIRST_TICK_S", "20")))
    while True:
        try:
            health_tick()
        except Exception as e:
            log(f"tick: {type(e).__name__}: {e}")
        time.sleep(TICK_S)


CSS = """body{font-family:system-ui,-apple-system,'Segoe UI',Arial,sans-serif;margin:0;background:#f4f6f8;color:#1b1f24}
header{background:#1f3b57;color:#fff;padding:14px 16px}header h1{margin:0;font-size:20px}header small{opacity:.8}
main{max-width:760px;margin:0 auto;padding:12px}section{background:#fff;border-radius:12px;padding:12px 14px;margin:12px 0;box-shadow:0 1px 3px #0001}
h2{font-size:17px;margin:4px 0 10px}.bot,.req{border-top:1px solid #eef0f2;padding:10px 0}.bot:first-of-type,.req:first-of-type{border-top:0}
.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.pill{font-size:13px;padding:2px 8px;border-radius:999px;background:#eef1f4}
.ok{background:#e3f6e8;color:#14662f}.bad{background:#fde7e7;color:#9b1c1c}.mid{background:#fff4d6;color:#7a5300}
.name{font-weight:600}.muted{color:#68707a;font-size:13px}.txt{white-space:pre-wrap;margin:6px 0}
button{font:inherit;border:0;border-radius:10px;padding:10px 14px;background:#1f6feb;color:#fff;cursor:pointer}
button.sec{background:#e9edf2;color:#1b1f24}button.danger{background:#9b1c1c}
.code{font-size:28px;letter-spacing:2px;font-family:monospace;background:#f0f4ff;padding:10px;border-radius:10px;text-align:center;direction:ltr}
.icode{font-family:ui-monospace,monospace;direction:ltr}
input{font:inherit;border:1px solid #d5dbe3;border-radius:8px;padding:8px}
details summary{cursor:pointer;color:#1f6feb}form{display:inline}"""


def _e(x):
    return html.escape(str(x if x is not None else ""))


def _yn(v, ok="מחובר", bad="מנותק", none="לא ידוע"):
    if v is True:
        return f'<span class="pill ok">{ok}</span>'
    if v is False:
        return f'<span class="pill bad">{bad}</span>'
    return f'<span class="pill">{none}</span>'


def _render_pilots(csrf, s):
    P = PREFIX + "/"
    pilots = s.get("pilots") or []
    out = [
        f'<section><h2>פיילוט</h2><p class="muted">עד {MAX_PILOTS} חברים. תקרת OpenAI: 20 דולר.</p>',
        f'<form method="post" action="{P}clients"><input type="hidden" name="csrf" value="{csrf}">',
        '<div class="row"><input name="label" maxlength="40" required placeholder="שם תצוגה" aria-label="שם תצוגה">',
        '<button>הוסף חבר</button></div></form>',
    ]
    cap = s.get("capacity") or {}
    if cap:
        out.append(f'<p class="muted">יש מקום בשרת לעוד בערך {_e(cap.get("more_bots_fit"))} בוטים.</p>')
    if not pilots:
        out.append('<p class="muted">אין עדיין חברים בפיילוט.</p></section>')
        return "".join(out)
    for b in pilots:
        cid = _e(b.get("id"))
        flags = (
            f'<input type="hidden" name="own_server" value="0"><label><input type="checkbox" name="own_server" value="1"{" checked" if b.get("own_server") else ""}> שרת משלו</label>'
            f'<input type="hidden" name="own_line" value="0"><label><input type="checkbox" name="own_line" value="1"{" checked" if b.get("own_line") else ""}> קו משלו</label>'
            f'<input type="hidden" name="own_key" value="0"><label><input type="checkbox" name="own_key" value="1"{" checked" if b.get("own_key") else ""}> מפתח משלו</label>'
        )
        due = '<span class="pill bad">מוכן לשחרור</span>' if b.get("release_due") else '<span class="pill">לפני שחרור</span>'
        line_state = "מולא" if b.get("line_slot_set") else "ריק"
        key_state = "מולא" if b.get("key_slot_set") else "ריק"
        out.append(
            f'<div class="bot"><div class="row"><span class="name">{_e(b.get("label"))}</span>'
            f'<span class="muted">{cid}</span>{due}</div>'
            f'<div class="icode">{_e(b.get("invite_code"))}</div>'
            f'<div class="muted">התחלה {_e((b.get("started_at") or "")[:16].replace("T", " "))} · '
            f'שחרור {_e((b.get("release_at") or "")[:16].replace("T", " "))} · '
            f'{_e(b.get("openai_spend_usd"))} / {_e(b.get("openai_cap_usd"))} דולר</div>'
            f'<div class="muted">קו: {line_state} · מפתח: {key_state}</div>'
            f'<form method="post" action="{P}clients/{cid}/postpone"><input type="hidden" name="csrf" value="{csrf}">'
            f'<div class="row"><input name="release_at" required placeholder="2026-12-01T09:00" aria-label="מועד שחרור">'
            f'<button class="sec">דחה שחרור</button></div></form>'
            f'<form method="post" action="{P}clients/{cid}/flags"><input type="hidden" name="csrf" value="{csrf}">'
            f'<div class="row">{flags}<button class="sec">שמור סימונים</button></div></form>'
            f'<form method="post" action="{P}clients/{cid}/slots"><input type="hidden" name="csrf" value="{csrf}">'
            f'<div class="row"><input name="line_slot" value="{_e(b.get("line_slot") or "")}" placeholder="קו" aria-label="קו" autocomplete="off">'
            f'<input name="key_slot" type="password" value="" placeholder="מפתח" aria-label="מפתח" autocomplete="off">'
            f'<label><input type="checkbox" name="clear_key" value="1"> נקה מפתח</label>'
            f'<button class="sec">שמור משבצות</button></div></form>'
            f'<form method="post" action="{P}clients/{cid}/spend"><input type="hidden" name="csrf" value="{csrf}">'
            f'<div class="row"><input name="usd" inputmode="decimal" required placeholder="0.00" aria-label="הוצאה">'
            f'<button class="sec">הוסף הוצאה</button></div></form>'
            f'<form method="post" action="{P}clients/{cid}/link"><input type="hidden" name="csrf" value="{csrf}">'
            f'<div class="row"><input name="slug" value="{_e(b.get("slug") or "")}" placeholder="slug" aria-label="slug">'
            f'<button class="sec">קשר בוט</button></div></form>'
            f'<form method="post" action="{P}clients/{cid}/delete" onsubmit="return confirm(\'למחוק את הרשומה ואת הנתונים אצלנו?\')">'
            f'<input type="hidden" name="csrf" value="{csrf}"><button class="danger">מחק מהרשימה</button></form></div>'
        )
    out.append("</section>")
    return "".join(out)


def render_page(csrf, invite_code=None, flash=""):
    s = summary()
    P = PREFIX + "/"
    out = [
        '<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<meta name="robots" content="noindex"><title>מנהל העוזרים</title><style>{CSS}</style></head><body>',
        f'<header><h1>מנהל העוזרים</h1><small>עודכן {_e(s["generated_at"][11:16])}',
    ]
    if s["status_age_min"] is not None:
        out.append(f' · בדיקת מצב לפני {s["status_age_min"]:.0f} דק׳')
    else:
        out.append(" · אין נתוני מצב")
    out.append("</small></header><main>")
    if flash:
        out.append(f"<section><b>{_e(flash)}</b></section>")
    if invite_code:
        msg = f"היי! הנה קוד ההזמנה שלך לעוזר האישי: {invite_code}\nנכנסים לכאן ומתחילים: {ORIGIN}/"
        out.append(
            f'<section><h2>הזמנה חדשה לחבר</h2><div class="code" id="c">{_e(invite_code)}</div>'
            f'<p class="muted">הקוד חד-פעמי. שלח לחבר יחד עם הקישור {_e(ORIGIN)}/</p><div class="row">'
            f'<button onclick="navigator.clipboard.writeText(document.getElementById(\'c\').textContent)">העתק קוד</button>'
            f'<a href="https://wa.me/?text={urllib.parse.quote(msg)}"><button class="sec" type="button">שלח בוואטסאפ</button></a></div></section>'
        )
    out.append(_render_pilots(csrf, s))
    out.append("<section><h2>הבוטים</h2>")
    if not s["bots"]:
        out.append('<p class="muted">אין עדיין בוטים.</p>')
    for b in s["bots"]:
        if b["up"]:
            up = '<span class="pill ok">פועל</span>'
        elif b["bot_unit"] == "active" or b.get("heartbeat_age_s") is not None:
            up = '<span class="pill bad">לא פועל</span>'
        else:
            up = '<span class="pill mid">בהקמה / כבוי</span>'
        if b["openai_error"]:
            oa = '<span class="pill bad">OpenAI: ' + _e({"auth": "מפתח נדחה", "quota": "נגמר קרדיט", "ceiling": "תקרת עלות"}.get(b["openai_error"], b["openai_error"])) + "</span>"
        else:
            oa = _yn(b["openai_key_set"], "OpenAI תקין", "OpenAI חסר", "OpenAI ?")
        cost = f'${float(b["cost_month_usd"] or 0):.2f} החודש' if b.get("cost_month_usd") is not None else ""
        nq = b["open_requests"]
        oq = "" if not nq else (" · בקשה פתוחה אחת" if nq == 1 else " · %d בקשות פתוחות" % nq)
        google = _yn(b["google_connected"] and not b["google_error"], "Google מחובר", "Google לא מחובר", "Google ?") if b["google_connected"] is not None else _yn(None, none="Google ?")
        out.append(
            f'<div class="bot"><div class="row"><span class="name">{_e(b["bot_name"] or b["slug"])}</span>'
            f'<span class="muted">{_e(b["owner_name"])} · {_e(b["slug"])}</span></div>'
            f'<div class="row">{up}{_yn(b["whatsapp_connected"], "וואטסאפ מחובר", "וואטסאפ מנותק", "וואטסאפ ?")}{google}{oa}</div>'
            f'<div class="muted">נוצר {_e(b["created"])}{" · " + cost if cost else ""}{oq}</div></div>'
        )
    out.append("</section><section><h2>בקשות מחברים</h2>")
    reqs = list_requests("all")
    opn = [r for r in reqs if r["status"] == "open"]
    done = [r for r in reqs if r["status"] == "done"]
    if not opn:
        out.append('<p class="muted">אין בקשות פתוחות 🎉</p>')
    for r in opn:
        out.append(
            f'<div class="req"><div class="row"><span class="name">#{r["id"]} · {_e(r["owner_name"] or r["slug"])}</span>'
            f'<span class="muted">{_e(r["bot_name"])} · {_e(r["created"][:16].replace("T", " "))}</span></div>'
            f'<div class="txt">{_e(r["text"])}</div><form method="post" action="{P}done/{r["id"]}"><input type="hidden" name="csrf" value="{csrf}">'
            f"<button>סמן כבוצע</button></form></div>"
        )
    if done:
        out.append(f"<details><summary>בוצעו ({len(done)})</summary>")
        for r in done[:100]:
            out.append(
                f'<div class="req"><div class="row"><span class="name">#{r["id"]} · {_e(r["owner_name"] or r["slug"])}</span>'
                f'<span class="pill ok">בוצע {_e((r.get("done_at") or "")[:10])}</span></div><div class="txt">{_e(r["text"])}</div>'
                f'<form method="post" action="{P}reopen/{r["id"]}"><input type="hidden" name="csrf" value="{csrf}"><button class="sec">פתח מחדש</button></form></div>'
            )
        out.append("</details>")
    out.append('</section><p class="muted" style="text-align:center">רק מצב ובקשות שהחברים אישרו להעביר. אין כאן גישה לשיחות, מיילים או זיכרונות שלהם.</p></main></body></html>')
    return "".join(out)


API_TOKEN = PROXY_SECRET = ""


def csrf_token():
    return hmac.new(PROXY_SECRET.encode(), b"am-csrf-v1", hashlib.sha256).hexdigest()[:32]


def _json_obj(raw):
    if not raw:
        return {}
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("object")
    return data


def _err_status(err):
    return {
        "missing": 404,
        "label required": 400,
        "bad time": 400,
        "bad amount": 400,
        "bad slug": 400,
        "bad status": 400,
        "bad flag": 400,
        "only later": 400,
        "pilot full": 409,
        "cap": 409,
        "slug used": 409,
        "invite store": 500,
    }.get(err, 400)


class H(BaseHTTPRequestHandler):
    server_version = "assistant-manager"
    sys_version = ""

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, bytes):
            b = body
        elif isinstance(body, str):
            b = body.encode("utf-8")
        else:
            b = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("referrer-policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("x-frame-options", "DENY")
            self.send_header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _body(self, limit=8192):
        n = int(self.headers.get("content-length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    @property
    def kind(self):
        kind = self.server.kind
        if kind != "both":
            return kind
        path = urllib.parse.urlsplit(self.path).path or "/"
        prefix = PREFIX or ""
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return "proxy"
        return "local"

    def _proxy_ok(self):
        return _digest_eq(self.headers.get("x-manager-proxy") or "", PROXY_SECRET)

    def _api_ok(self):
        return _digest_eq(self.headers.get("x-manager-token") or "", API_TOKEN)

    def _path(self):
        path = urllib.parse.urlsplit(self.path).path
        if not PREFIX:
            return path
        if path == PREFIX:
            return "/"
        head = PREFIX + "/"
        if path.startswith(head):
            return path[len(PREFIX):] or "/"
        return path

    def do_GET(self):
        p = self._path()
        if p == "/healthz":
            return self._send(200, {"ok": True})
        enforce_due_releases()
        if self.kind == "proxy":
            if not self._proxy_ok():
                return self._send(403, {"ok": False})
            if p in ("/", ""):
                return self._send(200, render_page(csrf_token()), "text/html; charset=utf-8")
            return self._send(404, {"ok": False})
        if not p.startswith("/api/") or not self._api_ok():
            return self._send(401 if p.startswith("/api/") else 404, {"ok": False})
        return self._api_get(p)

    def _api_get(self, p):
        if p == "/api/summary":
            return self._send(200, summary())
        if p == "/api/clients":
            return self._send(200, {"ok": True, "clients": [client_view(c) for c in load_ledger().get("clients") or []]})
        m = re.fullmatch(r"/api/clients/(c[1-9][0-9]{0,5})", p)
        if m:
            c = get_client(m.group(1))
            if not c:
                return self._send(404, {"ok": False, "error": "missing"})
            return self._send(200, {"ok": True, "client": client_view(c)})
        if p == "/api/requests":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return self._send(200, {"ok": True, "items": list_requests((q.get("status") or ["all"])[0])})
        if p == "/api/costs":
            return self._send(200, {"ok": True, "items": costs_view()})
        m = re.fullmatch(r"/api/costs/(c[1-9][0-9]{0,5})", p)
        if m:
            items = costs_view()
            if m.group(1) not in items:
                return self._send(404, {"ok": False, "error": "missing"})
            return self._send(200, {"ok": True, "client_id": m.group(1), "costs": items[m.group(1)]})
        m = re.fullmatch(r"/api/status/(c[1-9][0-9]{0,5})", p)
        if m:
            if not get_client(m.group(1)):
                return self._send(404, {"ok": False, "error": "missing"})
            hist = load_history()
            rows = hist.get(m.group(1))
            if not isinstance(rows, list):
                rows = []
            return self._send(200, {"ok": True, "items": rows})
        if p == "/api/invites":
            return self._send(200, {"ok": True, "items": invites_view()})
        m = re.fullmatch(r"/api/invites/([A-Za-z0-9]{6,40})", p)
        if m:
            c = lookup_invite(m.group(1))
            if not c:
                return self._send(404, {"ok": False, "error": "missing"})
            return self._send(200, {"ok": True, "client_id": c["id"], "label": c.get("label") or ""})
        return self._send(404, {"ok": False})

    def do_POST(self):
        p = self._path()
        try:
            raw = self._body()
        except Exception:
            return self._send(413, {"ok": False})
        if p != "/healthz":
            enforce_due_releases()
        if self.kind == "local" and p == "/intake":
            return self._intake(raw)
        if self.kind == "local" and p == "/cost":
            return self._cost(raw)
        if self.kind == "local" and p == "/status":
            return self._status_post(raw)
        if self.kind == "proxy":
            return self._admin_post(p, raw)
        if not p.startswith("/api/") or not self._api_ok():
            return self._send(401, {"ok": False})
        return self._api_post(p, raw)

    def _api_post(self, p, raw):
        try:
            d = _json_obj(raw)
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        if p in ("/api/clients", "/api/invite"):
            c, err = create_client(d.get("label") or "", d.get("slug") or "")
            if err:
                return self._send(_err_status(err), {"ok": False, "error": err})
            return self._send(200, {"ok": True, "client": client_view(c)})
        m = re.fullmatch(r"/api/requests/(\d+)/(done|reopen)", p)
        if m:
            r = set_status(int(m.group(1)), "done" if m.group(2) == "done" else "open")
            return self._send(200 if r else 404, {"ok": bool(r), "item": r})
        m = re.fullmatch(r"/api/clients/(c[1-9][0-9]{0,5})/(postpone|flags|slots|spend|status|delete|link)", p)
        if not m:
            return self._send(404, {"ok": False})
        cid, action = m.group(1), m.group(2)
        if action == "delete":
            if not delete_client(cid):
                return self._send(404, {"ok": False, "error": "missing"})
            return self._send(200, {"ok": True})
        if action == "postpone":
            c, err = postpone_client(cid, d.get("release_at"))
        elif action == "flags":
            c, err = set_flags(
                cid,
                d["own_server"] if "own_server" in d else None,
                d["own_line"] if "own_line" in d else None,
                d["own_key"] if "own_key" in d else None,
            )
            if err == "released":
                return self._send(200, {"ok": True, "released": True})
        elif action == "slots":
            c, err = set_slots(cid, d)
        elif action == "spend":
            c, err = add_spend(cid, d.get("usd"), "operator")
        elif action == "status":
            c, err = record_status(cid, d)
        else:
            c, err = set_link(cid, d.get("slug") or "")
        if err:
            return self._send(_err_status(err), {"ok": False, "error": err})
        if action == "status":
            return self._send(200, {"ok": True})
        return self._send(200, {"ok": True, "client": client_view(c)})

    def _intake(self, raw):
        try:
            d = _json_obj(raw)
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        slug = str(d.get("slug") or "")
        if not tenant_token_ok(slug, self.headers.get("x-manager-token") or ""):
            time.sleep(0.5)
            log(f"intake rejected (slug={slug[:22]!r})")
            return self._send(403, {"ok": False, "error": "unauthorized"})
        item, err = add_request(slug, d.get("summary"))
        if not item:
            return self._send(429 if err == "daily limit" else 400, {"ok": False, "error": err})
        return self._send(200, {"ok": True, "id": item["id"]})

    def _cost(self, raw):
        try:
            d = _json_obj(raw)
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        slug = str(d.get("slug") or "")
        if not tenant_token_ok(slug, self.headers.get("x-manager-token") or ""):
            time.sleep(0.5)
            log("cost rejected")
            return self._send(403, {"ok": False, "error": "unauthorized"})
        c = _client_for_slug(slug)
        if not c:
            return self._send(404, {"ok": False, "error": "missing"})
        saved, err = add_spend(c["id"], d.get("usd"), "tenant")
        if err:
            return self._send(_err_status(err), {"ok": False, "error": err})
        return self._send(200, {"ok": True, "openai_spend_usd": client_view(saved)["openai_spend_usd"]})

    def _status_post(self, raw):
        try:
            d = _json_obj(raw)
        except Exception:
            return self._send(400, {"ok": False, "error": "bad json"})
        slug = str(d.get("slug") or "")
        if not tenant_token_ok(slug, self.headers.get("x-manager-token") or ""):
            time.sleep(0.5)
            log("status rejected")
            return self._send(403, {"ok": False, "error": "unauthorized"})
        c = _client_for_slug(slug)
        if not c:
            return self._send(404, {"ok": False, "error": "missing"})
        snap = {k: d.get(k) for k in ("up", "whatsapp_connected", "google_connected", "openai_key_set", "note") if k in d}
        _saved, err = record_status(c["id"], snap)
        if err:
            return self._send(_err_status(err), {"ok": False, "error": err})
        return self._send(200, {"ok": True})

    def _admin_post(self, p, raw):
        if not self._proxy_ok():
            return self._send(403, {"ok": False})
        org = self.headers.get("origin")
        if org and org.rstrip("/") != ORIGIN.rstrip("/"):
            return self._send(403, {"ok": False, "error": "origin"})
        f = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        if not _digest_eq((f.get("csrf") or [""])[0], csrf_token()):
            return self._send(403, {"ok": False, "error": "csrf"})
        m = re.fullmatch(r"/(done|reopen)/(\d+)", p)
        if m:
            set_status(int(m.group(2)), "done" if m.group(1) == "done" else "open")
            return self._send(303, b"", "text/plain", {"location": PREFIX + "/"})
        if p == "/clients":
            c, err = create_client((f.get("label") or [""])[0], (f.get("slug") or [""])[0])
            if err:
                return self._send(200, render_page(csrf_token(), flash=_flash(err)), "text/html; charset=utf-8")
            return self._send(200, render_page(csrf_token(), invite_code=c["invite_code"]), "text/html; charset=utf-8")
        m = re.fullmatch(r"/clients/(c[1-9][0-9]{0,5})/(postpone|flags|slots|spend|delete|link)", p)
        if not m:
            return self._send(404, {"ok": False})
        cid, action = m.group(1), m.group(2)
        err = None
        if action == "delete":
            if not delete_client(cid):
                err = "missing"
        elif action == "postpone":
            _c, err = postpone_client(cid, (f.get("release_at") or [""])[0])
        elif action == "flags":
            _c, err = set_flags(
                cid,
                None if "own_server" not in f else f["own_server"][-1],
                None if "own_line" not in f else f["own_line"][-1],
                None if "own_key" not in f else f["own_key"][-1],
            )
            if err == "released":
                return self._send(303, b"", "text/plain", {"location": PREFIX + "/"})
        elif action == "slots":
            payload = {}
            if "line_slot" in f:
                payload["line_slot"] = f["line_slot"][-1]
            if "key_slot" in f and f["key_slot"][-1] != "":
                payload["key_slot"] = f["key_slot"][-1]
            if (f.get("clear_line") or ["0"])[-1] in ("1", "true", "on"):
                payload["clear_line"] = True
            if (f.get("clear_key") or ["0"])[-1] in ("1", "true", "on"):
                payload["clear_key"] = True
            _c, err = set_slots(cid, payload)
        elif action == "spend":
            _c, err = add_spend(cid, (f.get("usd") or [""])[0], "operator")
        else:
            _c, err = set_link(cid, (f.get("slug") or [""])[0])
        if err:
            return self._send(200, render_page(csrf_token(), flash=_flash(err)), "text/html; charset=utf-8")
        return self._send(303, b"", "text/plain", {"location": PREFIX + "/"})


class Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, kind):
        self.kind = kind
        super().__init__(addr, H)


def _serve(host, kind):
    while True:
        try:
            s = Srv((host, PORT), kind)
            log(f"listening {host}:{PORT} ({kind})")
            s.serve_forever()
        except OSError as e:
            log(f"bind {host}:{PORT} failed ({e.strerror}); retry in 15s")
            time.sleep(15)


def serve():
    global API_TOKEN, PROXY_SECRET
    API_TOKEN, PROXY_SECRET = init_data()
    log(f"start dry_run={DRY} data={DATA} agents={AGENTS}")
    threading.Thread(target=watcher_loop, daemon=True, name="watcher").start()
    if BIND_PROXY and BIND_PROXY != BIND_LOCAL:
        threading.Thread(target=_serve, args=(BIND_PROXY, "proxy"), daemon=True, name="proxy").start()
        _serve(BIND_LOCAL, "local")
    else:
        # One socket on 127.0.0.1:8850 serves /manage/ and the local API.
        _serve(BIND_LOCAL or BIND_PROXY, "both")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "provision":
        init_data()
        print(json.dumps({"created": provision_tokens()}))
    elif cmd == "tick":
        API_TOKEN, PROXY_SECRET = init_data()
        print(json.dumps({"alerts": health_tick()}, ensure_ascii=False))
    elif cmd == "render":
        API_TOKEN, PROXY_SECRET = init_data()
        print(render_page(csrf_token()))
    else:
        sys.exit("usage: assistant_manager.py serve|provision|tick|render")
