#!/usr/bin/env python3
"""Central onboarding site.
Default ("/"): the bot is created on THIS host (multi-bot mode) after a one-time invite code; the request goes through a
root-owned spool (agentsctl process-requests) and the owner continues on /t/<slug>/ (proxied to that bot's own setup page).
Optional ("/own-server"): the owner pastes a Hetzner Cloud API token, we create his own server with cloud-init that installs
the assistant, show progress, then hand off to the setup page on HIS server.
Security: the Hetzner token is used only inside the provisioning request and never stored or logged.
The per-customer setup token lives in memory only until handoff (max 3h). Binds 127.0.0.1 (Caddy in front)."""
import os, re, sys, json, html, time, hashlib, secrets, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("ONBOARD_PORT", "8840"))  # 8791/8792 belong to Tokyo APIs on this host - never use them
BIND = os.environ.get("ONBOARD_BIND", "127.0.0.1")  # set ONBOARD_BIND to the docker bridge address so the Caddy container can reach it; never 0.0.0.0
PUBLIC = (os.environ.get("ONBOARD_PUBLIC_URL", "") or "").rstrip("/")     # e.g. https://setup.example.com
PKG = os.environ.get("ONBOARD_PKG", os.path.join(BASE, "agent-template.tar.gz"))
HZ = "https://api.hetzner.cloud/v1"
SERVER_TYPES = [s.strip() for s in os.environ.get("ONBOARD_SERVER_TYPES", "cx23,cpx22,cax11").split(",") if s.strip()]
LOCATIONS = [s.strip() for s in os.environ.get("ONBOARD_LOCATIONS", "fsn1,nbg1,hel1").split(",") if s.strip()]
JOBS = {}; LOCK = threading.Lock(); RATE = {}
SPOOL = os.environ.get("ONBOARD_SPOOL", "/var/lib/assistant-onboarding")
ROUTES = os.environ.get("ONBOARD_ROUTES", "/opt/agents/_status/routes.json")
import http.client
VIDEOS = os.environ.get("ONBOARD_VIDEOS", os.path.join(BASE, "videos"))   # explainer videos (non-secret)

def video_block(name):
    if not os.path.isfile(os.path.join(VIDEOS, name + ".mp4")): return ""
    poster = f' poster="/videos/{name}.jpg"' if os.path.isfile(os.path.join(VIDEOS, name + ".jpg")) else ""
    return (f'<div class="vid"><p class="vcap">▶️ צפו בהסבר קצר</p>'
            f'<video controls playsinline preload="none"{poster} src="/videos/{name}.mp4"></video></div>')

def rate_ok(ip, key, limit):
    now = time.time()
    with LOCK:
        k = (key, ip); RATE[k] = [t for t in RATE.get(k, []) if now - t < 3600]
        if len(RATE[k]) >= limit: return False
        RATE[k].append(now); return True

def route_port(slug):
    try: return int(json.load(open(ROUTES)).get(slug) or 0)
    except Exception: return 0

def pkg_sha():
    try: return hashlib.sha256(open(PKG, "rb").read()).hexdigest()
    except Exception: return ""

def hz(method, path, token, body=None, timeout=30):
    req = urllib.request.Request(HZ + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}
    except Exception:
        return 0, {}

def cloud_init(setup_token, sha):
    url = f"{PUBLIC}/pkg/agent-template.tar.gz"
    return f"""#cloud-config
package_update: true
runcmd:
  - [bash, -c, "set -e; curl -fsSL '{url}' -o /root/agent.tar.gz; echo '{sha}  /root/agent.tar.gz' | sha256sum -c -; mkdir -p /root/agent-src; tar xzf /root/agent.tar.gz -C /root/agent-src; bash /root/agent-src/install.sh --yes --setup-token '{setup_token}' > /root/agent-install.log 2>&1"]
"""

