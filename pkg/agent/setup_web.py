#!/usr/bin/env python3
"""Setup & connections page for the assistant (runs on the owner's own server, behind Caddy HTTPS).
One step per page: 1) identity 2) WhatsApp 3) OpenAI 4) Google 5) done. Afterwards it is the status / reconnect page.
The status page opens support access, shows who can enter the server, and sends a team request only after an explicit consent checkbox.
Self-host mode has no team-request hook. Access: one-time setup link (?t=<token>) -> HttpOnly cookie.
Secrets are written to AGENT_HOME/.env (600) and never shown back."""
import os, re, sys, json, hmac, html, time, hashlib, secrets, subprocess, threading, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("AGENT_HOME") or CODE
os.environ["AGENT_HOME"] = HOME
ENV = os.path.join(HOME, ".env")
TOKEN_FILE = os.path.join(HOME, ".setup_token")
PERSONA = os.path.join(HOME, "persona.txt")
PORT = int(os.environ.get("SETUP_PORT", "8790"))
PREFIX = (os.environ.get("SETUP_BASE_PATH", "") or "").rstrip("/")   # multi-bot host: page lives under /t/<slug>
TENANT = os.environ.get("AGENT_TENANT", "")                           # multi-bot host: restart via a root path unit
BOT_UNIT = f"assistant-bot@{TENANT}" if TENANT else "assistant-bot"
sys.path.insert(0, CODE)

# ---------------- .env helpers ----------------
def env_read():
    d = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    return d

def env_write(updates):
    lines = open(ENV, encoding="utf-8").read().split("\n") if os.path.exists(ENV) else []
    done = set(); out = []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if k in updates: out.append(f"{k}={updates[k]}"); done.add(k)
        else: out.append(line)
    for k, v in updates.items():
        if k not in done: out.append(f"{k}={v}")
    tmp = ENV + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write("\n".join(out).strip("\n") + "\n")
    os.replace(tmp, ENV)
    for k, v in updates.items(): os.environ[k] = str(v)

def one_line(v, n=200):
    return re.sub(r"[\r\n=\"'`$\\]", "", str(v or "")).strip()[:n]

def phone_digits(v):
    d = re.sub(r"\D", "", str(v or ""))
    cc = env_read().get("PHONE_COUNTRY_CODE", "972") or "972"
    if d.startswith("0") and len(d) == 10: d = cc + d[1:]
    return d

# ---------------- auth ----------------
def setup_token():
    try: return open(TOKEN_FILE).read().strip()
    except Exception: return ""
def session_value():
    t = setup_token()
    return hmac.new(t.encode(), b"setup-session", hashlib.sha256).hexdigest() if t else ""

# ---------------- status ----------------
def bridge(path, payload=None, timeout=20, raw=False):
    e = env_read()
    url = (e.get("WA_BRIDGE_URL") or "http://127.0.0.1:8795") + path
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"content-type": "application/json", "x-wa-token": e.get("WA_BRIDGE_TOKEN", "")},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        return b if raw else json.loads(b or b"{}")

def wa_state():
    try: return bridge("/status", timeout=5)
    except Exception as ex: return {"connection": "bridge_down", "lastError": type(ex).__name__}

def google_state():
    try:
        import google_accounts as GA
        GA._load_env()
        return {"client": bool(GA.clients()), "connected": GA.connected()}
    except Exception as ex: return {"client": False, "connected": {}, "error": str(ex)[:200]}

NO_SYSTEM = os.environ.get("SETUP_NO_SYSTEM") == "1"   # tests: never touch systemd / timezone
def service_active(name):
    if NO_SYSTEM: return False
    if TENANT:   # unprivileged tenant user: read the bot's heartbeat instead of asking systemd
        try: return time.time() - os.path.getmtime(os.path.join(HOME, "data", "heartbeat")) < 180
        except Exception: return False
    try: return subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5).stdout.strip() == "active"
    except Exception: return False

def status():
    e = env_read(); w = wa_state(); g = google_state()
    return {
        "identity": bool(e.get("BOT_NAME") and e.get("OWNER_NAME") and e.get("OWNER_PHONE")),
        "whatsapp": w.get("connection") == "open", "whatsapp_state": w.get("connection"),
        "whatsapp_me": ((w.get("me") or {}).get("id") or "").split(":")[0].split("@")[0],
        "openai": bool(e.get("OPENAI_API_KEY")),
        "google_client": g.get("client"), "google": bool(g.get("connected")),
        "google_accounts": {k: v.get("email") for k, v in (g.get("connected") or {}).items()},
        "bot_running": service_active(BOT_UNIT),
        "bot_name": e.get("BOT_NAME", ""),
    }

def restart_bot():
    if NO_SYSTEM: return
    st = status()
    if st["identity"] and st["openai"]:
        if TENANT:   # a root-owned assistant-restart@<slug>.path unit watches this file and (re)starts the bot
            with open(os.path.join(HOME, ".restart"), "w") as fh: fh.write(str(time.time()))
            return
        subprocess.run(["systemctl", "enable", "--now", BOT_UNIT], capture_output=True, timeout=30)
        subprocess.run(["systemctl", "restart", BOT_UNIT], capture_output=True, timeout=30)

