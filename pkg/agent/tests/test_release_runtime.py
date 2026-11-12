"""Release clock, reminders, key cut, and the hard stop on manager contact.

WhatsApp is never contacted. Reminder delivery is a fake sender passed into tick.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_DIR))

import manager_contact
import release_runtime

T0 = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
RELEASE_AT = T0 + timedelta(days=30)  # 2026-02-01 12:00 UTC
HEBREW = __import__("re").compile(r"[\u0590-\u05FF]")
CLOCK_TIME = __import__("re").compile(r"\d{1,2}:\d{2}")


def _env(**extra) -> str:
    base = {
        "BOT_NAME": "עוזר",
        "OWNER_NAME": "בעלים",
        "OWNER_PHONE": "5550100",
        "TIMEZONE": "Asia/Jerusalem",
        "OPENAI_API_KEY": "our-openai-key",
        "WA_BRIDGE_TOKEN": "bridge-token-old",
        "MANAGER_TOKEN": "manager-token-old",
        "MANAGER_URL": "http://127.0.0.1:8850",
        "OUR_SSH_PUBLIC_KEY": "ssh-ed25519 AAAATEST our-key",
        "PUBLIC_BASE_URL": "https://setup.example/t/tenant-a",
    }
    base.update(extra)
    return "".join(f"{key}={value}\n" for key, value in base.items())


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(manager_contact)
        release_runtime.configure(send=None, apply_key=None)
        self._agentsctl = os.environ.pop("AGENTSCTL", None)
        self._socket = socket.create_connection
        self._urlopen = urllib.request.urlopen
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        socket.create_connection = self._socket
        urllib.request.urlopen = self._urlopen
        if self._agentsctl is None:
            os.environ.pop("AGENTSCTL", None)
        else:
            os.environ["AGENTSCTL"] = self._agentsctl
        self.tmp.cleanup()

    def _home(self, env_text=None) -> Path:
        home = self.root / "tenant-a"
        (home / "data").mkdir(parents=True)
        (home / "wa-bridge" / "auth").mkdir(parents=True)
        (home / "ssh").mkdir()
        (home / ".ssh").mkdir()
        (home / ".env").write_text(env_text if env_text is not None else _env(), encoding="utf-8")
        (home / "wa-bridge" / ".env").write_text(
            "WA_BRIDGE_TOKEN=bridge-token-old\nWA_BRIDGE_PORT=8795\n", encoding="utf-8"
        )
        (home / ".setup_token").write_text("setup-token-old\n", encoding="utf-8")
        (home / ".manager_token").write_text("manager-token-old\n", encoding="utf-8")
        (home / "ssh" / "our_ed25519").write_text("our-ssh-material\n", encoding="utf-8")
        (home / "ssh" / "our_ed25519.pub").write_text("ssh-ed25519 AAAATEST our-key\n", encoding="utf-8")
        auth = "ssh-ed25519 AAAATEST our-key release-ours\nssh-ed25519 AAAAFRIEND friend-key\n"
        (home / ".ssh" / "authorized_keys").write_text(auth, encoding="utf-8")
        (home / "ssh" / "authorized_keys").write_text(auth, encoding="utf-8")
        (home / "data" / "memory.db").write_bytes(b"memory-db-bytes")
        (home / "data" / "groups.json").write_text('{"family":"kept"}\n', encoding="utf-8")
        (home / "wa-bridge" / "auth" / "creds.json").write_text('{"session":"kept"}\n', encoding="utf-8")
        (home / "status.json").write_text('{"bot":"up","cost_today_usd":1}\n', encoding="utf-8")
        (home / "data" / "heartbeat").write_text("stamp\n", encoding="utf-8")
        status_dir = self.root / "_status"
        status_dir.mkdir(exist_ok=True)
        (status_dir / "tenant-a.json").write_text("{}\n", encoding="utf-8")
        (status_dir / "index.json").write_text('{"bots":{}}\n', encoding="utf-8")
        return home

    def _clock(self, home: Path) -> dict:
        return json.loads((home / "data" / "tenant_clock.json").read_text(encoding="utf-8"))

    def _block_network(self):
        calls = []

        def block_socket(*_a, **_k):
            calls.append("socket")
            raise AssertionError("socket.create_connection")

        def block_urlopen(*_a, **_k):
            calls.append("urllib")
            raise AssertionError("urlopen")

        socket.create_connection = block_socket
        urllib.request.urlopen = block_urlopen
        return calls

    def _seed_due(self, home: Path, when: datetime = RELEASE_AT) -> datetime:
        (home / "data" / "tenant_clock.json").write_text(
            json.dumps({
                "started_at": (when - timedelta(days=30)).isoformat(),
                "release_at": when.isoformat(),
                "reminders_sent": [],
            }),
            encoding="utf-8",
        )
        return when

    def test_clock_defaults_to_thirty_days_and_started_at_stays(self):
        home = self._home()
        release_runtime.tick(home, now=T0, send=lambda _text: None)
        first = self._clock(home)
        self.assertEqual(release_runtime._parse_dt(first["started_at"]), T0)
        self.assertEqual(
            release_runtime._parse_dt(first["release_at"]) - release_runtime._parse_dt(first["started_at"]),
            timedelta(days=30),
        )
        later = T0 + timedelta(days=2)
        release_runtime.tick(home, now=later, send=lambda _text: None)
        second = self._clock(home)
        self.assertEqual(second["started_at"], first["started_at"])
        self.assertEqual(second["release_at"], first["release_at"])
        self.assertFalse(second.get("released_at"))

    def test_admin_file_postpones_release_at_only(self):
        home = self._home()
        release_runtime.tick(home, now=T0, send=lambda _text: None)
        before = self._clock(home)
        later = (T0 + timedelta(days=60)).isoformat()
        (home / "admin").mkdir()
        (home / "admin" / "release.json").write_text(
            json.dumps({"release_at": later, "started_at": "2030-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        release_runtime.tick(home, now=T0 + timedelta(days=1), send=lambda _text: None)
        after = self._clock(home)
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(release_runtime._parse_dt(after["release_at"]), release_runtime._parse_dt(later))
        self.assertNotEqual(after["release_at"], before["release_at"])

    def test_earlier_or_broken_admin_file_does_not_move_the_clock(self):
        home = self._home()
        release_runtime.tick(home, now=T0, send=lambda _text: None)
        before = self._clock(home)
        (home / "admin").mkdir()
        (home / "admin" / "release.json").write_text(
            json.dumps({"release_at": (T0 + timedelta(days=10)).isoformat()}),
            encoding="utf-8",
        )
        release_runtime.tick(home, now=T0 + timedelta(hours=1), send=lambda _text: None)
        self.assertEqual(self._clock(home)["release_at"], before["release_at"])
        (home / "admin" / "release.json").write_text("not-json", encoding="utf-8")
        release_runtime.tick(home, now=T0 + timedelta(hours=2), send=lambda _text: None)
        self.assertEqual(self._clock(home)["release_at"], before["release_at"])
        self.assertEqual(self._clock(home)["started_at"], before["started_at"])

    def test_postpone_moves_the_reminder_window(self):
        home = self._home()
        sent = []
        release_runtime.tick(home, now=T0, send=sent.append)
        (home / "admin").mkdir()
        (home / "admin" / "release.json").write_text(
            json.dumps({"release_at": datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        result = release_runtime.tick(home, now=datetime(2026, 1, 25, 12, 0, tzinfo=timezone.utc), send=sent.append)
        self.assertEqual(sent, [])
        self.assertFalse(result["released"])

    def test_reminders_are_four_short_hebrew_notes_and_not_repeated(self):
        home = self._home()
        sent = []
        release_runtime.tick(home, now=T0, send=sent.append)
        self.assertEqual(sent, [])
        points = [
            RELEASE_AT - timedelta(days=7),
            RELEASE_AT - timedelta(days=3),
            RELEASE_AT - timedelta(days=1),
            datetime(2026, 2, 1, 0, 30, tzinfo=timezone.utc),
        ]
        for moment in points:
            release_runtime.tick(home, now=moment, send=sent.append)
        release_runtime.tick(home, now=points[0], send=sent.append)
        self.assertEqual(len(sent), 4)
        self.assertEqual(len(set(sent)), 4)
        self.assertFalse(self._clock(home).get("released_at"))
        for text in sent:
            self.assertRegex(text, HEBREW)
            self.assertIsNone(CLOCK_TIME.search(text))
            self.assertNotIn("בשעה", text)
            self.assertLess(len(text), 160)
            self.assertIn("01.02.2026", text)
        done = release_runtime.tick(home, now=RELEASE_AT, send=sent.append)
        self.assertTrue(done["released"])
        self.assertEqual(len(sent), 4)
        self.assertEqual(len(self._clock(home)["reminders_sent"]), 4)

    def test_missed_windows_do_not_burst(self):
        home = self._home()
        sent = []
        release_runtime.tick(home, now=T0, send=sent.append)
        release_runtime.tick(home, now=datetime(2026, 2, 1, 0, 30, tzinfo=timezone.utc), send=sent.append)
        self.assertEqual(len(sent), 1)
        self.assertIn("היום", sent[0])
        self.assertFalse(self._clock(home).get("released_at"))

    def test_immediate_release_needs_server_line_and_own_key(self):
        home = self._home(_env(FRIEND_OPENAI_KEY="friend-openai-key", OWN_SERVER="1", OWN_LINE="1"))
        sent = []
        result = release_runtime.tick(home, now=T0, send=sent.append)
        self.assertTrue(result["released"])
        self.assertTrue(result["immediate"])
        self.assertEqual(sent, [])
        text = (home / ".env").read_text(encoding="utf-8")
        self.assertIn("friend-openai-key", text)
        self.assertNotIn("our-openai-key", text)
        self.assertFalse((home / "data" / "tenant_clock.json").read_text(encoding="utf-8").find("our-openai-key") >= 0)

    def test_missing_any_ownership_flag_waits_for_the_clock(self):
        home = self._home(_env(FRIEND_OPENAI_KEY="friend-openai-key", OWN_SERVER="1"))
        result = release_runtime.tick(home, now=T0, send=lambda _text: None)
        self.assertFalse(result["released"])
        self.assertIn("our-openai-key", (home / ".env").read_text(encoding="utf-8"))

    def test_ownership_files_count_as_server_and_line(self):
        home = self._home()
        (home / "data" / "ownership.json").write_text(
            json.dumps({"own_server": True, "own_line": True}), encoding="utf-8"
        )
        (home / "data" / "friend_openai_key").write_text("friend-openai-key\n", encoding="utf-8")
        result = release_runtime.tick(home, now=T0, send=lambda _text: None)
        self.assertTrue(result["immediate"])
        self.assertTrue(result["released"])
        self.assertIn("friend-openai-key", (home / ".env").read_text(encoding="utf-8"))

    def test_replaced_openai_key_counts_as_the_friend_key(self):
        home = self._home()
        release_runtime.tick(home, now=T0, send=lambda _text: None)
        release_runtime.write_env(
            home / ".env",
            updates={"OPENAI_API_KEY": "friend-openai-key", "OWN_SERVER": "1", "OWN_LINE": "1"},
        )
        result = release_runtime.tick(home, now=T0 + timedelta(days=1), send=lambda _text: None)
        self.assertTrue(result["immediate"])
        text = (home / ".env").read_text(encoding="utf-8")
        self.assertIn("friend-openai-key", text)
        self.assertNotIn("our-openai-key", text)

    def test_release_cuts_our_material_and_keeps_memory_groups_and_settings(self):
        home = self._home()
        memory = (home / "data" / "memory.db").read_bytes()
        groups = (home / "data" / "groups.json").read_bytes()
        creds = (home / "wa-bridge" / "auth" / "creds.json").read_bytes()
        sent = []
        self._seed_due(home)
        try:
            result = release_runtime.tick(home, now=RELEASE_AT, send=sent.append)
        except SystemExit:
            self.fail("release exited the process")
        self.assertTrue(result["released_now"])
        self.assertEqual((home / "data" / "memory.db").read_bytes(), memory)
        self.assertEqual((home / "data" / "groups.json").read_bytes(), groups)
        self.assertEqual((home / "wa-bridge" / "auth" / "creds.json").read_bytes(), creds)
        env = (home / ".env").read_text(encoding="utf-8")
        self.assertIn("BOT_NAME=עוזר", env)
        self.assertIn("OWNER_NAME=בעלים", env)
        self.assertNotIn("our-openai-key", env)
        self.assertNotIn("manager-token-old", env)
        self.assertNotIn("MANAGER_URL", env)
        self.assertNotIn("OUR_SSH", env)
        self.assertNotIn("bridge-token-old", env)
        self.assertNotIn("our-openai-key", (home / "data" / "tenant_clock.json").read_text(encoding="utf-8"))
        self.assertFalse((home / ".manager_token").exists())
        self.assertFalse((home / "ssh" / "our_ed25519").exists())
        self.assertFalse((home / "ssh" / "our_ed25519.pub").exists())
        self.assertFalse((home / "status.json").exists())
        self.assertFalse((home / "data" / "heartbeat").exists())
        self.assertFalse((self.root / "_status" / "tenant-a.json").exists())
        self.assertTrue((self.root / "_status" / "index.json").is_file())
        for rel in (".ssh/authorized_keys", "ssh/authorized_keys"):
            kept = (home / rel).read_text(encoding="utf-8")
            self.assertNotIn("AAAATEST", kept)
            self.assertNotIn("release-ours", kept)
            self.assertIn("AAAAFRIEND", kept)
        bridge = release_runtime.read_env(home / ".env")["WA_BRIDGE_TOKEN"]
        bridge_file = release_runtime.read_env(home / "wa-bridge" / ".env")["WA_BRIDGE_TOKEN"]
        self.assertEqual(bridge, bridge_file)
        self.assertNotEqual(bridge, "bridge-token-old")
        self.assertGreater(len(bridge), 16)
        setup = (home / ".setup_token").read_text(encoding="utf-8").strip()
        self.assertNotEqual(setup, "setup-token-old")
        for path in (home / ".env", home / "wa-bridge" / ".env", home / ".setup_token"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        again = release_runtime.tick(home, now=RELEASE_AT + timedelta(days=1), send=sent.append)
        self.assertFalse(again["released_now"])
        self.assertEqual(release_runtime.read_env(home / ".env")["WA_BRIDGE_TOKEN"], bridge)
        self.assertEqual((home / ".setup_token").read_text(encoding="utf-8").strip(), setup)

    def test_agentsctl_release_access_when_the_binary_exists(self):
        home = self._home()
        log = self.root / "agentsctl-args"
        script = self.root / "agentsctl"
        script.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$1\" \"$2\" >> \"$LOG\"\nexit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        os.environ["AGENTSCTL"] = str(script)
        os.environ["LOG"] = str(log)
        self._seed_due(home)
        try:
            result = release_runtime.tick(home, now=RELEASE_AT, send=lambda _text: None)
            release_runtime.tick(home, now=RELEASE_AT + timedelta(hours=1), send=lambda _text: None)
        finally:
            os.environ.pop("LOG", None)
        self.assertEqual(result["agentsctl"], "called")
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["release-access", "tenant-a"])
        self.assertFalse((home / ".manager_token").exists())
        self.assertNotIn("our-openai-key", (home / ".env").read_text(encoding="utf-8"))

    def test_agentsctl_failure_still_cuts_and_stays_up(self):
        home = self._home()
        script = self.root / "agentsctl-fail"
        script.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        script.chmod(0o755)
        os.environ["AGENTSCTL"] = str(script)
        self._seed_due(home)
        try:
            result = release_runtime.tick(home, now=RELEASE_AT, send=lambda _text: None)
        except SystemExit:
            self.fail("release exited the process")
        self.assertTrue(result["released"])
        self.assertEqual(result["agentsctl"], "failed")
        self.assertEqual(self._clock(home).get("agentsctl_exit"), 3)
        self.assertFalse((home / ".manager_token").exists())

    def test_no_friend_key_replies_with_the_fixed_message_and_a_later_key_resumes(self):
        home = self._home()
        clock_path = home / "data" / "tenant_clock.json"
        release_runtime.tick(home, now=T0, send=lambda _text: None)
        clock = self._clock(home)
        clock["release_at"] = T0.isoformat()
        clock_path.write_text(json.dumps(clock), encoding="utf-8")

        def full(_text):
            raise AssertionError("full reply")

        reply = release_runtime.reply_for_turn(home, "שלום", full_reply=full, now=T0)
        self.assertTrue(reply.startswith(release_runtime.NO_KEY_MESSAGE))
        self.assertIn("https://setup.example/t/tenant-a?t=", reply)
        self.assertNotIn("setup-token-old", reply)
        token = (home / ".setup_token").read_text(encoding="utf-8").strip()
        self.assertIn(token, reply)
        self.assertEqual((home / "data" / "memory.db").read_bytes(), b"memory-db-bytes")
        self.assertEqual((home / "data" / "groups.json").read_text(encoding="utf-8"), '{"family":"kept"}\n')

        release_runtime.write_env(home / ".env", updates={"OPENAI_API_KEY": "friend-openai-key"})
        seen = []

        def resume(text):
            seen.append(text)
            return "full-reply"

        again = release_runtime.reply_for_turn(home, "היי", full_reply=resume, now=T0 + timedelta(hours=1))
        self.assertEqual(again, "full-reply")
        self.assertEqual(seen, ["היי"])
        self.assertIn("friend-openai-key", (home / ".env").read_text(encoding="utf-8"))

    def test_friend_key_at_release_keeps_full_replies(self):
        home = self._home(_env(FRIEND_OPENAI_KEY="friend-openai-key"))
        seen = []
        release_runtime.tick(home, now=T0, send=lambda _text: None)
        clock = self._clock(home)
        clock["release_at"] = T0.isoformat()
        (home / "data" / "tenant_clock.json").write_text(json.dumps(clock), encoding="utf-8")
        reply = release_runtime.reply_for_turn(
            home, "מה נשמע", full_reply=lambda text: seen.append(text) or "answered", now=T0
        )
        self.assertEqual(reply, "answered")
        self.assertEqual(seen, ["מה נשמע"])
        text = (home / ".env").read_text(encoding="utf-8")
        self.assertIn("friend-openai-key", text)
        self.assertNotIn("our-openai-key", text)

    def test_release_path_and_later_turn_do_not_touch_network(self):
        calls = self._block_network()
        home = self._home()
        self._seed_due(home)
        sent = []
        result = release_runtime.tick(home, now=RELEASE_AT, send=sent.append)
        self.assertTrue(result["released"])

        def full(_text):
            raise AssertionError("full reply")

        reply = release_runtime.reply_for_turn(home, "שלום", full_reply=full, now=RELEASE_AT)
        self.assertTrue(reply.startswith(release_runtime.NO_KEY_MESSAGE))
        (home / ".manager_token").write_text("restored-token\n", encoding="utf-8")
        release_runtime.write_env(
            home / ".env",
            updates={"MANAGER_URL": "http://127.0.0.1:9", "MANAGER_TOKEN": "restored-token"},
        )
        for call in (
            lambda: manager_contact.send_request(
                "http://127.0.0.1:9/intake", "restored-token", {"slug": "tenant-a"}
            ),
            lambda: manager_contact.send_status({"updated_at": "x", "cost_today_usd": 1}, home),
            lambda: manager_contact.send_cost({"cost_today_usd": 1}, home),
            lambda: manager_contact.send_heartbeat({"updated_at": "x"}, home),
            lambda: manager_contact.notify_turn(home),
        ):
            with self.assertRaises(manager_contact.ManagerContactBlocked):
                call()
        self.assertEqual(calls, [])
        self.assertFalse((home / "status.json").exists())
        self.assertFalse((home / "data" / "heartbeat").exists())
        for name in ("send_request", "send_status", "send_cost", "send_heartbeat", "notify_turn"):
            source = inspect.getsource(getattr(manager_contact, name))
            self.assertNotIn("urlopen", source)
            self.assertNotIn("create_connection", source)
            self.assertNotIn("getaddrinfo", source)

    def test_request_sender_uses_urllib_before_release_and_not_after(self):
        calls = {"url": 0, "socket": 0}

        class _Resp:
            status = 200

            def read(self):
                return b'{"ok": true, "id": 7}'

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake_urlopen(*_a, **_k):
            calls["url"] += 1
            return _Resp()

        def fake_socket(*_a, **_k):
            calls["socket"] += 1
            raise AssertionError("socket.create_connection")

        urllib.request.urlopen = fake_urlopen
        socket.create_connection = fake_socket
        status, body = manager_contact.send_request(
            "http://127.0.0.1:9/intake", "manager-token-old", {"slug": "tenant-a", "summary": "hi"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(calls, {"url": 1, "socket": 0})
        manager_contact.trip()
        with self.assertRaises(manager_contact.ManagerContactBlocked):
            manager_contact.send_request(
                "http://127.0.0.1:9/intake", "manager-token-old", {"slug": "tenant-a", "summary": "hi"}
            )
        self.assertEqual(calls, {"url": 1, "socket": 0})

    def test_health_files_publish_before_release(self):
        home = self._home()
        snap = {"updated_at": "2026-01-02T12:00:00", "cost_today_usd": 1.5, "cost_month_usd": 2, "bot": "up"}
        costs = manager_contact.send_cost(snap, home)
        self.assertEqual(costs["cost_today_usd"], 1.5)
        manager_contact.send_status(snap, home)
        manager_contact.send_heartbeat(snap, home)
        self.assertIn("cost_today_usd", (home / "status.json").read_text(encoding="utf-8"))
        self.assertEqual((home / "data" / "heartbeat").read_text(encoding="utf-8"), snap["updated_at"])

    def test_run_allows_a_missing_openai_key_only_after_release(self):
        import run

        home = self._home()
        self.assertIn("OPENAI_API_KEY", run.required_keys(home))
        self._seed_due(home)
        release_runtime.tick(home, now=RELEASE_AT, send=lambda _text: None)
        keys = run.required_keys(home)
        self.assertNotIn("OPENAI_API_KEY", keys)
        self.assertEqual(keys, ["BOT_NAME", "OWNER_NAME", "OWNER_PHONE"])

    def _import_time_events(self, src):
        """Module-level health start and release tick, ignoring function bodies."""
        events = []
        def_indent = []
        for lineno, line in enumerate(src.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            while def_indent and indent <= def_indent[-1]:
                def_indent.pop()
            stripped = line.strip()
            if def_indent:
                continue
            if stripped.startswith(("def ", "async def ", "class ")):
                def_indent.append(indent)
                continue
            if "target=_health_loop" in stripped:
                events.append(("health", lineno))
            if "startup_publish(" in stripped or "release_runtime.tick(" in stripped:
                events.append(("tick", lineno))
        return events

    def test_released_restart_does_not_publish_status_or_heartbeat(self):
        home = self._home()
        self._seed_due(home)
        release_runtime.tick(home, now=RELEASE_AT, send=lambda _text: None)
        request = home / ".release-request"
        self.assertTrue(request.is_file())
        self.assertNotIn("our-openai-key", request.read_text(encoding="utf-8"))
        importlib.reload(manager_contact)
        release_runtime.configure(send=None, apply_key=None)
        for path in (home / "status.json", home / "data" / "heartbeat"):
            if path.exists():
                path.unlink()
        snap = {"updated_at": "restarted", "cost_today_usd": 1.25, "cost_month_usd": 2, "bot": "up"}
        published = []

        def publish():
            published.append("request" if request.is_file() else "no-request")
            manager_contact.send_cost(snap, home)
            manager_contact.send_status(snap, home)
            manager_contact.send_heartbeat(snap, home)
            published.append("published")

        publish()
        self.assertEqual(published, ["request", "published"])
        self.assertIn("cost_today_usd", (home / "status.json").read_text(encoding="utf-8"))
        self.assertEqual((home / "data" / "heartbeat").read_text(encoding="utf-8"), "restarted")
        for path in (home / "status.json", home / "data" / "heartbeat", request):
            path.unlink()
        published.clear()
        release_runtime.startup_publish(home, publish, now=RELEASE_AT)
        self.assertEqual(published, ["request"])
        self.assertTrue(request.is_file())
        self.assertFalse((home / "status.json").exists())
        self.assertFalse((home / "data" / "heartbeat").exists())
        events = self._import_time_events((AGENT_DIR / "agent.py").read_text(encoding="utf-8"))
        health_at = [lineno for name, lineno in events if name == "health"]
        tick_at = [lineno for name, lineno in events if name == "tick"]
        self.assertTrue(tick_at, "startup does not tick before the health thread")
        self.assertTrue(health_at, "health thread is not started")
        self.assertLess(min(tick_at), min(health_at))

    def test_agent_source_wires_release_and_senders(self):
        agent = (AGENT_DIR / "agent.py").read_text(encoding="utf-8")
        launcher = (AGENT_DIR / "run.py").read_text(encoding="utf-8")
        self.assertIn("release_runtime.reply_for_turn", agent)
        self.assertIn("_mc.send_request", agent)
        self.assertIn("_mc.send_status", agent)
        self.assertIn("_mc.send_cost", agent)
        self.assertIn("_mc.send_heartbeat", agent)
        self.assertIn("allows_missing_openai_key", launcher)


if __name__ == "__main__":
    unittest.main()