def provision(token, name_hint):
    """-> (job dict, error_text). The token never leaves this function."""
    st, me = hz("GET", "/servers?per_page=1", token)
    if st == 401: return None, "הטוקן לא תקין. ודא שהעתקת את כולו."
    if st == 403: return None, "לטוקן אין הרשאה מתאימה. צור טוקן עם הרשאת Read & Write."
    if st == 0: return None, "לא הצלחתי להגיע ל-Hetzner כרגע. נסה שוב עוד מעט."
    if st != 200: return None, f"Hetzner החזיר שגיאה ({st}). נסה שוב עוד מעט."
    sha = pkg_sha()
    if not sha: return None, "חבילת ההתקנה לא זמינה כרגע בשרת ההרשמה."
    setup_token = secrets.token_urlsafe(24)
    name = "assistant-" + (re.sub(r"[^a-z0-9-]", "", (name_hint or "").lower())[:20] or secrets.token_hex(3))
    last = ""
    for stype in SERVER_TYPES:
        for loc in LOCATIONS:
            st, r = hz("POST", "/servers", token, {"name": name, "server_type": stype, "image": "ubuntu-24.04", "location": loc,
                                                   "user_data": cloud_init(setup_token, sha), "labels": {"app": "assistant"},
                                                   "start_after_create": True, "public_net": {"enable_ipv4": True, "enable_ipv6": True}}, timeout=60)
            if st in (200, 201):
                s = r.get("server") or {}
                ip = ((s.get("public_net") or {}).get("ipv4") or {}).get("ip", "")
                if not ip: return None, "השרת נוצר אבל לא קיבל כתובת IPv4. בדוק בקונסולה של Hetzner."
                job = {"id": secrets.token_urlsafe(12), "ip": ip, "host": ip.replace(".", "-") + ".sslip.io", "type": stype, "loc": loc,
                       "setup_token": setup_token, "root_password": r.get("root_password") or "", "created": time.time(), "ready": False}
                return job, ""
            err = (r.get("error") or {})
            last = err.get("code", str(st))
            if err.get("code") == "uniqueness_error": name = name + "-" + secrets.token_hex(2); continue
            if err.get("code") in ("unauthorized", "forbidden", "token_readonly"): return None, "לטוקן אין הרשאת כתיבה (Read & Write)."
    if last in ("resource_unavailable", "placement_error", "resource_limit_exceeded", "limit_exceeded"):
        return None, ("Hetzner לא מאפשר כרגע ליצור שרת בחשבון הזה (מגבלת זמינות/חשבון חדש). "
                      "נסה שוב מחר, או צור שרת ידנית בקונסולה ואז השתמש בהתקנה הידנית.")
    return None, f"יצירת השרת נכשלה ({last}). נסה שוב."

def check_ready(job):
    try:
        with urllib.request.urlopen(f"https://{job['host']}/health", timeout=6) as r:
            return r.status == 200 and json.loads(r.read() or b"{}").get("ok") is True
    except Exception: return False

