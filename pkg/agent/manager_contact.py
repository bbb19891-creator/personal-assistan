"""Outbound manager contact: status, cost, requests, heartbeats.

Before release, status and heartbeat are files the host watcher reads, and a
customization request is an HTTP POST. After release, those functions are
replaced. The replacements raise before any DNS or socket use. Putting a
manager URL back does not restore them.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


class ManagerContactBlocked(RuntimeError):
    """Raised before DNS or a socket when manager contact is no longer possible."""


_DEAD = False


def tripped() -> bool:
    return _DEAD


def _guard() -> None:
    if _DEAD:
        raise ManagerContactBlocked("refusing before DNS")


def _refuse(*_args, **_kwargs):
    raise ManagerContactBlocked("refusing before DNS")


def trip() -> None:
    """Destroy the sender functions. There is no reverse operation."""
    global _DEAD, send_status, send_cost, send_request, send_heartbeat, notify_turn, _post_request
    _DEAD = True
    send_status = _refuse
    send_cost = _refuse
    send_request = _refuse
    send_heartbeat = _refuse
    notify_turn = _refuse
    _post_request = _refuse


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def send_status(snapshot, home) -> None:
    """Write the local health file the host copies while we still have access."""
    _guard()
    if not isinstance(snapshot, dict) or not home:
        return
    _atomic_text(Path(home) / "status.json", json.dumps(snapshot, ensure_ascii=False))


def send_cost(snapshot, home) -> dict:
    """Cost figures are published only inside the status file. This is that sender."""
    _guard()
    if not isinstance(snapshot, dict):
        return {}
    return {
        "cost_today_usd": snapshot.get("cost_today_usd"),
        "cost_month_usd": snapshot.get("cost_month_usd"),
        "cost_ceiling_usd": snapshot.get("cost_ceiling_usd"),
    }


def send_heartbeat(snapshot, home) -> None:
    """Write the heartbeat file the host turns into heartbeat age."""
    _guard()
    if not home:
        return
    stamp = ""
    if isinstance(snapshot, dict):
        stamp = str(snapshot.get("updated_at") or "")
    _atomic_text(Path(home) / "data" / "heartbeat", stamp)


def _post_request(url: str, token: str, payload: dict) -> tuple[int, dict]:
    _guard()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", "x-manager-token": token or ""},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(status), body


def send_request(url: str, token: str, payload: dict) -> tuple[int, dict]:
    """POST one approved customization summary. Refuses outright after release."""
    _guard()
    return _post_request(url, token, payload)


def notify_turn(home=None) -> None:
    """Per-turn manager report. Before release there is nothing to send."""
    _guard()
    return None