# ---------------- UI ----------------
LOGO = {
 "bot": '<svg viewBox="0 0 48 48" width="56" height="56"><rect x="6" y="12" width="36" height="28" rx="10" fill="#6C5CE7"/><circle cx="18" cy="26" r="4" fill="#fff"/><circle cx="30" cy="26" r="4" fill="#fff"/><rect x="22" y="4" width="4" height="9" rx="2" fill="#6C5CE7"/><circle cx="24" cy="4" r="3" fill="#6C5CE7"/></svg>',
 "whatsapp": '<svg viewBox="0 0 48 48" width="56" height="56"><path fill="#25D366" d="M24 4C13 4 4 12.9 4 23.9c0 3.5.9 6.9 2.7 9.9L4 44l10.5-2.7c2.9 1.6 6.2 2.4 9.5 2.4 11 0 20-8.9 20-19.9S35 4 24 4z"/><path fill="#fff" d="M34.6 28.9c-.6-.3-3.4-1.7-3.9-1.9-.5-.2-.9-.3-1.3.3-.4.6-1.5 1.9-1.8 2.2-.3.4-.7.4-1.2.1-.6-.3-2.4-.9-4.6-2.8-1.7-1.5-2.8-3.3-3.2-3.9-.3-.6 0-.9.3-1.2l.9-1c.3-.4.4-.6.6-1 .2-.4.1-.8 0-1.1-.1-.3-1.3-3.1-1.8-4.2-.5-1.1-.9-.9-1.3-1h-1.1c-.4 0-1 .1-1.5.7-.5.6-2 2-2 4.8s2 5.6 2.3 6c.3.4 4 6.2 9.8 8.6 1.4.6 2.4.9 3.3 1.2 1.4.4 2.6.4 3.6.2 1.1-.2 3.4-1.4 3.9-2.7.5-1.3.5-2.5.3-2.7-.1-.2-.5-.4-1.1-.6z"/></svg>',
 "openai": '<svg viewBox="0 0 48 48" width="56" height="56"><rect width="48" height="48" rx="12" fill="#000"/><g fill="none" stroke="#fff" stroke-width="2.6" stroke-linejoin="round"><path d="M24 11.5l10.8 6.25v12.5L24 36.5l-10.8-6.25v-12.5z"/><path d="M24 11.5v12.5l10.8 6.25M24 24l-10.8 6.25M24 24l10.8-6.25M13.2 17.75L24 24"/></g></svg>',
 "google": '<svg viewBox="0 0 48 48" width="56" height="56"><path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.4-.4-3.5z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2c-2 1.5-4.5 2.4-7.2 2.4-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/><path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/></svg>',
 "done": '<svg viewBox="0 0 48 48" width="56" height="56"><circle cx="24" cy="24" r="20" fill="#22C55E"/><path d="M14 25l7 7 13-14" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
}
CSS = """*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Arial,sans-serif;background:#F6F7FB;color:#1F2330;line-height:1.55}
.wrap{max-width:520px;margin:0 auto;padding:18px 16px 40px}.card{background:#fff;border-radius:18px;padding:22px 18px;box-shadow:0 2px 14px rgba(20,30,60,.07)}
h1{font-size:1.35rem;margin:.4em 0 .2em}p{margin:.5em 0}.muted{color:#5B6275;font-size:.95rem}.steps{display:flex;gap:6px;justify-content:center;margin:4px 0 14px}
.dot{width:30px;height:6px;border-radius:4px;background:#D9DCE6}.dot.on{background:#6C5CE7}.dot.ok{background:#22C55E}
label{display:block;font-weight:600;margin:14px 0 4px}input,textarea,select{width:100%;font-size:16px;padding:12px;border:1px solid #CFD3DE;border-radius:12px;background:#fff}
input[type=checkbox]{width:auto;padding:0;margin-inline-end:8px}
textarea{min-height:90px}input[dir=ltr]{text-align:left}.btn{display:block;width:100%;text-align:center;font-size:1.05rem;font-weight:700;padding:14px;border-radius:14px;border:0;margin-top:16px;cursor:pointer;text-decoration:none}
.pri{background:#6C5CE7;color:#fff}.sec{background:#EEF0F7;color:#1F2330}.ok{color:#15803D;font-weight:700}.bad{color:#B91C1C;font-weight:700}
.status{display:inline-block;padding:4px 10px;border-radius:999px;font-size:.9rem;font-weight:700}.s-ok{background:#DCFCE7;color:#15803D}.s-wait{background:#FEF3C7;color:#92400E}
ol{padding-inline-start:20px}li{margin:6px 0}.code{font-family:ui-monospace,monospace;font-size:2rem;letter-spacing:.25em;text-align:center;direction:ltr;background:#F1F0FF;border-radius:12px;padding:14px;margin:12px 0}
.row{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #EEF0F5}.row:last-child{border:0}.grow{flex:1}.small{font-size:.85rem}
img.qr{display:block;margin:12px auto;width:260px;max-width:100%;border-radius:12px;background:#fff}.only-desktop{display:none}@media(min-width:760px){.only-desktop{display:block}.only-mobile{display:none}}
.msg{background:#FEF2F2;color:#991B1B;border-radius:12px;padding:10px;margin-top:10px}.note{background:#F1F5FF;border-radius:12px;padding:10px 12px;margin-top:10px;font-size:.93rem}
.need{background:#FFF8E6;border-radius:12px;padding:10px 12px;margin:10px 0;font-size:.93rem}.need b{display:block;margin-bottom:2px}
.why{font-size:.93rem;color:#3B4257;margin:8px 0}.hint{font-size:.85rem;color:#5B6275;margin:4px 0 0}
h2{font-size:1.05rem;margin:20px 0 4px}details{background:#F6F7FB;border-radius:12px;padding:10px 12px;margin-top:12px;font-size:.93rem}summary{cursor:pointer;font-weight:700}
.part{border-top:1px solid #EEF0F5;margin-top:16px;padding-top:4px}
.vid{margin:0 0 14px;text-align:center}.vid video{display:block;width:100%;max-width:300px;aspect-ratio:9/16;margin:0 auto;border-radius:14px;background:#000}.vcap{margin:0 0 6px;font-weight:700;font-size:.95rem}"""
STEPS = ["identity", "whatsapp", "openai", "google", "done"]
VIDEOS = os.path.join(CODE, "videos")            # explainer videos (non-secret, served without the setup token)
VIDEO_FOR = {"identity": "details", "whatsapp": "whatsapp", "openai": "openai", "google": "google"}

def video_block(name):
    if not name or not os.path.isfile(os.path.join(VIDEOS, name + ".mp4")): return ""
    poster = f' poster="/videos/{name}.jpg"' if os.path.isfile(os.path.join(VIDEOS, name + ".jpg")) else ""
    return (f'<div class="vid"><p class="vcap">▶️ צפו בהסבר קצר</p>'
            f'<video controls playsinline preload="none"{poster} src="/videos/{name}.mp4"></video></div>')
ESIM_019_URL = "https://019mobile.co.il/%D7%94%D7%A8%D7%A9%D7%9E%D7%AA-%D7%9C%D7%A7%D7%95%D7%97/?pack_id=10576&pack_name=%D7%97%D7%91%D7%99%D7%9C%D7%AA+%D7%A2%D7%A9%D7%A8&pack_desc=10+%E2%82%AA+%7C+100mb+%D7%92%D7%9C%D7%99%D7%A9%D7%94%2C100+%D7%93%D7%A7+100+%D7%94%D7%95%D7%93%D7%A2%D7%95%D7%AA%2C+%D7%9E%D7%97%D7%99%D7%A8+%D7%A7%D7%91%D7%95%D7%A2+%D7%9C%D7%9B%D7%9C+%D7%94%D7%97%D7%99%D7%99%D7%9D"   # direct signup link for 019 "חבילת עשר" (replace this placeholder)
NEED = lambda t, items: f'<div class="need"><b>{t}</b>{items}</div>'

def page(title, body, step=None, st=None):
    dots = ""
    if step:
        st = st or status()
        okmap = {"identity": st["identity"], "whatsapp": st["whatsapp"], "openai": st["openai"], "google": st["google"], "done": False}
        dots = '<div class="steps">' + "".join(f'<span class="dot {"ok" if okmap[s] else ("on" if s == step else "")}"></span>' for s in STEPS) + "</div>"
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{html.escape(title)} · מתאימים את העוזר עבורך</title><style>{CSS}</style></head><body><div class="wrap">{dots}<div class="card">{video_block(VIDEO_FOR.get(step)) if step else ""}{body}</div></div></body></html>"""

def badge(ok, wait="ממתין"):
    return '<span class="status s-ok">✅ מחובר</span>' if ok else f'<span class="status s-wait">{wait}</span>'

def next_step(st):
    for s in STEPS[:-1]:
        if not st[s if s != "identity" else "identity"]: return s
    return "done"

def view_identity(st, err=""):
    e = env_read()
    persona = open(PERSONA, encoding="utf-8").read() if os.path.exists(PERSONA) else ""
    v = lambda k, d="": html.escape(e.get(k, d))
    return page("הגדרת הבוט", f"""{LOGO['bot']}<h1>שלום! בוא נכיר את העוזר שלך</h1>
{NEED('מה צריך ביד', 'רק את מספר הטלפון שלך.')}
<p class="why"><b>למה?</b> ככה העוזר יודע איך קוראים לו, מי הבעלים שלו ואיפה אתה גר. אפשר לשנות הכל אחר כך.</p>
<form method="post" action="/step/identity">
<label>שם הבוט</label><input name="BOT_NAME" required maxlength="30" value="{v('BOT_NAME')}" placeholder="למשל: רובי"><p class="hint">השם שבו תקרא לו בהודעות.</p>
<label>אופי וסגנון (לא חובה)</label><textarea name="persona" maxlength="2000" placeholder="למשל: חם, מצחיק, קצר ולעניין. פונה אליי בשם.">{html.escape(persona)}</textarea><p class="hint">איך תרצה שידבר איתך. אפשר להשאיר ריק.</p>
<label>השם שלך</label><input name="OWNER_NAME" required maxlength="30" value="{v('OWNER_NAME')}">
<label>מספר הוואטסאפ שלך (שלך, לא של הבוט)</label><input name="OWNER_PHONE" required dir="ltr" inputmode="tel" value="{v('OWNER_PHONE')}" placeholder="05XXXXXXXX"><p class="hint">המספר הרגיל שלך. ככה העוזר יודע שזה אתה כשאתה כותב לו.</p>
<label>בן/בת זוג שגם יכולים לנהל את הבוט (לא חובה)</label><input name="PARTNER_NAME" maxlength="30" value="{v('PARTNER_NAME')}" placeholder="שם">
<input name="PARTNER_PHONE" dir="ltr" inputmode="tel" value="{v('PARTNER_PHONE')}" placeholder="מספר וואטסאפ" style="margin-top:8px"><p class="hint">מישהו נוסף שהעוזר יקשיב לו כמו לך. אפשר להשאיר ריק.</p>
<label>עיר מגורים (למזג אוויר והמלצות)</label><input name="HOME_AREA" maxlength="40" value="{v('HOME_AREA')}">
<label>אזור זמן</label><input name="TIMEZONE" dir="ltr" value="{v('TIMEZONE', 'Asia/Jerusalem')}"><p class="hint">גר בישראל? אל תיגע, זה כבר נכון.</p>
{f'<div class="msg">{html.escape(err)}</div>' if err else ''}
<button class="btn pri">שמור והמשך ←</button></form>""", "identity", st)

def view_whatsapp(st):
    me = st.get("whatsapp_me")
    if st["whatsapp"]:
        body = f"""{LOGO['whatsapp']}<h1>וואטסאפ {badge(True)}</h1><p>הבוט מחובר למספר <bdi dir="ltr"><b>+{html.escape(me)}</b></bdi>.</p>