# ---------------- UI ----------------
HETZNER = '<svg viewBox="0 0 48 48" width="56" height="56"><rect width="48" height="48" rx="10" fill="#D50C2D"/><path fill="#fff" d="M13 11h6v10h10V11h6v26h-6V27H19v10h-6z"/></svg>'
CSS = """*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Arial,sans-serif;background:#F6F7FB;color:#1F2330;line-height:1.55}
.wrap{max-width:520px;margin:0 auto;padding:18px 16px 40px}.card{background:#fff;border-radius:18px;padding:22px 18px;box-shadow:0 2px 14px rgba(20,30,60,.07)}
h1{font-size:1.35rem;margin:.4em 0 .2em}.muted{color:#5B6275;font-size:.95rem}label{display:block;font-weight:600;margin:14px 0 4px}
input{width:100%;font-size:16px;padding:12px;border:1px solid #CFD3DE;border-radius:12px;direction:ltr;text-align:left}
.btn{display:block;width:100%;text-align:center;font-size:1.05rem;font-weight:700;padding:14px;border-radius:14px;border:0;margin-top:14px;cursor:pointer;text-decoration:none}
.pri{background:#6C5CE7;color:#fff}.sec{background:#EEF0F7;color:#1F2330}ol{padding-inline-start:20px}li{margin:6px 0}
.msg{background:#FEF2F2;color:#991B1B;border-radius:12px;padding:10px;margin-top:10px}.note{background:#F1F5FF;border-radius:12px;padding:10px 12px;margin-top:10px;font-size:.93rem}
.bar{height:10px;background:#EEF0F7;border-radius:6px;overflow:hidden;margin:14px 0}.bar i{display:block;height:100%;width:5%;background:#6C5CE7;transition:width 1s}
.steps{display:flex;gap:6px;justify-content:center;margin:4px 0 14px}.dot{width:30px;height:6px;border-radius:4px;background:#D9DCE6}.dot.on{background:#6C5CE7}
code{direction:ltr;unicode-bidi:embed;background:#F1F0FF;padding:2px 6px;border-radius:6px}
.need{background:#FFF8E6;border-radius:12px;padding:10px 12px;margin:10px 0;font-size:.93rem}.need b{display:block;margin-bottom:2px}
.why{font-size:.93rem;color:#3B4257;margin:8px 0}.hint{font-size:.85rem;color:#5B6275;margin:4px 0 0}
details{background:#F6F7FB;border-radius:12px;padding:10px 12px;margin-top:12px;font-size:.93rem}summary{cursor:pointer;font-weight:700}
h2{font-size:1.05rem;margin:18px 0 4px}
.tagline{font-size:1.15rem;font-weight:800;color:#6C5CE7;margin:.05em 0 .35em;line-height:1.35}
.lead{color:#5B6275;font-size:.95rem;margin:0 0 14px}
.feats{margin:0 0 4px}.feats>h2{margin:0 0 8px}
.hl{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.hl div{background:#F1F0FF;border-radius:14px;padding:10px 11px 11px;font-size:.88rem;font-weight:700;color:#1F2330;line-height:1.3}
.hl div span{display:block;font-size:1.15rem;font-weight:400;margin-bottom:2px}
details.full{background:transparent;padding:0;margin:8px 0 0;font-size:.9rem}
details.full summary{font-weight:600;color:#6C5CE7;font-size:.9rem}
details.full[open]{background:#F6F7FB;padding:10px 12px;border-radius:12px;margin-top:8px}
.fg{margin:12px 0 0}.fg h3{font-size:.95rem;margin:0 0 4px}.fg ul{margin:0;padding-inline-start:1.2em}.fg li{margin:2px 0;color:#3B4257}
.tailor{margin:14px 0 6px}
.tailor>p:first-child{margin:0;font-size:.95rem;color:#1F2330;line-height:1.45}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px}
.chip{background:#F1F0FF;border-radius:999px;padding:6px 11px;font-size:.82rem;font-weight:600;color:#3B4257;line-height:1.35}
.andmore{font-weight:800;color:#6C5CE7;margin:0;font-size:.9rem}
.vid{margin:0 0 14px;text-align:center}.vid video{display:block;width:100%;max-width:300px;aspect-ratio:9/16;margin:0 auto;border-radius:14px;background:#000}.vcap{margin:0 0 6px;font-weight:700;font-size:.95rem}"""
def page(body):
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>מתאימים את העוזר עבורך</title><style>{CSS}</style></head><body><div class="wrap">
<div class="steps"><span class="dot on"></span><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
<div class="card">{body}</div></div></body></html>"""

BOT = '<svg viewBox="0 0 48 48" width="56" height="56"><rect width="48" height="48" rx="12" fill="#6C5CE7"/><circle cx="18" cy="22" r="4" fill="#fff"/><circle cx="30" cy="22" r="4" fill="#fff"/><path d="M15 31q9 7 18 0" stroke="#fff" stroke-width="3" fill="none" stroke-linecap="round"/></svg>'
HOSTED = f"""{BOT}<h1>בוא ניצור לך עוזר אישי</h1>
<p class="tagline">ניהול הזמן לעולם לא היה קל יותר.</p>
<p class="lead">עוזר אישי ששייך רק לך, והכל נשאר אצלך.</p>
{video_block("start")}<div class="feats">
<h2>מה אני יודע לעשות</h2>
<div class="hl">
<div><span>💬</span>וואטסאפ וקבוצות</div>
<div><span>🎧</span>קול ותמונות</div>
<div><span>⏰</span>תזכורות ורשימות</div>
<div><span>📅</span>יומן ומייל</div>
<div><span>💰</span>חשבוניות והוצאות</div>
<div><span>☀️</span>דוח בוקר</div>
</div>
<details class="full"><summary>ראה את כל הרשימה</summary>
<div class="fg"><h3>💬 וואטסאפ ושיחה</h3>
<ul>
<li>עונה בצ'אט פרטי ובקבוצות שהוא מצורף אליהן.</li>
<li>מבין הודעות קוליות ותמונות.</li>
<li>רואה גם הודעה שעשו לה ציטוט או תיוג.</li>
<li>שולח הודעות לאנשים ולקבוצות כשמבקשים.</li>
<li>מחפש בהיסטוריה של הצ'אט.</li>
<li>מחפש באינטרנט מידע עדכני.</li>
</ul></div>
<div class="fg"><h3>🧠 זיכרון ולמידה</h3>
<ul>
<li>זוכר עובדות, מעדכן אותן ומוחק כשמבקשים.</li>
<li>לומד מתיקונים: אומרים לו "לא ככה" והוא זוכר.</li>
<li>שומר כללים קבועים שאתה קובע.</li>
<li>אפשר לבחור לו אישיות וסגנון דיבור.</li>
</ul></div>
<div class="fg"><h3>⏰ תזכורות ומשימות</h3>
<ul>
<li>תזכורות חד-פעמיות וקבועות, עם דחייה וביטול.</li>
<li>רשימות קניות ומשימות, וסימון "בוצע".</li>
<li>מבין "קניתי", "שילמתי", "טיפלתי" ומוריד מהרשימה.</li>
</ul></div>
<div class="fg"><h3>📅 יומן (גוגל)</h3>
<ul>
<li>מציג אירועים ובודק זמינות.</li>
<li>שולח זימונים.</li>
<li>מעדכן או מזיז אירועים.</li>
</ul></div>
<div class="fg"><h3>✉️ מייל (ג'ימייל)</h3>
<ul>
<li>בודק את התיבה וקורא מיילים.</li>
<li>מוריד קבצים מצורפים.</li>
<li>שולח מייל רק אחרי קוד אישור בוואטסאפ.</li>
</ul></div>
<div class="fg"><h3>📄 מסמכים וקבצים</h3>
<ul>
<li>קורא קבצי PDF, סריקות ותמונות של מסמכים.</li>
<li>שומר, מתייג ומחפש, ושולח בחזרה בוואטסאפ.</li>
<li>מנסח מחדש מסמך לפי הוראות.</li>
<li>עובד עם גוגל דרייב: חיפוש, קישור והעלאה, וגם גיבוי אוטומטי של חשבוניות.</li>
</ul></div>
<div class="fg"><h3>💰 כסף וחשבונות</h3>
<ul>
<li>מזהה חשבוניות אוטומטית (ספק, סכום ותאריך).</li>
<li>סורק את המייל לחשבונות.</li>
<li>עונה על שאלות על הוצאות ונותן סיכום חודשי.</li>
<li>מציע קניות לפי ההיסטוריה, ומשווה מחירים.</li>
<li>מראה כמה העוזר עצמו עולה.</li>
</ul></div>
<div class="fg"><h3>👪 משפחה</h3>
<ul>
<li>ימי הולדת, ימי נישואין ותאריכים חשובים — לפי תאריך לועזי או עברי.</li>
<li>לוח שנה עברי וחגים.</li>
<li>חוגים של הילדים, לפי יום ושעה.</li>
</ul></div>
<div class="fg"><h3>✈️ טיולים</h3>
<ul>
<li>סורק את המייל לטיסות ולמלונות ובונה תיק טיול.</li>
<li>מציג מה הטיול הבא.</li>
</ul></div>
<div class="fg"><h3>🔒 רקע ובביטחון</h3>
<ul>
<li>דוח בוקר יומי.</li>
<li>הנתונים שלך נשארים אצלך.</li>
<li>תוכן ממיילים, מאתרים ומקבצים לא נחשב להוראות.</li>
<li>מייל או זימון לאנשים מבחוץ יוצאים רק אחרי קוד אישור ממך.</li>
</ul></div>
</details>
</div>
<div class="tailor">
<p>העוזר נתפר בדיוק בשבילך — נשמח לכל בקשה.</p>
<div class="chips">
<span class="chip">חידה יומית לילדים בקבוצה</span>
<span class="chip">מעקב אימוני כושר</span>
<span class="chip">דוחות לרואה החשבון</span>
<span class="chip">ניהול אלבום התמונות</span>
</div>
<p class="andmore">ועוד…</p>
</div>
<form id="f"><label>קוד הזמנה</label><input id="inv" autocomplete="off" required placeholder="invite code">
<p class="hint">הקוד שקיבלת ממי שהזמין אותך. מעתיקים ומדביקים אותו כמו שהוא.</p>
<label style="direction:rtl;text-align:right">איך לקרוא לעוזר?</label><input id="bn" maxlength="30" style="direction:rtl;text-align:right" placeholder="למשל: נועה">
<p class="hint">כל שם שבא לך. ככה תקרא לו בהודעות.</p>
<label style="direction:rtl;text-align:right">שם קצר באנגלית (לא חובה)</label><input id="sl" maxlength="14" placeholder="noa">
<p class="hint">רק לכתובת של דף ההגדרות. אפשר להשאיר ריק.</p>
<button class="btn pri" id="go">צור לי עוזר ←</button></form><div id="out"></div>
<details><summary>נתקעת?</summary><ul>
<li><b>"צריך קוד הזמנה תקין"</b>: בדוק שהעתקת את כל הקוד, בלי רווחים, באותיות גדולות וקטנות בדיוק כמו שקיבלת.</li>
<li><b>הקוד כבר נוצל</b>: כל קוד עובד פעם אחת. בקש קוד חדש ממי שהזמין אותך.</li>
<li><b>הדף נראה תקוע</b>: יצירת העוזר בדרך. אל תסגור את הדף.</li>
</ul></details>
<div class="note">רוצה שהעוזר ירוץ על שרת משלך? <a href="/own-server">אפשר גם כך</a> (בערך 6€ לחודש ל-Hetzner).</div>
<script>
const out=document.getElementById('out'),b=document.getElementById('go');
document.getElementById('f').onsubmit=async(e)=>{{e.preventDefault();b.disabled=true;b.textContent='יוצר...';
const r=await fetch('/api/create',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{invite:document.getElementById('inv').value.trim(),bot_name:document.getElementById('bn').value,slug:document.getElementById('sl').value}})}});
const j=await r.json();if(!j.ok){{out.innerHTML='<div class="msg">'+j.error+'</div>';b.disabled=false;b.textContent='צור לי עוזר ←';return;}}
let n=0;const iv=setInterval(async()=>{{n++;try{{const k=await (await fetch('/api/req/'+j.id)).json();
if(k.ok&&k.next){{clearInterval(iv);location.href=k.next;}}else if(k.error){{clearInterval(iv);out.innerHTML='<div class="msg">'+k.error+'</div>';b.disabled=false;b.textContent='צור לי עוזר ←';}}}}catch(e){{}}
if(n>40){{clearInterval(iv);out.innerHTML='<div class="msg">זה לוקח יותר מהרגיל. נסה שוב עוד מעט.</div>';}}}},3000);}};
</script>"""

HOME = f"""{HETZNER}<h1>שלב 0: שרת משלך ב-Hetzner</h1>
{video_block("own-server")}<div class="need"><b>מה צריך ביד</b>מייל, כרטיס אשראי, ולפעמים תעודה מזהה (Hetzner מבקשים אימות זהות בחשבון חדש).</div>
<p class="why"><b>למה?</b> Hetzner זו חברה שמשכירה מחשבים באינטרנט. העוזר יגור על מחשב קטן שרק שלך (בערך 6€ לחודש, משלמים ישירות ל-Hetzner).</p>
<p class="why"><b>מה זה "טוקן"?</b> סיסמה חד-פעמית שנותנת לנו רשות להקים בשבילך את השרת בחשבון שלך. כשהשרת מוכן אפשר למחוק אותה.</p>
<h2>1. פותחים חשבון</h2>
<ol><li>לוחצים על הכפתור, ממלאים מייל וסיסמה.</li><li>מאשרים את המייל שמגיע לתיבה.</li><li>מוסיפים כרטיס אשראי, ואם מבקשים, מאמתים זהות.</li></ol>
<a class="btn sec" href="https://accounts.hetzner.com/signUp" target="_blank" rel="noopener">1. הרשמה ל-Hetzner ↗</a>
<p class="hint">כבר יש לך חשבון? דלג לשלב 2.</p>
<h2>2. יוצרים טוקן</h2>
<ol><li>לוחצים על הכפתור ונכנסים לקונסולה.</li>
<li>לוחצים <b>New project</b>, נותנים שם (למשל <code>assistant</code>) ונכנסים לפרויקט.</li>
<li>בתפריט בצד: <b>Security</b>, ואז הלשונית <b>API tokens</b>.</li>
<li>לוחצים <b>Generate API token</b>, כותבים שם כלשהו, ומסמנים <b>Read &amp; Write</b> (חשוב!).</li>
<li>לוחצים שוב <b>Generate API token</b> ומעתיקים את הטוקן הארוך. הוא מוצג רק פעם אחת.</li></ol>
<a class="btn sec" href="https://console.hetzner.com/projects" target="_blank" rel="noopener">2. הקונסולה של Hetzner (פרויקט + טוקן) ↗</a>
<h2>3. מדביקים כאן</h2>
<form id="f"><label>הדבק את ה-API token</label><input id="tok" autocomplete="off" required placeholder="Hetzner API token">
<p class="hint">לחיצה ארוכה בתיבה ← הדבק.</p>
<label style="direction:rtl;text-align:right">שם קצר לשרת (לא חובה, באנגלית)</label><input id="nm" maxlength="20" placeholder="dani">
<button class="btn pri" id="go">צור לי שרת ←</button></form><div id="out"></div>
<details><summary>נתקעת?</summary><ul>
<li><b>"הטוקן לא תקין"</b>: כנראה לא הועתק כולו. צור טוקן חדש והעתק שוב בעזרת כפתור ההעתקה שליד הטוקן.</li>
<li><b>"אין הרשאת כתיבה"</b>: יצרת טוקן עם Read בלבד. צור טוקן חדש ובחר <b>Read &amp; Write</b>.</li>
<li><b>"Hetzner לא מאפשר כרגע ליצור שרת"</b>: בחשבון חדש זה קורה עד שהאימות שלהם מסתיים. נסה שוב מחר.</li>
<li><b>לא רואה Security</b>: קודם צריך להיכנס לתוך פרויקט (לחיצה על שם הפרויקט).</li>
</ul></details>
<div class="note">הטוקן משמש רק ליצירת השרת ולא נשמר אצלנו. אחרי שהשרת מוכן אפשר למחוק אותו ב-Hetzner.</div>
<script>
document.getElementById('f').onsubmit=async(e)=>{{e.preventDefault();const b=document.getElementById('go');b.disabled=true;b.textContent='יוצר שרת...';
const r=await fetch('/api/provision',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{token:document.getElementById('tok').value.trim(),name:document.getElementById('nm').value}})}});
const j=await r.json();document.getElementById('tok').value='';
if(!j.ok){{document.getElementById('out').innerHTML='<div class="msg">'+j.error+'</div>';b.disabled=false;b.textContent='צור לי שרת ←';return;}}
location.href='/job/'+j.job;}};
</script>"""

def job_page(job):
    pw = job.pop("root_password", "")   # shown once, then forgotten
    pw_html = (f'<div class="note">סיסמת root של השרת (מוצגת פעם אחת. לא צריך אותה לשימוש רגיל, אבל כדאי לשמור במקום בטוח):<br><code>{html.escape(pw)}</code></div>' if pw else "")
    return page(f"""{HETZNER}<h1>מקים את השרת שלך…</h1><p class="muted">שרת {html.escape(job['type'].upper())} נוצר ב-Hetzner. לא צריך לעשות כלום, רק להשאיר את הדף פתוח. בסוף הוא יעביר אותך לבד לשלב הבא.</p>
