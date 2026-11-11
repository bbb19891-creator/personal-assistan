"""Tests for the pilot ledger. They use a temporary data directory and never send WhatsApp."""
import json
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MANAGER = os.path.join(REPO, "platform", "assistant_manager.py")
sys.path.insert(0, os.path.join(REPO, "platform"))
import assistant_manager as am  # noqa: E402

FORBIDDEN = os.sep + "root"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fail_if(cond, message):
    if cond:
        raise AssertionError(message)


def _walk_hits(root, needle):
    raw = needle.encode("utf-8")
    hits = []
    if not os.path.isdir(root):
        return hits
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if raw in data:
                hits.append(name)
    return hits


def _child_env(data, agents, extra=None):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("AM_"):
            env.pop(key, None)
    env["AM_DATA"] = data
    env["AM_AGENTS_ROOT"] = agents
    env["AM_DRY_RUN"] = "1"
    env["AM_CHOWN"] = "0"
    env["AM_QUIET_FROM"] = "24"
    env["AM_QUIET_TO"] = "0"
    env["AM_FIRST_TICK_S"] = "3600"
    env["AM_TICK_S"] = "3600"
    env["AM_BIND_LOCAL"] = "127.0.0.1"
    env["AM_BIND_PROXY"] = "127.0.0.1"
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        os.environ["AM_DATA"] = str(self.root / "data")
        os.environ["AM_AGENTS_ROOT"] = str(self.root / "agents")
        os.environ["AM_DRY_RUN"] = "1"
        os.environ["AM_CHOWN"] = "0"
        os.environ["AM_QUIET_FROM"] = "24"
        os.environ["AM_QUIET_TO"] = "0"
        os.environ["AM_BIND_LOCAL"] = "127.0.0.1"
        os.environ["AM_BIND_PROXY"] = "127.0.0.1"
        os.environ.pop("AM_NOW", None)
        for key in ("AM_WA_BRIDGE_URL", "AM_WA_BRIDGE_TOKEN", "AM_WA_ADMIN_JID"):
            os.environ.pop(key, None)
        am.configure()
        am.API_TOKEN, am.PROXY_SECRET = am.init_data()
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def _start(self, kind):
        srv = am.Srv(("127.0.0.1", 0), kind)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.servers.append(srv)
        return srv.server_address[1]

    def _api(self, port, method, path, body=None, token=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["x-manager-token"] = token
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            data=data,
            method=method,
            headers=headers,
        )
        last = None
        for _ in range(30):
            try:
                with urllib.request.urlopen(req, timeout=5) as res:
                    raw = res.read()
                    parsed = json.loads(raw.decode("utf-8") or "{}")
                    return res.status, parsed
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    parsed = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    parsed = {}
                return exc.code, parsed
            except urllib.error.URLError as exc:
                last = exc
                time.sleep(0.05)
        self.fail("server did not accept connections")
        return 0, {"error": str(last)}

    def _form(self, port, path, fields):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            data=urllib.parse.urlencode(fields).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-manager-proxy": am.PROXY_SECRET,
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=5) as res:
                return res.status, res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _page(self, port, path):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path),
            headers={"x-manager-proxy": am.PROXY_SECRET},
        )
        last = None
        for _ in range(30):
            try:
                with urllib.request.urlopen(req, timeout=5) as res:
                    return res.status, res.read().decode("utf-8")
            except urllib.error.URLError as exc:
                last = exc
                time.sleep(0.05)
        self.fail("page did not accept connections: %s" % type(last).__name__)
        return 0, ""

    def _gone(self, needle, category):
        for base in (self.root / "data", self.root / "agents"):
            hits = _walk_hits(str(base), needle)
            _fail_if(hits, category + " still stored")

    def _outbox(self):
        path = self.root / "data" / "outbox.log"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def test_ledger_starts_empty(self):
        led = am.load_ledger()
        self.assertEqual(led["clients"], [])
        mode = os.stat(am.LEDGER_F).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_source_has_no_private_host_prefix(self):
        text = pathlib.Path(MANAGER).read_text(encoding="utf-8")
        _fail_if(FORBIDDEN in text, "manager source contains a private-host prefix")
        _fail_if("sqlite3" in text, "manager source opens a database")
        _fail_if("AM_ROOT_ENV" in text or "AM_ROOT_DB" in text, "removed settings came back")

    def test_rows_release_offset_and_limit(self):
        labels = ["pilot-a", "pilot-b", "pilot-c", "pilot-d", "עוזר בדיקה"]
        rows = []
        for label in labels:
            row, err = am.create_client(label)
            self.assertIsNone(err)
            rows.append(row)
        extra, err = am.create_client("pilot-extra")
        self.assertIsNone(extra)
        self.assertEqual(err, "pilot full")
        stored = [c["label"] for c in am.load_ledger()["clients"]]
        self.assertEqual(stored, labels)
        started = datetime.fromisoformat(rows[0]["started_at"])
        release = datetime.fromisoformat(rows[0]["release_at"])
        self.assertEqual(release - started, timedelta(days=30))
        codes = [c["invite_code"] for c in rows]
        self.assertEqual(len(set(codes)), 5)
        for code in codes:
            self.assertRegex(code, r"^[A-Z0-9]{10}$")
            self.assertTrue(any(ch.isalpha() for ch in code))
        self.assertEqual(rows[0]["line_slot"], "")
        self.assertEqual(rows[0]["key_slot"], "")
        self.assertEqual(rows[0]["openai_cap_usd"], "20.00")
        self.assertEqual(rows[0]["openai_spend_usd"], "0.0000")

    def test_postpone_only_moves_later(self):
        row, err = am.create_client("pilot-postpone")
        self.assertIsNone(err)
        current = row["release_at"]
        earlier = (datetime.fromisoformat(current) - timedelta(seconds=1)).isoformat(timespec="seconds")
        same = current
        later = (datetime.fromisoformat(current) + timedelta(days=2)).isoformat(timespec="seconds")
        denied, err = am.postpone_client(row["id"], earlier)
        self.assertIsNone(denied)
        self.assertEqual(err, "only later")
        denied, err = am.postpone_client(row["id"], same)
        self.assertIsNone(denied)
        self.assertEqual(err, "only later")
        self.assertEqual(am.get_client(row["id"])["release_at"], current)
        updated, err = am.postpone_client(row["id"], later)
        self.assertIsNone(err)
        self.assertEqual(updated["release_at"], later)

    def test_slots_are_operator_filled_and_cap_blocks(self):
        calls = []
        real = urllib.request.urlopen

        def boom(*_a, **_k):
            calls.append(1)
            raise AssertionError("network")

        urllib.request.urlopen = boom
        try:
            row, err = am.create_client("pilot-cap")
            self.assertIsNone(err)
            saved, err = am.set_slots(row["id"], {"line_slot": "line-slot-unique", "key_slot": "key-slot-unique"})
            self.assertIsNone(err)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(calls, [])
        stored = am.get_client(row["id"])
        _fail_if(stored["line_slot"] != "line-slot-unique", "line slot was not stored as entered")
        _fail_if(stored["key_slot"] != "key-slot-unique", "key slot was not stored as entered")
        _fail_if(stored["key_slot"].startswith("sk-"), "key slot looks generated")
        view = am.client_view(stored)
        self.assertEqual(view["line_slot"], "line-slot-unique")
        self.assertTrue(view["key_slot_set"])
        self.assertNotIn("key_slot", view)
        steps = [("19.9999", None, "19.9999"), ("0.0002", "cap", "19.9999"), ("0.0001", None, "20.0000"), ("0.0001", "cap", "20.0000")]
        for amount, expect, remain in steps:
            _saved, err = am.add_spend(row["id"], amount, "operator")
            self.assertEqual(err, expect)
            self.assertEqual(am.client_view(am.get_client(row["id"]))["openai_spend_usd"], remain)
            self.assertEqual(am.client_view(am.get_client(row["id"]))["openai_cap_usd"], "20.00")
        box = self._outbox()
        _fail_if("תקרה" not in box, "cap alert missing")
        _fail_if("key-slot-unique" in box, "key slot leaked into an alert")
        _fail_if(row["invite_code"] in box, "invite leaked into an alert")

    def test_cap_alert_still_sends_during_quiet_hours(self):
        os.environ["AM_QUIET_FROM"] = "0"
        os.environ["AM_QUIET_TO"] = "0"
        am.configure()
        row, err = am.create_client("pilot-quiet")
        self.assertIsNone(err)
        _saved, err = am.add_spend(row["id"], "20", "operator")
        self.assertIsNone(err)
        _fail_if("תקרה" not in self._outbox(), "quiet hours swallowed the cap alert")

    def test_own_flags_mark_due_without_deleting(self):
        row, err = am.create_client("pilot-flags")
        self.assertIsNone(err)
        saved, err = am.set_flags(row["id"], True, False, False)
        self.assertIsNone(err)
        self.assertFalse(am.is_release_due(saved))
        saved, err = am.set_flags(row["id"], own_line=True, own_key=True)
        self.assertIsNone(err)
        self.assertTrue(am.is_release_due(saved))
        self.assertEqual(saved["line_slot"], "")
        self.assertEqual(saved["key_slot"], "")
        self.assertIsNotNone(am.get_client(row["id"]))

    def test_past_release_marks_due_without_deleting(self):
        os.environ["AM_NOW"] = "2026-01-01T00:00:00"
        row, err = am.create_client("pilot-clock")
        self.assertIsNone(err)
        os.environ["AM_NOW"] = "2026-02-01T00:00:00"
        current = am.get_client(row["id"])
        self.assertIsNotNone(current)
        self.assertTrue(am.is_release_due(current))
        self.assertEqual(len(am.load_ledger()["clients"]), 1)

    def test_invite_store_failure_keeps_the_ledger_empty(self):
        agents = self.root / "agents"
        agents.mkdir()
        (agents / "_shared").write_text("not-a-dir", encoding="utf-8")
        row, err = am.create_client("pilot-rollback")
        self.assertIsNone(row)
        self.assertEqual(err, "invite store")
        self.assertEqual(am.load_ledger()["clients"], [])

    def test_intake_records_a_request(self):
        slug = "intake-bot"
        token = secrets.token_urlsafe(32)
        agents = self.root / "agents"
        home = agents / slug
        home.mkdir(parents=True)
        (home / ".manager_token").write_text(token + "\n", encoding="utf-8")
        (home / ".env").write_text("BOT_NAME=helper\n", encoding="utf-8")
        (agents / "registry.json").write_text(
            json.dumps({slug: {"user": "agent-intake-bot", "created": "2026-11-11 00:00:00"}}),
            encoding="utf-8",
        )
        port = self._start("local")
        status, body = self._api(port, "POST", "/intake", {"slug": slug, "summary": "please add a button"}, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        status, listed = self._api(port, "GET", "/api/requests", token=am.API_TOKEN)
        self.assertEqual(status, 200)
        texts = [item["text"] for item in listed["items"]]
        self.assertIn("please add a button", texts)
        status, _body = self._api(port, "GET", "/api/clients", token="nope")
        self.assertEqual(status, 401)

    def test_admin_round_trip(self):
        port = self._start("proxy")
        status, page = self._page(port, "/manage/")
        self.assertEqual(status, 200)
        _fail_if("אין עדיין חברים בפיילוט" not in page, "empty pilot state missing")
        csrf = am.csrf_token()
        status, page = self._form(port, "/manage/clients", {"csrf": csrf, "label": "pilot-admin"})
        self.assertEqual(status, 200)
        _fail_if("pilot-admin" not in page, "created row missing from the page")
        row = am.get_client("c1")
        self.assertIsNotNone(row)
        _fail_if(row["invite_code"] not in page, "invite missing from the page")
        status, page = self._form(port, "/manage/clients/c1/postpone", {"csrf": csrf, "release_at": "2000-01-01T00:00"})
        self.assertEqual(status, 200)
        _fail_if("מאוחר" not in page, "postpone error missing")
        _fail_if("only later" in page, "internal error on the page")
        later = (datetime.fromisoformat(row["release_at"]) + timedelta(days=3)).isoformat(timespec="minutes")
        status, _page = self._form(port, "/manage/clients/c1/postpone", {"csrf": csrf, "release_at": later})
        self.assertEqual(status, 303)
        self.assertGreater(am.get_client("c1")["release_at"], row["release_at"])
        status, _page = self._form(
            port, "/manage/clients/c1/flags",
            {"csrf": csrf, "own_server": "1", "own_line": "1", "own_key": "1"},
        )
        self.assertEqual(status, 303)
        flagged = am.get_client("c1")
        self.assertTrue(am.is_release_due(flagged))
        self.assertIsNotNone(am.get_client("c1"))
        status, _page = self._form(
            port, "/manage/clients/c1/slots",
            {"csrf": csrf, "line_slot": "line-slot-unique", "key_slot": "key-slot-unique"},
        )
        self.assertEqual(status, 303)
        _fail_if(am.get_client("c1")["key_slot"] != "key-slot-unique", "admin key slot was not stored")
        status, _page = self._form(port, "/manage/clients/c1/spend", {"csrf": csrf, "usd": "1.50"})
        self.assertEqual(status, 303)
        self.assertEqual(am.client_view(am.get_client("c1"))["openai_spend_usd"], "1.5000")
        status, _page = self._form(port, "/manage/clients/c1/link", {"csrf": csrf, "slug": "pilot-adm"})
        self.assertEqual(status, 303)
        self.assertEqual(am.get_client("c1")["slug"], "pilot-adm")
        status, page = self._page(port, "/manage/")
        self.assertEqual(status, 200)
        _fail_if("pilot-admin" not in page, "row missing before delete")
        _fail_if("key-slot-unique" in page, "key slot rendered on the page")
        status, _page = self._form(port, "/manage/clients/c1/delete", {"csrf": csrf})
        self.assertEqual(status, 303)
        status, page = self._page(port, "/manage/")
        self.assertEqual(status, 200)
        _fail_if("pilot-admin" in page, "deleted row still on the page")
        _fail_if(row["invite_code"] in page, "deleted invite still on the page")
        self.assertIsNone(am.get_client("c1"))

    def test_delete_removes_client_from_every_endpoint(self):
        slug = "pilot-del"
        other = "other-bot"
        label = "pilot-delete-row"
        note = "status-note-unique"
        line_slot = "line-slot-unique"
        key_slot = "key-slot-unique"
        request_text = "remove-this-request"
        keep_text = "keep-this-request"
        amount = "1.3700"
        agents = self.root / "agents"
        shared = agents / "_shared"
        shared.mkdir(parents=True)
        (shared / "invites.txt").write_text("KEEPCODE01\n", encoding="utf-8")
        token = secrets.token_urlsafe(32)
        home = agents / slug
        home.mkdir()
        (home / ".manager_token").write_text(token + "\n", encoding="utf-8")
        (home / ".env").write_text("BOT_NAME=helper\n", encoding="utf-8")
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        registry = {
            slug: {"user": "agent-pilot-del", "created": created},
            other: {"user": "agent-other-bot", "created": created},
        }
        (agents / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
        status_dir = agents / "_status"
        status_dir.mkdir()
        (status_dir / (slug + ".json")).write_text(json.dumps({"up": False}), encoding="utf-8")
        (status_dir / "index.json").write_text(json.dumps({
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "bots": {slug: {"up": False}, other: {"up": True}},
        }), encoding="utf-8")
        port = self._start("local")
        proxy = self._start("proxy")
        status, body = self._api(port, "POST", "/api/clients", {"label": label, "slug": slug}, token=am.API_TOKEN)
        self.assertEqual(status, 200)
        cid = body["client"]["id"]
        code = body["client"]["invite_code"]
        self.assertNotIn("key_slot", body["client"])
        status, body = self._api(port, "POST", "/api/clients/%s/slots" % cid, {"line_slot": line_slot, "key_slot": key_slot}, token=am.API_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["client"]["line_slot"], line_slot)
        self.assertNotIn("key_slot", body["client"])
        _fail_if(key_slot in json.dumps(body), "key slot returned by slots")
        status, body = self._api(port, "POST", "/cost", {"slug": slug, "usd": amount}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["openai_spend_usd"], amount)
        status, body = self._api(port, "POST", "/status", {"slug": slug, "up": False, "note": note}, token=token)
        self.assertEqual(status, 200)
        self.assertNotIn("note", body)
        item, err = am.add_request(other, keep_text)
        self.assertIsNone(err)
        status, body = self._api(port, "POST", "/intake", {"slug": slug, "summary": request_text}, token=token)
        self.assertEqual(status, 200)
        am.health_tick()
        _fail_if(code not in (shared / "invites.txt").read_text(encoding="utf-8"), "invite was not published")
        status, body = self._api(port, "GET", "/api/clients/%s" % cid, token=am.API_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["client"]["label"], label)
        status, page = self._page(proxy, "/manage/")
        self.assertEqual(status, 200)
        _fail_if(label not in page, "page missing the row before delete")
        _fail_if(key_slot in page, "key slot rendered before delete")

        status, body = self._api(port, "POST", "/api/clients/%s/delete" % cid, {}, token=am.API_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        _fail_if(label in json.dumps(body), "delete response returned the client")

        checks = [
            ("GET", "/healthz", None, 200),
            ("GET", "/api/summary", None, 200),
            ("GET", "/api/clients", None, 200),
            ("GET", "/api/clients/%s" % cid, None, 404),
            ("GET", "/api/requests", None, 200),
            ("GET", "/api/costs", None, 200),
            ("GET", "/api/costs/%s" % cid, None, 404),
            ("GET", "/api/status/%s" % cid, None, 404),
            ("GET", "/api/invites", None, 200),
            ("GET", "/api/invites/%s" % code, None, 404),
            ("POST", "/api/clients/%s/spend" % cid, {"usd": "1"}, 404),
            ("POST", "/api/clients/%s/postpone" % cid, {"release_at": "2099-01-01T00:00:00"}, 404),
            ("POST", "/api/clients/%s/delete" % cid, {}, 404),
        ]
        markers = [label, code, line_slot, key_slot, request_text, note, amount]
        for method, path, payload, expect in checks:
            status, body = self._api(port, method, path, payload, token=am.API_TOKEN)
            self.assertEqual(status, expect, path)
            blob = json.dumps(body, ensure_ascii=False)
            for marker in markers:
                _fail_if(marker in blob, "endpoint returned deleted client data")
        status, body = self._api(port, "POST", "/cost", {"slug": slug, "usd": "0.10"}, token=token)
        self.assertIn(status, (403, 404))
        _fail_if(label in json.dumps(body), "cost endpoint returned deleted client data")
        status, body = self._api(port, "POST", "/status", {"slug": slug, "note": note}, token=token)
        self.assertIn(status, (403, 404))
        _fail_if(note in json.dumps(body, ensure_ascii=False), "status endpoint returned deleted client data")
        status, page = self._page(proxy, "/manage/")
        self.assertEqual(status, 200)
        for marker in markers:
            _fail_if(marker in page, "page returned deleted client data")
        for marker in markers:
            self._gone(marker, "deleted data")
        requests = json.loads((self.root / "data" / "requests.json").read_text(encoding="utf-8"))
        texts = [item["text"] for item in requests["items"]]
        self.assertEqual(texts, [keep_text])
        invite_lines = (shared / "invites.txt").read_text(encoding="utf-8").split()
        self.assertEqual(invite_lines, ["KEEPCODE01"])
        self.assertFalse((home / ".manager_token").exists())
        self.assertTrue(home.is_dir())
        self.assertTrue((home / ".env").is_file())
        reg = json.loads((agents / "registry.json").read_text(encoding="utf-8"))
        self.assertNotIn(slug, reg)
        self.assertIn(other, reg)
        self.assertFalse((status_dir / (slug + ".json")).exists())
        index = json.loads((status_dir / "index.json").read_text(encoding="utf-8"))
        self.assertNotIn(slug, index.get("bots") or {})
        self.assertIn(other, index.get("bots") or {})
        self.assertIsNone(am.get_client(cid))


class BootTests(unittest.TestCase):
    def test_missing_data_dir_is_rejected(self):
        os.environ["AM_AGENTS_ROOT"] = tempfile.mkdtemp()
        os.environ.pop("AM_DATA", None)
        try:
            am.configure()
        except SystemExit as exc:
            self.assertEqual(str(exc), "AM_DATA is required")
            _fail_if(FORBIDDEN in str(exc), "error included a private-host path")
        else:
            self.fail("missing data directory was accepted")

    def test_private_host_data_dir_is_rejected_without_opening_it(self):
        opened = []
        real_open = os.open

        def spy(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        os.environ["AM_DATA"] = os.path.join(os.sep, "root", "mgr-data")
        os.environ["AM_AGENTS_ROOT"] = os.path.join(tempfile.mkdtemp(), "agents")
        os.open = spy
        try:
            try:
                am.configure()
            except SystemExit as exc:
                _fail_if("not allowed" not in str(exc), "private-host data dir rejected for the wrong reason")
                _fail_if(FORBIDDEN in str(exc), "error included a private-host path")
            else:
                self.fail("private-host data dir was accepted")
        finally:
            os.open = real_open
        _fail_if(any(FORBIDDEN in os.path.abspath(path) for path in opened), "opened a private-host path")

    def test_symlink_to_private_host_is_rejected(self):
        tmp = tempfile.mkdtemp()
        link = os.path.join(tmp, "link")
        os.symlink(os.path.join(os.sep, "root", "mgr-link"), link)
        os.environ["AM_DATA"] = link
        os.environ["AM_AGENTS_ROOT"] = os.path.join(tmp, "agents")
        try:
            am.configure()
        except SystemExit as exc:
            _fail_if("not allowed" not in str(exc), "symlink rejected for the wrong reason")
            _fail_if(FORBIDDEN in str(exc), "error included a private-host path")
        else:
            self.fail("symlink was accepted")

    def test_commands_boot_in_a_temp_dir(self):
        tmp = tempfile.mkdtemp()
        env = _child_env(os.path.join(tmp, "data"), os.path.join(tmp, "agents"))
        for cmd in ("tick", "render", "provision"):
            proc = subprocess.run(
                [sys.executable, MANAGER, cmd],
                cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
            )
            _fail_if(proc.returncode != 0, cmd + " failed")
            _fail_if(FORBIDDEN in (proc.stdout + proc.stderr), cmd + " printed a private-host path")
        bare = env.copy()
        bare.pop("AM_DATA")
        proc = subprocess.run(
            [sys.executable, MANAGER, "tick"],
            cwd=REPO, env=bare, capture_output=True, text=True, timeout=30,
        )
        _fail_if(proc.returncode == 0, "tick should require a data directory")
        _fail_if(FORBIDDEN in (proc.stdout + proc.stderr), "missing data dir mentioned a private-host path")
        _fail_if("AM_DATA is required" not in proc.stderr, "missing data dir failed for the wrong reason")

    def test_import_and_tick_open_no_private_host_path(self):
        tmp = tempfile.mkdtemp()
        script = r"""
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "platform"))
opened = []
import builtins
_open = builtins.open
def spy(path, *args, **kwargs):
    opened.append(os.fspath(path))
    return _open(path, *args, **kwargs)
builtins.open = spy
_osopen = os.open
def spy_os(path, *args, **kwargs):
    opened.append(os.fspath(path))
    return _osopen(path, *args, **kwargs)
os.open = spy_os
import urllib.request
def boom(*args, **kwargs):
    sys.stderr.write("unexpected-network\n")
    sys.exit(1)
urllib.request.urlopen = boom
import assistant_manager as am
am.configure()
am.init_data()
am.health_tick()
am.summary()
am.render_page("t")
am.DRY = False
if am.wa_send_now("ping"):
    sys.stderr.write("alert sent without a bridge\n")
    sys.exit(1)
needle = os.sep + "root"
bad = [p for p in opened if needle in os.path.abspath(p)]
if bad:
    sys.stderr.write("forbidden-open %d\n" % len(bad))
    sys.exit(1)
print("BOOT_OK")
"""
        env = _child_env(os.path.join(tmp, "data"), os.path.join(tmp, "agents"))
        env["AM_DRY_RUN"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
        )
        _fail_if(proc.returncode != 0, "boot spy failed")
        _fail_if("BOOT_OK" not in proc.stdout, "boot spy did not finish")
        _fail_if(FORBIDDEN in (proc.stdout + proc.stderr), "boot spy printed a private-host path")

    def test_serve_answers_healthz(self):
        tmp = tempfile.mkdtemp()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = _child_env(os.path.join(tmp, "data"), os.path.join(tmp, "agents"), {"AM_PORT": str(port)})
        proc = subprocess.Popen(
            [sys.executable, MANAGER, "serve"],
            cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        ok = False
        output = ""
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=1) as res:
                        body = json.loads(res.read().decode("utf-8"))
                        ok = res.status == 200 and body.get("ok") is True
                        break
                except Exception:
                    time.sleep(0.1)
        finally:
            proc.terminate()
            try:
                output, _err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _err = proc.communicate(timeout=5)
        _fail_if(not ok, "serve did not answer")
        _fail_if(FORBIDDEN in (output or ""), "serve printed a private-host path")


if __name__ == "__main__":
    unittest.main()