<a class="btn pri" href="/step/openai">המשך ←</a>
<form method="post" action="/api/wa/logout" onsubmit="return confirm('לנתק את הוואטסאפ של הבוט?')"><button class="btn sec">נתק וחבר מספר אחר</button></form>"""
        return page("וואטסאפ", body, "whatsapp", st)
    body = f"""{LOGO['whatsapp']}<h1>חיבור וואטסאפ {badge(False, 'לא מחובר')}</h1>
{NEED('מה צריך ביד', 'הטלפון שלך. לקו חדש: גם תעודת זהות וכרטיס אשראי.')}
<p class="why"><b>למה?</b> לעוזר צריך מספר וואטסאפ משלו, כמו לכל איש קשר. ככה תכתוב לו כמו לחבר, והוא גם יוכל להיות בקבוצות.</p>
<p class="hint">לא משתמשים במספר הפרטי שלך. המספר הפרטי ממשיך לעבוד בדיוק כמו היום.</p>
<p class="hint">כבר יש לך מספר נוסף עם וואטסאפ? דלג ישר ל<b>חלק 4</b>.</p>

<div class="part"><h2>1. מוציאים קו לעוזר (eSIM)</h2>
<p class="why"><b>מה זה eSIM?</b> סים דיגיטלי. אין כרטיס להכניס, רק סורקים קוד בטלפון. הוא עובד ליד הקו הרגיל שלך, באותו טלפון.</p>
<p class="why">אנחנו ממליצים על <b>019, «חבילת עשר»</b>: 10 ₪ לחודש, מחיר קבוע לכל החיים (100 דקות, 100 SMS, 100MB). לעוזר זה מספיק.</p>
<a class="btn pri" href="{html.escape(ESIM_019_URL)}" target="_blank" rel="noopener">הוצא eSIM ב-019 (10 ₪ לחודש) ↗</a>
<ol class="small"><li>בטופס בוחרים <b>מספר חדש</b> ומסמנים <b>eSIM</b>.</li><li>ממלאים תעודת זהות וכרטיס אשראי.</li><li>019 שולחים לך קוד QR להפעלת הקו.</li></ol>
<details><summary>הכפתור פתח את דף הבית של 019?</summary>גוללים ל-<b>«חבילות נוספות»</b>, מוצאים את <b>«חבילת עשר»</b> ולוחצים <b>«להצטרפות באתר»</b>.</details></div>

<div class="part"><h2>2. מפעילים את ה-eSIM בטלפון</h2>
<ol class="small"><li>פותחים את <b>הגדרות</b> הטלפון.</li><li>כותבים בחיפוש למעלה <b>eSIM</b> ובוחרים בהוספת eSIM.</li><li>סורקים את הקוד ש-019 שלחו, ומחכים שהקו יופעל.</li></ol>
<p class="hint">הקוד של 019 פתוח אצלך בטלפון? בדרך כלל אפשר גם להקליד אותו ידנית במסך ההוספה, או לפתוח אותו במחשב ולסרוק מהמסך.</p></div>

<div class="part"><h2>3. מתקינים וואטסאפ לעוזר</h2>
<p class="why">המספר הפרטי שלך כבר תפוס בוואטסאפ הרגיל, אז לעוזר מתקינים את <b>WhatsApp Business</b> (האייקון הירוק עם B), מחנות האפליקציות.</p>
<a class="btn sec" href="https://play.google.com/store/apps/details?id=com.whatsapp.w4b" target="_blank" rel="noopener">WhatsApp Business לאנדרואיד ↗</a>
<a class="btn sec" href="https://apps.apple.com/app/whatsapp-business/id1386412706" target="_blank" rel="noopener">WhatsApp Business לאייפון ↗</a>
<ol class="small"><li>פותחים את WhatsApp Business ונרשמים עם <b>המספר החדש</b>.</li><li>מקלידים את הקוד שמגיע ב-SMS לקו החדש.</li><li>בשם הפרופיל כותבים את שם העוזר.</li></ol>
<p class="hint">כבר יש לך וואטסאפ ביזנס? אז מתקינים לעוזר את הוואטסאפ הרגיל: <a href="https://play.google.com/store/apps/details?id=com.whatsapp" target="_blank" rel="noopener">אנדרואיד ↗</a> · <a href="https://apps.apple.com/app/whatsapp-messenger/id310633997" target="_blank" rel="noopener">אייפון ↗</a></p></div>

