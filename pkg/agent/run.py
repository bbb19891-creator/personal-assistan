#!/usr/bin/env python3
"""Launcher: loads AGENT_HOME/.env, renders the bot identity (name / owner / partner / timezone) into the
agent template and runs it. Exit code 3 = not configured yet (systemd will not restart; finish the setup wizard)."""
import os, re, sys, time, runpy

CODE = os.path.dirname(os.path.abspath(__file__))          # shared code dir (single install: same as HOME)
HOME = os.environ.get("AGENT_HOME") or CODE                 # this bot's own dir: .env, data/, logs/, wa-bridge/
os.environ["AGENT_HOME"] = HOME
os.environ["AGENT_CODE"] = CODE

def load_env(path):
    if not os.path.exists(path): return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def clean_name(v, fallback):
    """Names are rendered into Python string literals/regexes: letters, digits, spaces and hyphens only."""
    v = re.sub(r"[^\w\u0590-\u05FF \-]", "", str(v or "")).strip()
    v = re.sub(r"\s+", " ", v)[:40]
    return v or fallback

def clean_tz(v):
    v = str(v or "").strip()
    return v if re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+){0,2}", v) else "Asia/Jerusalem"

def required_keys(home=None):
    """Identity is always required. After release the OpenAI key may be absent."""
    keys = ["OPENAI_API_KEY", "BOT_NAME", "OWNER_NAME", "OWNER_PHONE"]
    home = home or HOME
    try:
        if CODE not in sys.path:
            sys.path.insert(0, CODE)
        from release_runtime import allows_missing_openai_key
        if allows_missing_openai_key(home):
            keys = [k for k in keys if k != "OPENAI_API_KEY"]
    except Exception:
        pass
    return keys

def main():
    os.umask(0o077)   # db, tokens and logs readable by root only
    load_env(os.path.join(HOME, ".env"))
    missing = [k for k in required_keys(HOME) if not os.environ.get(k)]
    if missing:
        print(f"[SETUP] not configured yet, missing: {', '.join(missing)} — open the setup page", flush=True)
        sys.exit(3)
    tz = clean_tz(os.environ.get("TIMEZONE"))
    os.environ["TZ"] = tz
    try: time.tzset()
    except Exception: pass
    tokens = {
        "__BOT__": clean_name(os.environ.get("BOT_NAME"), "העוזר"),
        "__OWNER__": clean_name(os.environ.get("OWNER_NAME"), "הבעלים"),
        "__PARTNER__": clean_name(os.environ.get("PARTNER_NAME"), "בן/בת הזוג"),
        "__TZ__": tz,
    }
    src = open(os.path.join(CODE, "agent.py"), encoding="utf-8").read()
    for k, v in tokens.items(): src = src.replace(k, v)
    out_dir = os.path.join(HOME, ".run"); os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "agent_rendered.py")
    with open(out, "w", encoding="utf-8") as f: f.write(src)
    compile(src, out, "exec")   # fail fast with a clear traceback if a value broke the source
    if CODE not in sys.path: sys.path.insert(0, CODE)
    runpy.run_path(out, run_name="__main__")

if __name__ == "__main__":
    main()
