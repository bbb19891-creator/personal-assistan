"""Support access on the management page. Expiry uses a fake clock. No network."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pkg" / "agent"))
os.environ.setdefault("SETUP_NO_SYSTEM", "1")

import setup_web

T0 = 10 ** 6
HOUR = 3600
STATUS = {
    "whatsapp": False,
    "openai": False,
    "google": False,
    "identity": False,
    "bot_running": False,
    "bot_name": "נועה",
    "whatsapp_me": "",
}


class SupportAccessTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="support-")
        self._env = {}
        for key in ("AGENT_HOME", "SELF_HOST", "ASSISTANT_MODE", "MANAGER_URL", "MANAGER_TOKEN", "AGENT_TENANT", "SSH_AUTHORIZED_KEYS", "OWNER_PHONE"):
            self._env[key] = os.environ.get(key)
            os.environ.pop(key, None)
        os.environ["AGENT_HOME"] = self.home
        os.environ["SETUP_NO_SYSTEM"] = "1"
        self._urlopen = setup_web.urllib.request.urlopen
        setup_web.urllib.request.urlopen = self._no_network

    def tearDown(self):
        setup_web.urllib.request.urlopen = self._urlopen
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.home, ignore_errors=True)

    @staticmethod
    def _no_network(*_args, **_kwargs):
        raise AssertionError("network")

    def test_expires_with_fake_clock(self):
        opened = setup_web.open_support(1, self.home, now=T0)
        self.assertTrue(opened["ok"])
        self.assertTrue(opened["view"]["open"])
        self.assertEqual(opened["view"]["grant"]["expires_at"], T0 + HOUR)
        self.assertTrue(setup_web.support_is_open(self.home, now=T0 + HOUR - 1))
        expired = setup_web.support_view(self.home, now=T0 + HOUR)
        self.assertFalse(expired["open"])
        self.assertEqual(expired["grants"], [])
        # expiry is stored; an earlier clock does not reopen it
        self.assertFalse(setup_web.support_view(self.home, now=T0 + 10)["open"])
        stored = json.loads(Path(self.home, "support_access.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["grant"]["close_reason"], "expired")
        self.assertEqual(os.stat(Path(self.home, "support_access.json")).st_mode & 0o777, 0o600)

    def test_close_and_reopen_and_bad_hours(self):
        for bad in (0, 2, 3, 23, 48, 99, "", "2", "1.5", None, True, False):
            result = setup_web.open_support(bad, self.home, now=T0)
            self.assertFalse(result["ok"], bad)
        self.assertFalse(Path(self.home, "support_access.json").exists())
        opened = setup_web.open_support("4", self.home, now=T0)
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["view"]["grant"]["hours"], 4)
        again = setup_web.open_support(72, self.home, now=T0 + 10)
        self.assertEqual(again["error"], "already-open")
        self.assertEqual(again["view"]["grant"]["hours"], 4)
        setup_web.close_support(self.home, now=T0 + 20)
        self.assertFalse(setup_web.support_view(self.home, now=T0 + 21)["open"])
        reopened = setup_web.open_support(24, self.home, now=T0 + 30)
        self.assertTrue(reopened["ok"])
        self.assertEqual(reopened["view"]["grant"]["hours"], 24)

    def test_logins_are_visible_and_contain_no_chat(self):
        setup_web.open_support(1, self.home, now=T0)
        ok = setup_web.record_support_login(self.home, now=T0 + 5)
        self.assertEqual(ok["result"], "ok")
        self.assertEqual(set(ok), {"at", "kind", "result", "grant_id"})
        denied = setup_web.record_support_login(self.home, now=T0 + HOUR)
        self.assertEqual(denied["result"], "denied")
        view = setup_web.support_view(self.home, now=T0 + HOUR)
        self.assertEqual([item["result"] for item in view["logins"]], ["ok", "denied"])
        self.assertFalse(view["open"])
        page = setup_web.view_support(self.home, now=T0 + HOUR)
        self.assertIn("<p>הגישה סגורה. אנחנו לא נכנסים.</p>", page)
        self.assertIn("נכנסה תמיכה", page)
        self.assertIn("ניסיון שנדחה", page)
        self.assertNotIn('<p class="ok">הגישה פתוחה.</p>', page)
        blob = json.dumps(view, ensure_ascii=False)
        for key in ("summary", "chat", "messages", "history", "phone"):
            self.assertNotIn(key, blob)

    def test_support_page_buttons(self):
        closed = setup_web.view_support(self.home, now=T0)
        self.assertIn("פתח גישה לתמיכה", closed)
        self.assertIn("הגישה סגורה", closed)
        for hours in ("1", "4", "24", "72"):
            self.assertIn(f'value="{hours}"', closed)
        setup_web.open_support(72, self.home, now=T0)
        opened = setup_web.view_support(self.home, now=T0 + 3)
        self.assertIn("הגישה פתוחה", opened)
        self.assertIn("סגור גישה", opened)
        self.assertNotIn('name="hours"', opened)
        done = setup_web.view_done(STATUS)
        self.assertIn("פתח גישה לתמיכה", done)
        self.assertIn("מי יכול להיכנס לשרת שלי?", done)
        self.assertIn("אין כניסות", done)

    def test_cli_records_a_login_without_network(self):
        setup_web.open_support(1, self.home, now=T0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = setup_web.main(["support-login"])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().strip(), "denied")
        stored = json.loads(Path(self.home, "support_access.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["logins"][-1]["result"], "denied")
        self.assertEqual(stored["logins"][-1]["kind"], "support")


if __name__ == "__main__":
    unittest.main()