<div class="part"><h2>4. מחברים את העוזר למספר</h2>
<div class="only-mobile"><p><b>מהטלפון:</b> מקבלים כאן קוד, ומקלידים אותו בוואטסאפ של העוזר.</p>
<label>המספר של העוזר (המספר החדש)</label><input id="ph" dir="ltr" inputmode="tel" placeholder="05XXXXXXXX">
<button class="btn pri" onclick="pair()">קבל קוד חיבור</button><div id="code"></div>
<ol class="small muted"><li>פותחים את הוואטסאפ של העוזר (WhatsApp Business).</li><li>באנדרואיד ⋮ למעלה, באייפון <b>הגדרות</b> ← <b>מכשירים מקושרים</b> ← <b>קישור מכשיר</b>.</li><li>בוחרים קישור <b>באמצעות מספר טלפון</b> (בתחתית המסך) ומקלידים את הקוד.</li><li>חוזרים לדף הזה, והוא יתעדכן לבד.</li></ol></div>
<div class="only-desktop"><p><b>מהמחשב:</b> סורקים את הקוד עם הוואטסאפ של העוזר.</p>
<ol class="small muted"><li>בטלפון פותחים את הוואטסאפ של העוזר (WhatsApp Business).</li><li>באנדרואיד ⋮ למעלה, באייפון <b>הגדרות</b> ← <b>מכשירים מקושרים</b> ← <b>קישור מכשיר</b>.</li><li>מכוונים את המצלמה לקוד שכאן.</li></ol>
<img class="qr" id="qr" alt="QR" src="/api/wa/qr.png?{int(time.time())}"></div>
<details><summary>נתקעת?</summary><ul>
<li><b>אין אצלי אפשרות eSIM</b>: הטלפון כנראה ישן מדי. אפשר לבקש מ-019 סים רגיל ולשים בטלפון ישן בבית.</li>
<li><b>ה-SMS מוואטסאפ לא מגיע</b>: בדוק שהקו החדש פעיל (יש קליטה עליו). אפשר לבחור "התקשרו אליי" במקום SMS.</li>
<li><b>הקוד לא עובד</b>: הוא תקף לזמן קצר. לחץ שוב "קבל קוד חיבור" והקלד מהר.</li>
<li><b>הקלדתי את המספר הפרטי בטעות</b>: כאן צריך את <b>המספר של העוזר</b>, לא את שלך.</li>
<li><b>הקוד לא מופיע במחשב</b>: רענן את הדף.</li>
</ul></details></div>
<p class="muted small" id="st">מצב: {html.escape(str(st.get('whatsapp_state')))}</p>
<script>
async function pair(){{const p=document.getElementById('ph').value;const r=await fetch('/api/wa/pair',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{phone:p}})}});const j=await r.json();
document.getElementById('code').innerHTML=j.ok?'<div class="code">'+j.code+'</div>':'<div class="msg">'+(j.error||'שגיאה')+'</div>';}}
setInterval(async()=>{{try{{const j=await (await fetch('/api/status')).json();document.getElementById('st').textContent='מצב: '+j.whatsapp_state;if(j.whatsapp)location.href='/step/whatsapp';
const q=document.getElementById('qr');if(q&&window.innerWidth>=760)q.src='/api/wa/qr.png?'+Date.now();}}catch(e){{}}}},4000);
</script>"""
    return page("וואטסאפ", body, "whatsapp", st)

def view_openai(st, err=""):
    e = env_read()
    ok = st["openai"]
    return page("OpenAI", f"""{LOGO['openai']}<h1>המוח: OpenAI {badge(ok, 'חסר מפתח')}</h1>
{NEED('מה צריך ביד', 'מייל (או חשבון Google) וכרטיס אשראי.')}
<p class="why"><b>למה?</b> OpenAI (החברה של ChatGPT) נותנת לעוזר את ה"מוח". הוא חושב שם, אבל הכל נשמר אצלך.</p>
<p class="why"><b>מה זה מפתח (API key)?</b> כמו כרטיס חכם: שורה ארוכה של אותיות שמאפשרת לעוזר להשתמש במוח על החשבון שלך. אתה משלם ל-OpenAI ישירות, רק לפי כמה שהעוזר עובד.</p>
<p class="hint">מנוי ChatGPT Plus לא עוזר כאן. זה חשבון נפרד עם תשלום נפרד.</p>
<div class="part"><h2>1. טוענים קרדיט</h2>
<ol class="small"><li>לוחצים על הכפתור ונרשמים או נכנסים (אפשר עם Google).</li><li>לוחצים <b>Add payment details</b> ומוסיפים כרטיס.</li><li>טוענים סכום קטן, 10$ מספיקים להתחלה.</li></ol>
<a class="btn sec" href="https://platform.openai.com/settings/organization/billing/overview" target="_blank" rel="noopener">1. דף התשלום ב-OpenAI ↗</a>
<p class="hint">כבר יש כרטיס וחסר קרדיט? באותו דף: <b>Add to credit balance</b>.</p></div>
<div class="part"><h2>2. יוצרים מפתח</h2>
<ol class="small"><li>לוחצים על הכפתור.</li><li>לוחצים <b>Create new secret key</b>, נותנים שם כלשהו ולוחצים <b>Create secret key</b>.</li><li>לוחצים <b>Copy</b>. המפתח מוצג רק פעם אחת.</li></ol>
<a class="btn sec" href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">2. דף המפתחות ב-OpenAI ↗</a></div>
<div class="part"><h2>3. מדביקים כאן</h2>
<form method="post" action="/step/openai"><label>הדבק את המפתח (מתחיל ב-sk-)</label>
<input name="key" dir="ltr" autocomplete="off" placeholder="{'נשמר מפתח. הדבק חדש רק כדי להחליף' if ok else 'sk-...'}">
<label>תקרת עלות חודשית בדולרים</label><input name="ceiling" dir="ltr" inputmode="numeric" value="{html.escape(e.get('COST_CEILING_USD', '30'))}">
<p class="hint">ביטוח: אם העוזר מגיע לסכום הזה בחודש, הוא עוצר. אפשר להשאיר כמו שזה.</p>
{f'<div class="msg">{html.escape(err)}</div>' if err else ''}
<button class="btn pri">בדוק ושמור ←</button></form></div>
<details><summary>נתקעת?</summary><ul>
<li><b>"המפתח לא נראה תקין"</b>: העתק שוב עם כפתור Copy. המפתח מתחיל ב-<span dir="ltr">sk-</span> וארוך מאוד.</li>
<li><b>"המפתח נדחה"</b>: אולי מחקת אותו או שהועתק חלקית. צור מפתח חדש.</li>
<li><b>מבקשים אימות טלפון</b>: אמת עם המספר הפרטי שלך.</li>
<li><b>הכל נשמר אבל העוזר לא עונה</b>: כנראה אין קרדיט. היכנס לדף התשלום וטען.</li>
</ul></details>
{'<a class="btn sec" href="/step/google">המשך בלי לשנות</a>' if ok else ''}""", "openai", st)

def view_google(st, err=""):
    g = google_state(); e = env_read()
    accts = "".join(f"<p class='ok'>✅ {html.escape(k)}: {html.escape(v or '')}</p>" for k, v in st["google_accounts"].items())
    if not st["google_client"]:
        body = f"""{LOGO['google']}<h1>Google: מייל, יומן ודרייב</h1>
{NEED('מה צריך ביד', 'חשבון Google (ג׳ימייל) שלך. נוח יותר ממחשב. אפשר לדלג ולחזור אחר כך.')}
<p class="why"><b>למה?</b> כדי שהעוזר יקרא מיילים, יקבע ביומן וימצא קבצים בדרייב.</p>
<p class="why"><b>למה כמה צעדים?</b> במקום לתת גישה לחברה זרה, יוצרים "מפתח חיבור" משלך אצל Google. ככה הגישה למיילים שלך רק אצלך.</p>
<a class="btn pri" href="/guide/google">פתח את המדריך צעד-אחר-צעד ←</a>
<p class="hint">בסוף המדריך מקבלים שני קודים (Client ID ו-Client secret) ומדביקים אותם כאן:</p>
<form method="post" action="/step/google-client"><label>Client ID</label><input name="cid" dir="ltr" autocomplete="off" placeholder="....apps.googleusercontent.com">
<label>Client secret</label><input name="sec" dir="ltr" autocomplete="off" placeholder="GOCSPX-...">
{f'<div class="msg">{html.escape(err)}</div>' if err else ''}<button class="btn pri">שמור ←</button></form>
<details><summary>נתקעת?</summary><ul>
<li><b>"Client ID צריך להסתיים ב-apps.googleusercontent.com"</b>: הדבקת משהו אחר. העתק שוב את ה-Client ID המלא.</li>
<li><b>לא מוצא את ה-Client secret</b>: ב-<a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noopener">Clients ↗</a> לוחצים על החיבור שיצרת. אם הוא לא מוצג, יוצרים חיבור חדש (Desktop app) ומעתיקים מיד.</li>
</ul></details>
<a class="btn sec" href="/step/done">דלג בינתיים</a>"""
        return page("Google", body, "google", st)
    import google_accounts as GA
    GA._load_env()
    try: url = GA.auth_url("personal")
    except Exception as ex: url = ""; err = err or str(ex)
    desktop = GA.preferred_client() == "desktop"
    body = f"""{LOGO['google']}<h1>Google {badge(st['google'], 'לא מחובר')}</h1>{accts}
<p class="why">עוד צעד אחד: מאשרים לעוזר להיכנס לחשבון. זה מה שתראה:</p>
<ol class="small"><li>בוחרים את חשבון ה-Google שלך.</li>
<li>מופיע מסך אזהרה <b>"Google hasn't verified this app"</b>. זה בסדר, זו האפליקציה שאתה יצרת. לוחצים <b>Advanced</b> ואז <b>Go to</b> + שם האפליקציה.</li>
<li>במסך ההרשאות מסמנים את כל התיבות (או "Select all") ולוחצים <b>Continue</b>.</li></ol>
<a class="btn pri" href="{html.escape(url)}" target="_blank" rel="noopener">{'חבר מחדש' if st['google'] else 'חבר את Google'} ↗</a>
{f'''<div class="note"><b>חשוב:</b> בסוף Google יעביר אותך לדף שלא נטען ("This site can't be reached"), עם כתובת שמתחילה ב-<bdi dir="ltr"><b>http://localhost:8765</b></bdi>. <b>זה צפוי, לא תקלה.</b><ol class="small"><li>לוחצים על שורת הכתובת למעלה.</li><li>מסמנים הכל ומעתיקים.</li><li>חוזרים ללשונית הזאת ומדביקים כאן.</li></ol></div>
<form method="post" action="/step/google-paste"><input name="url" dir="ltr" autocomplete="off" placeholder="http://localhost:8765/?state=...&code=..."><button class="btn pri">סיים חיבור ←</button></form>''' if desktop else ''}
{f'<div class="msg">{html.escape(err)}</div>' if err else ''}
<details><summary>נתקעת?</summary><ul>
<li><b>"Access blocked" / "access_denied"</b>: באפליקציה שיצרת לא הוספת את המייל שלך, או שלא לחצת Publish app. ראה שלב 4 ב<a href="/guide/google">מדריך</a>.</li>
<li><b>אין כפתור Advanced</b>: חפש קישור קטן בתחתית מסך האזהרה.</li>
<li><b>הדבקתי ויש שגיאה</b>: לחץ שוב "חבר את Google" והעתק את <b>כל</b> הכתובת, מההתחלה עד הסוף.</li>
<li><b>טעיתי ב-Client ID</b>: הכפתור "החלף Client ID / secret" בתחתית.</li>
</ul></details>
<a class="btn sec" href="/step/done">{'המשך ←' if st['google'] else 'דלג בינתיים'}</a>
<form method="post" action="/step/google-reset" onsubmit="return confirm('למחוק את מפתח החיבור ולהתחיל מחדש?')"><button class="btn sec small">החלף Client ID / secret</button></form>"""
    return page("Google", body, "google", st)

def view_done(st):
    rows = [("whatsapp", "וואטסאפ", st["whatsapp"], "/step/whatsapp"), ("openai", "OpenAI", st["openai"], "/step/openai"),
            ("google", "Google", st["google"], "/step/google"), ("bot", "הגדרות הבוט", st["identity"], "/step/identity")]
    lst = "".join(f'<div class="row">{LOGO[k].replace("56", "34")}<div class="grow"><b>{t}</b><br>{badge(ok, "לא מחובר")}</div><a href="{h}">{"שנה" if ok else "חבר"}</a></div>' for k, t, ok, h in rows)
    me = st.get("whatsapp_me")
    run = '<p class="ok">✅ הבוט פועל</p>' if st["bot_running"] else '<p class="bad">הבוט עוד לא פועל — חסר חיבור (וואטסאפ / OpenAI / פרטים)</p>'
    return page("החיבורים שלי", f"""{LOGO['done']}<h1>{html.escape(st.get('bot_name') or 'הבוט')} — החיבורים</h1>{run}{lst}
