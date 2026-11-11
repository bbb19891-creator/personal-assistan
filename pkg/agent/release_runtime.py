"""Local tenant release.

Clock: data/tenant_clock.json holds started_at and release_at (started_at + 30 days).
An admin postpone is data/admin/release.json {"release_at": "<ISO-8601>"} and replaces
release_at only when the new time is later. started_at never moves.

Immediate release: own server, own line, and an OpenAI key that is not the first
key this tenant saw. Markers (any one): env OWN_SERVER / OWN_LINE, data/ownership.json
keys own_server / own_line, or files data/own_server and data/own_line. The friend's
key is FRIEND_OPENAI_KEY, data/friend_openai_key, or OPENAI_API_KEY after it diverges
from the snapshotted key.

Reminders: at most four, in the 7-day, 3-day, 1-day, and release-day windows.
One per window. A missed window is not sent later.

On release the process stays up. Our OpenAI key, manager tokens, and our SSH
material are removed. WA_BRIDGE_TOKEN and the setup token are rotated on disk
(mode 600). agentsctl release-access <slug> is called when that executable exists.
Manager senders are tripped so they raise before DNS.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import manager_contact

PLACEHOLDER = "released-no-key"
NO_KEY_MESSAGE = (
    "אין מפתח, אז אני לא יכול לחשוב. הזיכרון, הקבוצות וההגדרות שמורים. "
    "מדביקים מפתח בדף הניהול:"
)
RELEASE_AFTER = timedelta(days=30)
REMINDER_KINDS = ("d7", "d3", "d1", "d0")
_SLUG_RE = re.compile(r"[a-z][a-z0-9-]{2,20}")
_LOCK = threading.RLock()
_HOOKS = {"send": None, "apply_key": None}

OUR_ENV_DELETE = (
    "MANAGER_TOKEN",
    "MANAGER_URL",
    "OUR_API_TOKEN",
    "OPERATOR_TOKEN",
    "HOST_API_TOKEN",
    "OUR_SSH_PUBLIC_KEY",
    "OUR_SSH_PRIVATE_KEY",
    "OPERATOR_SSH_PUBLIC_KEY",
    "HOST_SSH_PUBLIC_KEY",
)
OUR_SSH_FILES = (
    "ssh/our_ed25519",
    "ssh/our_ed25519.pub",
    "ssh/operator",
    "ssh/operator.pub",
    "data/our_ssh_key",
    "data/our_ssh_key.pub",
    "ssh/ours.pub",
)
AUTH_FILES = (".ssh/authorized_keys", "ssh/authorized_keys")
TOKEN_FILES = (".manager_token", "data/manager_token")
SSH_ENV_KEYS = (
    "OUR_SSH_PUBLIC_KEY",
    "OUR_SSH_PRIVATE_KEY",
    "OPERATOR_SSH_PUBLIC_KEY",
    "HOST_SSH_PUBLIC_KEY",
)


def configure(send=None, apply_key=None) -> None:
    """Replace both hooks. Pass None to clear a hook."""
    _HOOKS["send"] = send
    _HOOKS["apply_key"] = apply_key


def _apply_hook(friend_key: str) -> None:
    hook = _HOOKS.get("apply_key")
    if not hook:
        return
    try:
        hook(friend_key or "")
    except Exception:
        pass


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_env(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def write_env(path: Path, updates=None, delete=None) -> None:
    updates = dict(updates or {})
    delete = set(delete or ())
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen = set()
    out = []
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in delete:
            continue
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen and key not in delete:
            out.append(f"{key}={value}")
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _parse_dt(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock_path(home: Path) -> Path:
    return home / "data" / "tenant_clock.json"


def _load_clock(home: Path) -> dict | None:
    path = _clock_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _save_clock(home: Path, clock: dict) -> None:
    path = _clock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def allows_missing_openai_key(home) -> bool:
    clock = _load_clock(Path(home))
    return bool(clock and clock.get("released_at"))


def is_released(home) -> bool:
    return allows_missing_openai_key(home)


def _usable(value: str) -> str:
    text = (value or "").strip()
    if not text or text == PLACEHOLDER:
        return ""
    return text


def _tenant_tz(home: Path):
    name = read_env(home / ".env").get("TIMEZONE") or "Asia/Jerusalem"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Jerusalem")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _friend_key(home: Path, clock: dict) -> str:
    env = read_env(home / ".env")
    file_key = ""
    key_path = home / "data" / "friend_openai_key"
    if key_path.is_file():
        file_key = _usable(key_path.read_text(encoding="utf-8"))
    our = clock.get("our_openai_sha256") or ""
    for candidate in (
        _usable(env.get("FRIEND_OPENAI_KEY", "")),
        file_key,
        _usable(env.get("OPENAI_API_KEY", "")),
    ):
        if candidate and fingerprint(candidate) != our:
            return candidate
    return ""


def _ownership(home: Path, clock: dict) -> tuple[bool, bool, str]:
    env = read_env(home / ".env")
    marker = {}
    path = home / "data" / "ownership.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                marker = loaded
        except Exception:
            marker = {}
    own_server = (
        _truthy(env.get("OWN_SERVER"))
        or _truthy(marker.get("own_server"))
        or (home / "data" / "own_server").is_file()
    )
    own_line = (
        _truthy(env.get("OWN_LINE"))
        or _truthy(marker.get("own_line"))
        or (home / "data" / "own_line").is_file()
    )
    return own_server, own_line, _friend_key(home, clock)


def _apply_admin_postpone(home: Path, clock: dict) -> None:
    """Replace release_at only when the admin file names a later time."""
    if clock.get("released_at"):
        return
    path = home / "admin" / "release.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        new_at = _parse_dt(data["release_at"])
        current = _parse_dt(clock["release_at"])
    except Exception:
        return
    if new_at > current:
        clock["release_at"] = new_at.isoformat()


def _note_our_key(home: Path, clock: dict) -> None:
    if clock.get("released_at") or clock.get("our_openai_sha256"):
        return
    current = _usable(read_env(home / ".env").get("OPENAI_API_KEY", ""))
    if current:
        clock["our_openai_sha256"] = fingerprint(current)


def _ensure_clock(home: Path, now: datetime) -> dict:
    clock = _load_clock(home)
    if not clock or not clock.get("started_at") or not clock.get("release_at"):
        started = now.astimezone(timezone.utc)
        clock = {
            "started_at": started.isoformat(),
            "release_at": (started + RELEASE_AFTER).isoformat(),
            "our_openai_sha256": (clock or {}).get("our_openai_sha256") or "",
            "reminders_sent": list((clock or {}).get("reminders_sent") or []),
            "released_at": (clock or {}).get("released_at") or "",
            "secrets_rotated": bool((clock or {}).get("secrets_rotated")),
        }
    clock.setdefault("reminders_sent", [])
    clock.setdefault("released_at", "")
    clock.setdefault("secrets_rotated", False)
    clock.setdefault("our_openai_sha256", "")
    _note_our_key(home, clock)
    _apply_admin_postpone(home, clock)
    _save_clock(home, clock)
    return clock


def _reminder_text(kind: str, release_at: datetime, tz) -> str:
    local = release_at.astimezone(tz)
    date = f"{local.day:02d}.{local.month:02d}.{local.year}"
    texts = {
        "d7": f"עוד שבוע ({date}) העוזר עובר אליך. כדאי לחבר שרת, קו ומפתח משלך.",
        "d3": f"עוד שלושה ימים ({date}) העוזר עובר אליך. כדאי שיהיו שרת, קו ומפתח משלך.",
        "d1": f"מחר ({date}) העוזר עובר אליך.",
        "d0": f"היום ({date}) העוזר עובר אליך.",
    }
    return texts[kind]


def _due_kind(now: datetime, release_at: datetime, tz, sent: set) -> str:
    local_now = now.astimezone(tz).date()
    local_release = release_at.astimezone(tz).date()
    if local_now == local_release or now >= release_at:
        kind = "d0"
    elif now >= release_at - timedelta(days=1):
        kind = "d1"
    elif now >= release_at - timedelta(days=3):
        kind = "d3"
    elif now >= release_at - timedelta(days=7):
        kind = "d7"
    else:
        return ""
    if kind in sent:
        return ""
    return kind


def _emit(send, text: str) -> bool:
    target = send if send is not None else _HOOKS.get("send")
    if target is None:
        return False
    target(text)
    return True


def _maybe_remind(home: Path, clock: dict, now: datetime, send) -> str:
    release_at = _parse_dt(clock["release_at"])
    sent = set(clock.get("reminders_sent") or [])
    kind = _due_kind(now, release_at, _tenant_tz(home), sent)
    if not kind:
        return ""
    if len(sent) >= len(REMINDER_KINDS):
        return ""
    text = _reminder_text(kind, release_at, _tenant_tz(home))
    if not _emit(send, text):
        return ""
    sent.add(kind)
    clock["reminders_sent"] = [item for item in REMINDER_KINDS if item in sent]
    _save_clock(home, clock)
    return text


def find_agentsctl() -> str:
    override = os.environ.get("AGENTSCTL")
    candidates = []
    if override:
        candidates.append(override)
    else:
        found = shutil.which("agentsctl")
        if found:
            candidates.append(found)
        candidates.append("/usr/local/sbin/agentsctl")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def tenant_slug(home: Path) -> str:
    base = home.name.strip()
    if _SLUG_RE.fullmatch(base):
        return base
    env_slug = (os.environ.get("AGENT_TENANT") or "").strip()
    if _SLUG_RE.fullmatch(env_slug):
        return env_slug
    return ""


def _call_agentsctl(slug: str) -> tuple[str, int | None]:
    binary = find_agentsctl()
    if not binary or not slug:
        return "absent", None
    try:
        proc = subprocess.run(
            [binary, "release-access", slug],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return "failed", None
    if proc.returncode == 0:
        return "called", 0
    return "failed", int(proc.returncode)


def _key_blob(line: str) -> str:
    parts = line.split()
    if len(parts) >= 2 and parts[0].startswith("ssh-"):
        return parts[1]
    return ""


def _collect_our_blobs(home: Path, env: dict) -> set:
    blobs = set()
    for key in SSH_ENV_KEYS:
        blob = _key_blob(env.get(key, "")) or ""
        raw = env.get(key, "")
        if blob:
            blobs.add(blob)
        elif raw.strip():
            blobs.add(raw.strip())
    for rel in OUR_SSH_FILES:
        path = home / rel
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            blob = _key_blob(line)
            if blob:
                blobs.add(blob)
            elif line.strip():
                blobs.add(line.strip())
    return {item for item in blobs if item}


def _strip_authorized_keys(home: Path, blobs: set) -> None:
    for rel in AUTH_FILES:
        path = home / rel
        if not path.is_file():
            continue
        kept = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "release-ours" in line:
                continue
            blob = _key_blob(line)
            if blob and blob in blobs:
                continue
            if line.strip() and line.strip() in blobs:
                continue
            kept.append(line)
        text = "\n".join(kept)
        if text:
            text += "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)


def _delete(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError:
        pass


def _rotate_token(previous: str) -> str:
    fresh = secrets.token_urlsafe(24)
    if fresh == previous:
        fresh = secrets.token_urlsafe(24) + "x"
    return fresh


def _rotate_secrets(home: Path) -> None:
    env_path = home / ".env"
    env = read_env(env_path)
    bridge = _rotate_token(env.get("WA_BRIDGE_TOKEN", ""))
    write_env(env_path, updates={"WA_BRIDGE_TOKEN": bridge})
    bridge_env = home / "wa-bridge" / ".env"
    if bridge_env.is_file():
        write_env(bridge_env, updates={"WA_BRIDGE_TOKEN": bridge})
    setup = home / ".setup_token"
    if setup.is_file():
        current = setup.read_text(encoding="utf-8").strip()
        fd = os.open(setup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_rotate_token(current) + "\n")
        os.chmod(setup, 0o600)


def _drop_published_status(home: Path, slug: str) -> None:
    _delete(home / "status.json")
    _delete(home / "data" / "heartbeat")
    if not slug:
        return
    host_status = home.parent / "_status" / f"{slug}.json"
    try:
        if host_status.is_file() and host_status.parent.name == "_status":
            host_status.unlink()
    except OSError:
        pass


def _cut_local(home: Path, clock: dict, friend_key: str) -> None:
    if clock.get("local_cut"):
        return
    env_path = home / ".env"
    env = read_env(env_path)
    blobs = _collect_our_blobs(home, env)
    _strip_authorized_keys(home, blobs)
    for rel in OUR_SSH_FILES:
        _delete(home / rel)
    for rel in TOKEN_FILES:
        _delete(home / rel)
    delete = set(OUR_ENV_DELETE)
    updates = {}
    current = _usable(env.get("OPENAI_API_KEY", ""))
    our = clock.get("our_openai_sha256") or ""
    if friend_key:
        updates["OPENAI_API_KEY"] = friend_key
    elif current and our and fingerprint(current) == our:
        delete.add("OPENAI_API_KEY")
    elif current and our and fingerprint(current) != our:
        updates["OPENAI_API_KEY"] = current
    elif current and not our:
        delete.add("OPENAI_API_KEY")
    else:
        delete.add("OPENAI_API_KEY")
    if env_path.is_file() or updates:
        write_env(env_path, updates=updates, delete=delete)
    if not clock.get("secrets_rotated"):
        _rotate_secrets(home)
        clock["secrets_rotated"] = True
    clock["local_cut"] = True


def _record_agentsctl(home: Path, clock: dict) -> tuple[str, int | None]:
    """Call the host helper once. Retry only while it was not there yet."""
    if clock.get("agentsctl") in ("called", "failed"):
        return clock.get("agentsctl") or "absent", clock.get("agentsctl_exit")
    status, code = _call_agentsctl(tenant_slug(home))
    clock["agentsctl"] = status
    if code is not None:
        clock["agentsctl_exit"] = code
    _save_clock(home, clock)
    return status, code


def _perform(home: Path, clock: dict, now: datetime, friend_key: str) -> tuple[str, int | None]:
    manager_contact.trip(home)
    if not clock.get("released_at"):
        clock["released_at"] = now.astimezone(timezone.utc).isoformat()
        _save_clock(home, clock)
    _cut_local(home, clock, friend_key)
    _save_clock(home, clock)
    status, _code = _record_agentsctl(home, clock)
    _drop_published_status(home, tenant_slug(home))
    _apply_hook(friend_key)
    return status, _code


def _finish_if_needed(home: Path, clock: dict, now: datetime) -> None:
    """A restarted process trips senders again and finishes a partial cut."""
    manager_contact.trip(home)
    friend = _friend_key(home, clock)
    _cut_local(home, clock, friend)
    _save_clock(home, clock)
    _record_agentsctl(home, clock)
    _drop_published_status(home, tenant_slug(home))
    _apply_hook(friend)


def startup_publish(home, publish, now=None) -> dict:
    """Tick before any health publish. A restarted released process trips senders first."""
    result = tick(home, now=now)
    try:
        publish()
    except manager_contact.ManagerContactBlocked:
        return result
    return result


def tick(home, now=None, send=None) -> dict:
    """Advance the clock, send at most one reminder, and release when due."""
    with _LOCK:
        return _tick(Path(home), now=now, send=send)


def _tick(home: Path, now=None, send=None) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    clock = _ensure_clock(home, now)
    if clock.get("released_at"):
        _finish_if_needed(home, clock, now)
        return {
            "released": True,
            "released_now": False,
            "immediate": False,
            "reminder": "",
            "agentsctl": clock.get("agentsctl") or "absent",
        }
    own_server, own_line, friend = _ownership(home, clock)
    immediate = bool(own_server and own_line and friend)
    release_at = _parse_dt(clock["release_at"])
    due = now >= release_at
    reminder = ""
    if not immediate:
        try:
            reminder = _maybe_remind(home, clock, now, send)
        except Exception:
            reminder = ""
    if immediate or due:
        status, _code = _perform(home, clock, now, friend)
        return {
            "released": True,
            "released_now": True,
            "immediate": immediate,
            "reminder": reminder,
            "agentsctl": status,
        }
    return {
        "released": False,
        "released_now": False,
        "immediate": False,
        "reminder": reminder,
        "agentsctl": "absent",
    }


def management_link(home) -> str:
    home = Path(home)
    env = read_env(home / ".env")
    base = (env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        setup = read_env(home / "setup.env")
        base = (setup.get("SETUP_BASE_PATH") or "").strip().rstrip("/")
    if not base:
        return ""
    token = ""
    token_file = home / ".setup_token"
    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
    if token:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}t={token}"
    return base


def no_key_message(home) -> str:
    link = management_link(home)
    if link:
        return NO_KEY_MESSAGE + " " + link
    return NO_KEY_MESSAGE


def reply_for_turn(home, text, full_reply=None, now=None, send=None):
    """One inbound turn. After release with no friend key, a fixed reply and no model call."""
    home = Path(home)
    tick(home, now=now, send=send)
    try:
        manager_contact.notify_turn(home)
    except manager_contact.ManagerContactBlocked:
        pass
    if is_released(home):
        clock = _load_clock(home) or {}
        friend = _friend_key(home, clock)
        hook = _HOOKS.get("apply_key")
        if hook:
            try:
                hook(friend or "")
            except Exception:
                pass
        if not friend:
            return no_key_message(home)
    if full_reply is None:
        return None
    return full_reply(text)