<div class="bar"><i id="b"></i></div><p id="s" class="muted">מתקין…</p>{pw_html}
<script>let t0=Date.now();setInterval(async()=>{{const p=Math.min(95,5+(Date.now()-t0)/4800);document.getElementById('b').style.width=p+'%';
try{{const j=await (await fetch('/api/job/{job['id']}')).json();if(j.ready){{document.getElementById('b').style.width='100%';document.getElementById('s').innerHTML='✅ השרת מוכן! עוברים להגדרת וואטסאפ, OpenAI ו-Google…';setTimeout(()=>location.href=j.next,1500);}}
else if(j.error){{document.getElementById('s').textContent=j.error;}}}}catch(e){{}}}},5000);</script>""")

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stdout.write(f"[onboard] {self.command} {self.path.split('?')[0][:40]} {a[1] if len(a) > 1 else ''}\n"); sys.stdout.flush()
    def send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(b)), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                     "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}.items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def client_ip(self): return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
    def proxy(self, slug, rest):
        port = route_port(slug)
        if not port: return self.send(404, page("<h1>לא נמצא</h1><p>אין עוזר בכתובת הזאת.</p>"))
        n = min(int(self.headers.get("Content-Length", "0") or 0), 2_000_000)
        body = self.rfile.read(n) if n else None
        hdr = {k: v for k, v in self.headers.items() if k.lower() in ("cookie", "content-type", "accept", "user-agent", "range", "if-range")}
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            c.request(self.command, rest or "/", body=body, headers=hdr); r = c.getresponse(); data = r.read()
        except Exception:
            return self.send(502, page("<h1>העוזר לא זמין רגע</h1><p>נסה לרענן עוד מעט.</p>"))
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() in ("content-type", "set-cookie", "location", "cache-control", "content-range", "accept-ranges"): self.send_header(k, v)
        for k, v in {"Content-Length": str(len(data)), "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}.items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)
    def static_video(self, name):
        """explainer videos/posters from VIDEOS with Range support (iOS Safari needs 206)."""
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
        if self.command == "HEAD": return
        with open(f, "rb") as fh:
            fh.seek(start); left = end - start + 1
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk: break
                try: self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError): return
                left -= len(chunk)
    def do_HEAD(self):
        p = self.path.split("?")[0]
        if p.startswith("/videos/"): return self.static_video(p[len("/videos/"):])
        self.send_response(405); self.send_header("Content-Length", "0"); self.end_headers()
    def tenant(self):
        m = re.match(r"^/t/([a-z][a-z0-9-]{2,20})(/.*)?$", self.path)
        if not m: return False
        if m.group(2) is None: self.send_response(302); self.send_header("Location", f"/t/{m.group(1)}/"); self.send_header("Content-Length", "0"); self.end_headers(); return True
        self.proxy(m.group(1), m.group(2)); return True
    def do_GET(self):
        if self.tenant(): return
        p = self.path.split("?")[0]
        if p == "/": return self.send(200, page(HOSTED))
        if p == "/own-server": return self.send(200, page(HOME))
        m = re.fullmatch(r"/api/req/([a-f0-9]{24})", p)
        if m:
            f = os.path.join(SPOOL, "results", m.group(1) + ".json")
            try: r = json.load(open(f))
            except FileNotFoundError: return self.send(200, '{"ok":false,"pending":true}', "application/json")
            except Exception: r = {"ok": False, "error": "שגיאה. נסה שוב."}
            if r.get("ok"): out = {"ok": True, "next": r["path"]}
            else: out = {"ok": False, "error": r.get("error") or "שגיאה. נסה שוב."}
            return self.send(200, json.dumps(out, ensure_ascii=False), "application/json")
        if p == "/health": return self.send(200, '{"ok":true}', "application/json")
        if p.startswith("/videos/"): return self.static_video(p[len("/videos/"):])
        if p == "/pkg/agent-template.tar.gz":
            try: return self.send(200, open(PKG, "rb").read(), "application/gzip")
            except Exception: return self.send(404, "no package", "text/plain")
        m = re.fullmatch(r"/(job|api/job)/([\w-]{8,40})", p)
        if m:
            with LOCK: job = JOBS.get(m.group(2))
            if not job: return self.send(404, page("<h1>לא נמצא</h1><p>הקישור פג. אם השרת נוצר, ההתקנה ממשיכה — הקישור להגדרה מופיע בשרת בקובץ /root/SETUP_LINK.txt.</p>"))
            if m.group(1) == "job": return self.send(200, job_page(job))
            if not job["ready"] and check_ready(job): job["ready"] = True
            out = {"ready": job["ready"]}
            if job["ready"]:
                out["next"] = f"https://{job['host']}/?t={job['setup_token']}"
            elif time.time() - job["created"] > 1500: out["error"] = "ההתקנה לוקחת יותר מהרגיל. נסה לרענן עוד מעט."
            return self.send(200, json.dumps(out), "application/json")
        return self.send(404, page("<h1>לא נמצא</h1>"))
    def do_POST(self):
        if self.tenant(): return
        if self.path == "/api/create":
            if not rate_ok(self.client_ip(), "create", 5):
                return self.send(429, json.dumps({"ok": False, "error": "יותר מדי ניסיונות. נסה שוב בעוד שעה."}, ensure_ascii=False), "application/json")
            try: body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0") or 0), 2000)) or b"{}")
            except Exception: body = {}
            inv = re.sub(r"[^A-Za-z0-9]", "", str(body.get("invite") or ""))  # tolerate copied backticks/spaces/quotes
            if not re.fullmatch(r"[A-Za-z0-9]{6,40}", inv):
                return self.send(200, json.dumps({"ok": False, "error": "צריך קוד הזמנה תקין."}, ensure_ascii=False), "application/json")
            rid = secrets.token_hex(12)
            req = {"id": rid, "invite": inv, "bot_name": str(body.get("bot_name") or "")[:30], "slug": str(body.get("slug") or "")[:14]}
            tmp = os.path.join(SPOOL, "requests", "." + rid)
            try:
                with open(tmp, "w") as f: json.dump(req, f)
                os.rename(tmp, os.path.join(SPOOL, "requests", rid + ".json"))
            except Exception:
                return self.send(200, json.dumps({"ok": False, "error": "השירות לא זמין כרגע."}, ensure_ascii=False), "application/json")
            print("[onboard] new bot request queued", flush=True)
            return self.send(200, json.dumps({"ok": True, "id": rid}), "application/json")
        if self.path != "/api/provision": return self.send(404, "{}", "application/json")
        if not rate_ok(self.client_ip(), "provision", 3):
            return self.send(429, json.dumps({"ok": False, "error": "יותר מדי ניסיונות. נסה שוב בעוד שעה."}, ensure_ascii=False), "application/json")
        try:
            n = min(int(self.headers.get("Content-Length", "0") or 0), 4000)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: body = {}
        token = str(body.get("token") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{40,100}", token):
            return self.send(200, json.dumps({"ok": False, "error": "זה לא נראה כמו טוקן של Hetzner (אותיות ומספרים, 64 תווים)."}), "application/json")
        job, err = provision(token, body.get("name"))
        token = None; body = None   # drop the customer's token right away
        if not job: return self.send(200, json.dumps({"ok": False, "error": err}, ensure_ascii=False), "application/json")
        with LOCK: JOBS[job["id"]] = job
        print(f"[onboard] server created type={job['type']} loc={job['loc']}", flush=True)
        return self.send(200, json.dumps({"ok": True, "job": job["id"]}), "application/json")

def janitor():
    while True:
        time.sleep(300)
        with LOCK:
            for k in [k for k, j in JOBS.items() if time.time() - j["created"] > 3 * 3600]: JOBS.pop(k, None)

if __name__ == "__main__":
    if not PUBLIC: sys.exit("set ONBOARD_PUBLIC_URL (e.g. https://setup.example.com)")
    threading.Thread(target=janitor, daemon=True).start()
    print(f"[onboard] listening on {BIND}:{PORT} pkg_sha={pkg_sha()[:12]}", flush=True)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