<div class="note"><b>איך מתחילים?</b><ol class="small"><li>שמור את המספר של העוזר{f' <bdi dir="ltr"><b>+{html.escape(me)}</b></bdi>' if me else ''} באנשי הקשר שלך.</li><li>מהוואטסאפ הפרטי שלך שלח לו הודעה, למשל: "היי, מה אתה יודע לעשות?"</li><li>רוצה אותו בקבוצה? מוסיפים אותו כמו כל איש קשר.</li></ol></div>
<p class="muted small">שמור את הדף הזה במועדפים. דרכו מחברים מחדש כל דבר.</p>
{management_block()}""", None, st)

GUIDE = """<h1>מדריך: מפתח חיבור ל-Google</h1>
<div class="need"><b>נוח יותר ממחשב.</b>כל קישור נפתח בלשונית חדשה, ואחרי כל שלב חוזרים לכאן.</div>
<p class="why">הכל באנגלית אצל Google. לא צריך להבין הכל, רק ללחוץ על מה שכתוב כאן.</p>
<h2>1. יוצרים פרויקט</h2>
<ol class="small"><li>פותחים את <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">יצירת פרויקט ↗</a> ונכנסים עם החשבון שלך.</li><li>בשם כותבים <code>my-assistant</code> ולוחצים <b>Create</b>.</li><li>מחכים כמה שניות שהפרויקט ייווצר.</li></ol>
<h2>2. מדליקים 3 שירותים</h2>
<ol class="small"><li>פותחים כל קישור: <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener">Gmail API ↗</a>, <a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" rel="noopener">Google Calendar API ↗</a>, <a href="https://console.cloud.google.com/apis/library/drive.googleapis.com" target="_blank" rel="noopener">Google Drive API ↗</a>.</li><li>בודקים שלמעלה מופיע הפרויקט <code>my-assistant</code>.</li><li>לוחצים <b>Enable</b> בכל אחד מהשלושה.</li></ol>
<h2>3. נותנים שם לאפליקציה</h2>
<ol class="small"><li>פותחים את <a href="https://console.cloud.google.com/auth/branding" target="_blank" rel="noopener">Google Auth Platform ↗</a> ולוחצים <b>Get started</b>.</li><li>שם אפליקציה: כל שם, ובמייל בוחרים את המייל שלך.</li><li>ב-Audience בוחרים <b>External</b>.</li><li>שוב המייל שלך, מסמנים הסכמה ולוחצים <b>Create</b>.</li></ol>
<h2>4. פותחים את האפליקציה לשימוש</h2>
<ol class="small"><li>בתפריט בצד לוחצים <b>Audience</b>.</li><li>תחת Test users מוסיפים את המייל שלך.</li><li>לוחצים <b>Publish app</b> ומאשרים. בלי זה Google מנתק את החיבור כל 7 ימים.</li></ol>
<h2>5. יוצרים את מפתח החיבור</h2>
<ol class="small"><li>פותחים את <a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noopener">Clients ↗</a> ולוחצים <b>Create client</b>.</li><li>ב-Application type בוחרים <b>Desktop app</b> ולוחצים <b>Create</b>.</li><li>מעתיקים מיד את ה-<b>Client ID</b> ואת ה-<b>Client secret</b>.</li><li>חוזרים לדף הקודם ומדביקים אותם.</li></ol>
<details><summary>נתקעת?</summary><ul>
<li><b>לא רואה את הפרויקט</b>: למעלה בצד יש תפריט בחירת פרויקט. בוחרים <code>my-assistant</code>.</li>
<li><b>מבקשים כרטיס אשראי</b>: לשלבים האלה לא צריך. אם מופיע חלון כזה, סוגרים אותו וממשיכים.</li>
<li><b>הכפתורים במקום אחר</b>: Google משנים לפעמים את המסכים. מחפשים את אותה מילה באנגלית בדף.</li>
</ul></details>
<p class="muted small">האפליקציה שייכת רק לך, והחיבור נשמר רק בשרת שלך. מסך "האפליקציה לא אומתה" צפוי, כי זו אפליקציה שיצרת בעצמך.</p>
<a class="btn pri" href="/step/google">חזרה לחיבור ←</a>"""

# ---------------- support access, server keys, team requests ----------------
# Grants and the team-request hook live on the friend's server.
# Host SSH should run `python3 setup_web.py support-login` when a support session starts,
# and should allow that session only while support_is_open() is true.
# Opening a grant stores local state only. It does not send anything out.
# Self-host (SELF_HOST=1, ASSISTANT_MODE=self-host/own-server, or no manager token) has no team hook.

_SUPPORT_LOCK = threading.Lock()
_SSH_LOCK = threading.Lock()
TEAM_SENDER = None
ALLOWED_HOURS = (1, 4, 24, 72)
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
_SUMMARY_MAX = 300

def agent_home():
    return os.environ.get("AGENT_HOME") or HOME

def _clock(now):
    return time.time() if now is None else float(now)

def _support_path(home):
    return os.path.join(home, "support_access.json")

def _empty_support():
    return {"grant": None, "logins": []}

def _load_support(home):
    try:
        with open(_support_path(home), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return _empty_support()
    if not isinstance(data, dict):
        return _empty_support()
    grant = data.get("grant")
    if grant is not None and not isinstance(grant, dict):
        grant = None
    logins = [x for x in (data.get("logins") or []) if isinstance(x, dict)]
    return {"grant": grant, "logins": logins[-200:]}

def _save_support(home, data):
    os.makedirs(home, exist_ok=True)
    path = _support_path(home)
    tmp = path + ".tmp"
    payload = {"grant": data.get("grant"), "logins": (data.get("logins") or [])[-200:]}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)

def _grant_open(data, now):
    grant = data.get("grant")
    if not isinstance(grant, dict) or grant.get("closed_at"):
        return False
    try:
        return float(grant.get("expires_at") or 0) > float(now)
    except (TypeError, ValueError):
        return False

def _expire_support(home, now):
    data = _load_support(home)
    grant = data.get("grant")
    if isinstance(grant, dict) and not grant.get("closed_at"):
        try:
            exp = float(grant.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp <= now:
            grant["closed_at"] = exp or now
            grant["close_reason"] = "expired"
            data["grant"] = grant
            _save_support(home, data)
    return data

def _public_support(data, now):
    grant = data.get("grant") if isinstance(data.get("grant"), dict) else None
    active = None
    if grant and _grant_open(data, now):
        active = {
            "id": grant.get("id") or "",
            "hours": grant.get("hours"),
            "opened_at": grant.get("opened_at"),
            "expires_at": grant.get("expires_at"),
        }
    logins = []
    for item in (data.get("logins") or [])[-30:]:
        logins.append({
            "at": item.get("at"),
            "kind": "support",
            "result": "ok" if item.get("result") == "ok" else "denied",
            "grant_id": str(item.get("grant_id") or ""),
        })
    return {"open": active is not None, "grant": active, "grants": [active] if active else [], "logins": logins}

def _parse_hours(value):
    if isinstance(value, bool):
        raise ValueError("hours")
    if isinstance(value, int):
        hours = value
    else:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{1,2}", text):
            raise ValueError("hours")
        hours = int(text)
    if hours not in ALLOWED_HOURS:
        raise ValueError("hours")
    return hours

def support_view(home=None, now=None):
    home = home or agent_home()
    now = _clock(now)
    with _SUPPORT_LOCK:
        data = _expire_support(home, now)
        return _public_support(data, now)

def support_is_open(home=None, now=None):
    return bool(support_view(home, now).get("open"))

def open_support(hours, home=None, now=None):
    home = home or agent_home()
    now = _clock(now)
    try:
        hours = _parse_hours(hours)
    except ValueError:
        return {"ok": False, "error": "hours"}
    with _SUPPORT_LOCK:
        data = _expire_support(home, now)
        if _grant_open(data, now):
            return {"ok": False, "error": "already-open", "view": _public_support(data, now)}
        data["grant"] = {
            "id": secrets.token_hex(8),
            "hours": hours,
            "opened_at": now,
            "expires_at": now + hours * 3600,
            "closed_at": None,
            "close_reason": "",
        }
        _save_support(home, data)
        return {"ok": True, "view": _public_support(data, now)}

def close_support(home=None, now=None):
    home = home or agent_home()
    now = _clock(now)
    with _SUPPORT_LOCK:
        data = _expire_support(home, now)
        grant = data.get("grant")
        if isinstance(grant, dict) and _grant_open(data, now):
            grant["closed_at"] = now
            grant["close_reason"] = "closed"
            data["grant"] = grant
            _save_support(home, data)
        return {"ok": True, "view": _public_support(data, now)}

def record_support_login(home=None, now=None):
    """Append one support login: time, grant id, and result. Never chat content.

    Integration point: the host calls this when a support SSH session starts.
    """
    home = home or agent_home()
    now = _clock(now)
    with _SUPPORT_LOCK:
        data = _expire_support(home, now)
        grant = data.get("grant") if isinstance(data.get("grant"), dict) else {}
        entry = {
            "at": now,
            "kind": "support",
            "result": "ok" if _grant_open(data, now) else "denied",
            "grant_id": str(grant.get("id") or ""),
        }
        data["logins"] = (data.get("logins") or [])[-199:] + [entry]
        _save_support(home, data)
        return dict(entry)

def resolve_keys_path():
    return (os.environ.get("SSH_AUTHORIZED_KEYS") or "").strip() or os.path.expanduser("~/.ssh/authorized_keys")

def _fingerprint(key_type, material):
    return hashlib.sha256(f"{key_type} {material}".encode("utf-8")).hexdigest()

_KEY_RE = re.compile(
    r"(ssh-ed25519|ssh-rsa|ssh-dss|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|"
    r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)\s+(\S+)(?:\s+(.*))?$"
)

def _parse_key_line(raw):
    text = (raw or "").strip()
    if not text or text.startswith("#"):
        return None
    match = _KEY_RE.search(text)
    if not match:
        return None
    return match.group(1), match.group(2), (match.group(3) or "").strip()[:80]

def list_ssh_keys(path=None):
    path = path or resolve_keys_path()
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return []
    keys = []
    for line in text.splitlines():
        parsed = _parse_key_line(line)
        if not parsed:
            continue
        key_type, material, comment = parsed
        keys.append({
            "fingerprint": _fingerprint(key_type, material),
            "type": key_type,
            "comment": comment,
        })
    return keys

def delete_ssh_key(fingerprint, path=None):
    path = path or resolve_keys_path()
    if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint or "")):
        return {"ok": False, "error": "bad-fingerprint"}
    with _SSH_LOCK:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return {"ok": False, "error": "missing"}
        except Exception:
            return {"ok": False, "error": "read"}
        kept = []
        removed = 0
        for line in text.splitlines():
            parsed = _parse_key_line(line)
            if parsed and _fingerprint(parsed[0], parsed[1]) == fingerprint:
                removed += 1
                continue
            kept.append(line)
        if removed == 0:
            return {"ok": False, "error": "not-found"}
        body = ("\n".join(kept) + "\n") if kept else ""
        tmp = path + ".tmp"
        try:
            mode = os.stat(path).st_mode & 0o777
        except Exception:
            mode = 0o600
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return {"ok": True, "removed": removed}

def _self_host():
    if (os.environ.get("SELF_HOST") or "").strip() == "1":
        return True
    mode = (os.environ.get("ASSISTANT_MODE") or "").strip().lower()
    return mode in {"self-host", "selfhost", "own-server"}

def _manager_pair():
    if _self_host():
        return "", ""
    url = (os.environ.get("MANAGER_URL") or "").strip().rstrip("/")
    tok = (os.environ.get("MANAGER_TOKEN") or "").strip()
    token_file = os.path.join(agent_home(), ".manager_token")
    if not tok and os.path.isfile(token_file):
        try:
            tok = open(token_file, encoding="utf-8").read().strip()
        except Exception:
            tok = ""
    if tok and not url and os.path.isfile(token_file):
        url = "http://127.0.0.1:8850"
    if not url or not tok:
        return "", ""
    return url, tok

def team_hook_available():
    url, tok = _manager_pair()
    return bool(url and tok)

def clean_summary(text):
    return " ".join(str(text or "").split())[:_SUMMARY_MAX].strip()

def tenant_slug(home=None):
    raw = (os.environ.get("AGENT_TENANT") or "").strip().lower()
    if _SLUG_RE.fullmatch(raw):
        return raw
    base = os.path.basename(os.path.normpath(home or agent_home())).lower()
    if _SLUG_RE.fullmatch(base):
        return base
    return ""

def _consent_yes(consent):
    return consent is True or str(consent or "").strip().lower() == "yes"

def build_team_payload(summary, consent="yes", home=None):
    """summary is the consented sentence. slug is the tenant id when it is a hosted slug.
    Other form fields, chat text, and files are not copied. None if the hook is absent or consent is missing."""
    if not team_hook_available() or not _consent_yes(consent):
        return None
    text = summary.get("summary") if isinstance(summary, dict) else summary
    text = clean_summary(text)
    if len(text) < 3:
        return None
    payload = {"summary": text}
    slug = tenant_slug(home)
    if slug:
        payload["slug"] = slug
    return payload

def _default_team_sender(payload, opener=None):
    url, tok = _manager_pair()
    if not url or not tok:
        raise RuntimeError("absent")
    body_obj = {"summary": clean_summary((payload or {}).get("summary"))}
    slug = (payload or {}).get("slug") if isinstance(payload, dict) else ""
    if isinstance(slug, str) and _SLUG_RE.fullmatch(slug):
        body_obj["slug"] = slug
    data = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url + "/intake",
        data=data,
        method="POST",
        headers={"content-type": "application/json", "x-manager-token": tok},
    )
    open_fn = opener or urllib.request.urlopen
    with open_fn(req, timeout=10) as resp:
        raw = resp.read(2000)
    try:
        parsed = json.loads(raw or b"{}")
    except Exception as exc:
        raise RuntimeError("bad-response") from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise RuntimeError("not-ok")
    return parsed

def submit_team_request(summary, consent, sender=None, home=None):
    if not team_hook_available():
        return {"ok": False, "sent": False, "absent": True}
    if not _consent_yes(consent):
        return {"ok": False, "sent": False, "absent": False, "error": "לא נשלח. צריך לסמן הסכמה."}
    payload = build_team_payload(summary, consent="yes", home=home)
    if not payload:
        return {"ok": False, "sent": False, "absent": False, "error": "לא נשלח. כתוב משפט אחד."}
    send = sender if sender is not None else TEAM_SENDER
    if send is None:
        send = _default_team_sender
    try:
        send(payload)
    except Exception:
        return {"ok": False, "sent": False, "absent": False, "error": "לא נשלח. אפשר לנסות שוב."}
    return {"ok": True, "sent": True, "absent": False}

def _fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""

def _login_html(logins):
    if not logins:
        return "<p class='muted'>אין כניסות.</p>"
    rows = []
    for item in reversed(list(logins)[-20:]):
        label = "נכנסה תמיכה" if item.get("result") == "ok" else "ניסיון שנדחה"
        when = html.escape(_fmt_time(item.get("at")))
        rows.append(f"<li>{label} · <span dir='ltr'>{when}</span></li>")
    return "<ul>" + "".join(rows) + "</ul>"

def team_block(msg=""):
    if not team_hook_available():
        return ""
    msg = str(msg or "")
    note = f"<p class='ok'>{html.escape(msg)}</p>" if msg.startswith("נשלח") else ""
    err = f"<div class='msg'>{html.escape(msg)}</div>" if msg and not msg.startswith("נשלח") else ""
    return f"""<h2>בקשה לצוות</h2>
<p class="why">כתוב משפט אחד. מה לשנות.</p>
{note}{err}
<form method="post" action="/team/request">
<label>מה לשנות</label>
<textarea name="summary" maxlength="300" required></textarea>
<p><input type="checkbox" name="consent" value="yes"> אני מסכים. יישלח רק המשפט הזה. בלי שיחות.</p>
<button class="btn pri">שלח לצוות</button>
</form>"""

def management_block():
    try:
        view = support_view()
    except Exception:
        view = {"open": False, "logins": []}
    state = "<p class='ok'>הגישה פתוחה.</p>" if view.get("open") else "<p class='muted'>הגישה סגורה.</p>"
    return f"""<h2>תמיכה</h2>
{state}
<a class="btn sec" href="/support">פתח גישה לתמיכה</a>
<a class="btn sec" href="/who">מי יכול להיכנס לשרת שלי?</a>
<h2>כניסות תמיכה</h2>
{_login_html(view.get("logins") or [])}
{team_block()}"""

def view_support(home=None, now=None, error=""):
    home = home or agent_home()
    view = support_view(home, now)
    err = f"<div class='msg'>{html.escape(error)}</div>" if error else ""
    if view["open"]:
        grant = view.get("grant") or {}
        when = html.escape(_fmt_time(grant.get("expires_at")))
        controls = f"""<p class="ok">הגישה פתוחה.</p>
<p>נסגרת לבד ב-<span dir="ltr">{when}</span>.</p>
<form method="post" action="/support/close"><button class="btn pri">סגור גישה</button></form>"""
    else:
        controls = """<p>הגישה סגורה. אנחנו לא נכנסים.</p>
<p class="why">אם אתה רוצה עזרה, בחר כמה זמן. אחר כך זה נסגר לבד.</p>
<form method="post" action="/support/open">
<button class="btn pri" name="hours" value="1">שעה אחת</button>
<button class="btn sec" name="hours" value="4">4 שעות</button>
<button class="btn sec" name="hours" value="24">24 שעות</button>
<button class="btn sec" name="hours" value="72">72 שעות</button>
</form>"""
    body = f"""{LOGO['bot']}<h1>פתח גישה לתמיכה</h1>
{err}{controls}
<h2>כניסות תמיכה</h2>
{_login_html(view["logins"])}
<a class="btn sec" href="/status">חזרה</a>"""
    return page("פתח גישה לתמיכה", body)

def view_who(home=None, now=None, keys_path=None, error=""):
    home = home or agent_home()
    view = support_view(home, now)
    path = keys_path or resolve_keys_path()
    keys = list_ssh_keys(path)
    if keys:
        rows = []
        for key in keys:
            name = html.escape(key["comment"] or "מפתח בלי שם")
            fp = html.escape(key["fingerprint"])
            rows.append(
                '<div class="row"><div class="grow"><b>' + name + '</b><br><span class="small" dir="ltr">' + fp + '</span></div>'
                '<form method="post" action="/who/delete"><input type="hidden" name="fingerprint" value="' + fp + '">'
                '<button class="btn sec">מחק</button></form></div>'
            )
        key_html = "".join(rows)
    else:
        key_html = "<p class='muted'>אין מפתחות כניסה.</p>"
    if view["open"]:
        grant = view.get("grant") or {}
        when = html.escape(_fmt_time(grant.get("expires_at")))
        grant_html = f'<p class="ok">גישת תמיכה פתוחה. נסגרת לבד ב-<span dir="ltr">{when}</span>.</p>'
    else:
        grant_html = "<p>אין גישת תמיכה.</p>"
    err = f"<div class='msg'>{html.escape(error)}</div>" if error else ""
    body = f"""{LOGO['bot']}<h1>מי יכול להיכנס לשרת שלי?</h1>
<p class="why">כאן רואים מפתחות כניסה, גישת תמיכה, וכניסות אחרונות.</p>
{err}
<h2>מפתחות כניסה</h2>
{key_html}
<h2>גישת תמיכה</h2>
{grant_html}
<h2>כניסות אחרונות</h2>
{_login_html(view["logins"])}
<a class="btn sec" href="/status">חזרה</a>"""
    return page("מי יכול להיכנס לשרת שלי?", body)

def view_team(msg="", done=False):
    if not team_hook_available():
        return page("לא נמצא", "<p>לא נמצא</p>")
    if done:
        msg = msg or "נשלח. רק המשפט הזה."
    return page("בקשה לצוות", team_block(msg) + '<a class="btn sec" href="/status">חזרה</a>')

# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = "setup/1"
    def log_message(self, fmt, *a):  # never log query strings (setup token / oauth code)
        sys.stdout.write(f"[setup] {self.command} {self.path.split('?')[0]} {a[1] if len(a) > 1 else ''}\n"); sys.stdout.flush()
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if PREFIX and isinstance(body, str) and ctype.startswith("text/html"):
            for a in ('href="/', 'action="/', 'src="/', 'poster="/', "fetch('/", "location.href='/", "q.src='/"):
                body = body.replace(a, a[:-1] + PREFIX + "/")
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(b)), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                     "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", **(headers or {})}.items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def static_video(self, name, head=False):
        """explainer videos/posters from VIDEOS. Non-secret, so no setup token. Range support (iOS Safari needs 206)."""
        m = re.fullmatch(r"([a-z0-9][a-z0-9_-]{0,40})\.(mp4|jpg)", name or "")
        f = os.path.join(VIDEOS, name) if m else ""
        if not m or not os.path.isfile(f): return self.send(404, "not found", "text/plain")
        size = os.path.getsize(f); start, end = 0, size - 1; code = 200
        rg = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if rg and (rg.group(1) or rg.group(2)):
            if rg.group(1):
                start = int(rg.group(1)); end = min(int(rg.group(2)), size - 1) if rg.group(2) else size - 1
            else:
                start = max(0, size - int(rg.group(2)))
            if start > end or start >= size:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.send_header("Content-Length", "0"); self.end_headers(); return
            code = 206
        self.send_response(code)
        hdr = {"Content-Type": "video/mp4" if m.group(2) == "mp4" else "image/jpeg", "Content-Length": str(end - start + 1),
               "Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
        if code == 206: hdr["Content-Range"] = f"bytes {start}-{end}/{size}"
        for k, v in hdr.items(): self.send_header(k, v)
        self.end_headers()
        if head or self.command == "HEAD": return
        with open(f, "rb") as fh:
            fh.seek(start); left = end - start + 1
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk: break
                try: self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError): return
                left -= len(chunk)
    def do_HEAD(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/videos/"): return self.static_video(p[len("/videos/"):], head=True)
        self.send_response(405); self.send_header("Content-Length", "0"); self.end_headers()
    def redirect(self, to, cookie=None):
        h = {"Location": (PREFIX + to) if to.startswith("/") else to}
        if cookie: h["Set-Cookie"] = cookie
        self.send(303, "", headers=h)
    def authed(self):
        sv = session_value()
        c = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)setup_s=([0-9a-f]{64})", c)
        return bool(sv and m and hmac.compare_digest(m.group(1), sv))
    def form(self):
        n = min(int(self.headers.get("Content-Length", "0") or 0), 20000)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        if "json" in (self.headers.get("Content-Type") or ""):
            try: return json.loads(raw or "{}")
            except Exception: return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query); p = u.path.rstrip("/") or "/"
        if p == "/health": return self.send(200, json.dumps({"ok": True, "setup": True}), "application/json")
        if p.startswith("/videos/"): return self.static_video(p[len("/videos/"):])
        t = (q.get("t") or [""])[0]
        if t and setup_token() and hmac.compare_digest(t, setup_token()):
            return self.redirect("/", f"setup_s={session_value()}; Path={PREFIX or ''}/; HttpOnly; Secure; SameSite=Lax; Max-Age=31536000")
        if p == "/google/callback" and q.get("code"):   # web-client OAuth redirect (state is HMAC-signed, no cookie needed)
            try:
                import google_accounts as GA; GA._load_env()
                lab, em, ver, miss = GA.exchange(q["code"][0], (q.get("state") or [""])[0])
                return self.redirect("/step/google")
            except Exception as ex: return self.send(400, page("שגיאה", f"<p>{html.escape(str(ex))}</p><a class='btn pri' href='/step/google'>חזרה</a>"))
        if not self.authed():
            return self.send(403, page("נדרש קישור", "<h1>🔒 נדרש קישור ההגדרה</h1><p>פתח את הקישור האישי שקיבלת בהתקנה (מכיל <span dir='ltr'>?t=</span>).</p>"
                                       "<p class='muted small'>איבדת? בקש קישור חדש ממי שהזמין אותך. יש לך שרת משלך? בשרת: <span dir='ltr'>sudo agentctl link</span></p>"))
        st = status()
        if p == "/": return self.redirect("/step/" + next_step(st))
        if p == "/api/status": return self.send(200, json.dumps(st), "application/json")
        if p == "/api/wa/qr.png":
            try: return self.send(200, bridge("/qr.png", raw=True, timeout=5), "image/png")
            except Exception: return self.send(404, b"", "image/png")
        if p == "/guide/google": return self.send(200, page("מדריך Google", video_block("google") + GUIDE))
        if p == "/google/start": return self.redirect("/step/google")
        if p == "/support": return self.send(200, view_support())
        if p == "/who": return self.send(200, view_who())
        if p == "/team":
            if not team_hook_available(): return self.send(404, page("לא נמצא", "<p>לא נמצא</p>"))
            return self.send(200, view_team())
        views = {"/step/identity": view_identity, "/step/whatsapp": view_whatsapp, "/step/openai": view_openai,
                 "/step/google": view_google, "/step/done": view_done, "/status": view_done}
        if p in views: return self.send(200, views[p](st))
        return self.send(404, page("לא נמצא", "<p>לא נמצא</p>"))

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path.rstrip("/")
        if not self.authed(): return self.send(403, "forbidden", "text/plain")
        f = self.form()
        if p == "/step/identity":
            upd = {k: one_line(f.get(k), 40) for k in ("BOT_NAME", "OWNER_NAME", "PARTNER_NAME", "HOME_AREA")}
            upd["OWNER_PHONE"] = phone_digits(f.get("OWNER_PHONE")); upd["PARTNER_PHONE"] = phone_digits(f.get("PARTNER_PHONE"))
            tz = one_line(f.get("TIMEZONE"), 40) or "Asia/Jerusalem"
            if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+){0,2}", tz): tz = "Asia/Jerusalem"
            upd["TIMEZONE"] = tz; upd["WA_BOT_NAMES"] = upd["BOT_NAME"]
            if not (upd["BOT_NAME"] and upd["OWNER_NAME"] and len(upd["OWNER_PHONE"]) >= 9):
                return self.send(200, view_identity(status(), "חסר שם בוט, שם בעלים או מספר טלפון תקין"))
            env_write(upd)
            fd = os.open(PERSONA, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh: fh.write(str(f.get("persona") or "")[:2000])
            if not NO_SYSTEM and not TENANT: subprocess.run(["timedatectl", "set-timezone", tz], capture_output=True, timeout=10)
            restart_bot(); return self.redirect("/")
        if p == "/step/openai":
            key = re.sub(r"\s", "", str(f.get("key") or ""))
            upd = {}
            try: upd["COST_CEILING_USD"] = str(max(1, min(1000, int(float(f.get("ceiling") or 30)))))
            except ValueError: upd["COST_CEILING_USD"] = "30"
            if key:
                if not re.fullmatch(r"sk-[A-Za-z0-9_\-]{20,300}", key):
                    return self.send(200, view_openai(status(), "המפתח לא נראה תקין (צריך להתחיל ב-sk-)"))
                try:   # free call: list models
                    req = urllib.request.Request("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
                    with urllib.request.urlopen(req, timeout=20) as r: ok = r.status == 200
                except urllib.error.HTTPError as ex: ok = False; why = "המפתח נדחה על ידי OpenAI" if ex.code == 401 else f"OpenAI החזיר שגיאה {ex.code}"
                except Exception: ok = False; why = "לא הצלחתי להגיע ל-OpenAI, נסה שוב"
                if not ok: return self.send(200, view_openai(status(), why))
                upd["OPENAI_API_KEY"] = key
            env_write(upd); restart_bot(); return self.redirect("/")
        if p == "/step/google-client":
            cid = one_line(f.get("cid"), 200); sec = one_line(f.get("sec"), 200)
            if not cid.endswith(".apps.googleusercontent.com") or len(sec) < 10:
                return self.send(200, view_google(status(), "Client ID צריך להסתיים ב-apps.googleusercontent.com"))
            env_write({"GOOGLE_CLIENT_ID": cid, "GOOGLE_CLIENT_SECRET": sec, "GOOGLE_CLIENT_TYPE": "desktop"}); return self.redirect("/step/google")
        if p == "/step/google-reset":
            env_write({"GOOGLE_CLIENT_ID": "", "GOOGLE_CLIENT_SECRET": ""}); return self.redirect("/step/google")
        if p == "/step/google-paste":
            try:
                import google_accounts as GA; GA._load_env()
                lab, em, ver, miss = GA.exchange_from_pasted_url(f.get("url") or "")
                restart_bot(); return self.redirect("/step/google")
            except Exception as ex: return self.send(200, view_google(status(), str(ex)))
        if p == "/api/wa/pair":
            ph = phone_digits(f.get("phone"))
            try: r = bridge("/pair", {"phone": ph}, timeout=30)
            except urllib.error.HTTPError as ex:
                try: r = json.loads(ex.read() or b"{}")
                except Exception: r = {"ok": False, "error": f"bridge {ex.code}"}
            except Exception as ex: r = {"ok": False, "error": "שירות הוואטסאפ לא זמין עדיין, נסה שוב עוד מעט"}
            return self.send(200, json.dumps(r), "application/json")
        if p == "/api/wa/logout":
            try: bridge("/logout", {}, timeout=15)
            except Exception: pass
            return self.redirect("/step/whatsapp")
        if p == "/support/open":
            result = open_support(f.get("hours"))
            if not result.get("ok") and result.get("error") == "hours":
                return self.send(200, view_support(error="אפשר לבחור רק 1, 4, 24 או 72."))
            return self.redirect("/support")
        if p == "/support/close":
            close_support()
            return self.redirect("/support")
        if p == "/who/delete":
            result = delete_ssh_key(f.get("fingerprint") or "")
            if not result.get("ok"):
                return self.send(200, view_who(error="לא מחקתי את המפתח."))
            return self.redirect("/who")
        if p == "/team/request":
            if not team_hook_available():
                return self.send(404, "not found", "text/plain")
            result = submit_team_request(f.get("summary"), f.get("consent"))
            if result.get("ok"):
                return self.send(200, view_team(done=True))
            return self.send(200, view_team(msg=result.get("error") or "לא נשלח."))
        return self.send(404, "not found", "text/plain")

def ensure_token():
    if not setup_token():
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh: fh.write(secrets.token_urlsafe(24))

def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["support-login"]:
        entry = record_support_login(agent_home())
        print(entry.get("result") or "denied")
        return 0
    if args:
        print("usage: setup_web.py [support-login]", file=sys.stderr)
        return 2
    ensure_token()
    print(f"[setup] listening on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
