"""Who can enter, and team requests: consent, summary-only payload, no self-host hook. No network."""
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
STATUS = {
    "whatsapp": False,
    "openai": False,
    "google": False,
    "identity": True,
    "bot_running": False,
    "bot_name": "נועה",
    "whatsapp_me": "",
}
KEYS = "# keep this line\nssh-ed25519 QUJDREVGR0g laptop-a\nno-port-forwarding ssh-rsa QUJDREVGR0h laptop-b\n"


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="access-")
        self.keys = str(Path(self.home) / "authorized_keys")
        Path(self.keys).write_text(KEYS, encoding="utf-8")
        self._env = {}
        for key in ("AGENT_HOME", "SELF_HOST", "ASSISTANT_MODE", "MANAGER_URL", "MANAGER_TOKEN", "AGENT_TENANT", "SSH_AUTHORIZED_KEYS", "OWNER_PHONE"):
            self._env[key] = os.environ.get(key)
            os.environ.pop(key, None)
        os.environ["AGENT_HOME"] = self.home
        os.environ["SSH_AUTHORIZED_KEYS"] = self.keys
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

    def test_list_and_delete_unknown_key(self):
        keys = setup_web.list_ssh_keys(self.keys)
        self.assertEqual([item["comment"] for item in keys], ["laptop-a", "laptop-b"])
        removed = setup_web.delete_ssh_key(keys[0]["fingerprint"], self.keys)
        self.assertTrue(removed["ok"])
        self.assertEqual(removed["removed"], 1)
        text = Path(self.keys).read_text(encoding="utf-8")
        self.assertIn("# keep this line", text)
        self.assertIn("laptop-b", text)
        self.assertNotIn("laptop-a", text)
        self.assertEqual(len(setup_web.list_ssh_keys(self.keys)), 1)
        missing = setup_web.delete_ssh_key("ab" * 32, self.keys)
        self.assertFalse(missing["ok"])
        self.assertIn("laptop-b", Path(self.keys).read_text(encoding="utf-8"))
        page = setup_web.view_who(self.home, now=T0, keys_path=self.keys)
        self.assertIn("מי יכול להיכנס לשרת שלי?", page)
        self.assertIn("מחק", page)
        self.assertIn("laptop-b", page)
        self.assertNotIn("laptop-a", page)
        self.assertIn("אין גישת תמיכה.", page)
        self.assertIn("אין כניסות", page)

    def test_who_page_shows_active_grant_and_logins(self):
        setup_web.open_support(4, self.home, now=T0)
        setup_web.record_support_login(self.home, now=T0 + 8)
        page = setup_web.view_who(self.home, now=T0 + 9, keys_path=self.keys)
        self.assertIn("גישת תמיכה פתוחה", page)
        self.assertIn("נכנסה תמיכה", page)
        later = setup_web.view_who(self.home, now=T0 + 4 * 3600, keys_path=self.keys)
        self.assertIn("אין גישת תמיכה.", later)
        self.assertNotIn("גישת תמיכה פתוחה", later)

    def _hosted(self):
        os.environ.pop("SELF_HOST", None)
        os.environ.pop("ASSISTANT_MODE", None)
        os.environ["MANAGER_URL"] = "http://127.0.0.1:8850"
        os.environ["MANAGER_TOKEN"] = "test-token"
        os.environ["AGENT_TENANT"] = "noa"

    def test_team_request_needs_consent_and_drops_chat(self):
        self._hosted()
        marker = "PRIVATECHATMARKER"
        data = Path(self.home) / "data"
        data.mkdir()
        (data / "chat.txt").write_text(marker, encoding="utf-8")
        (Path(self.home) / "persona.txt").write_text(marker, encoding="utf-8")
        os.environ["OWNER_PHONE"] = "9" * 9
        calls = []
        refused = setup_web.submit_team_request(
            {"summary": "תוסיף חידה", "messages": marker, "chat": marker},
            "",
            sender=lambda payload: calls.append(payload),
            home=self.home,
        )
        self.assertFalse(refused["sent"])
        self.assertEqual(calls, [])
        self.assertIn("הסכמה", refused["error"])
        payload = setup_web.build_team_payload(
            {"summary": "תוסיף חידה", "messages": marker, "chat": marker, "history": marker, "phone": "9" * 9, "slug": "evil"},
            consent="yes",
            home=self.home,
        )
        self.assertEqual(payload, {"summary": "תוסיף חידה", "slug": "noa"})
        self.assertNotIn(marker, json.dumps(payload, ensure_ascii=False))
        sent = setup_web.submit_team_request("תוסיף חידה", "yes", sender=lambda item: calls.append(item), home=self.home)
        self.assertTrue(sent["ok"])
        self.assertEqual(calls, [payload])
        page = setup_web.view_done(STATUS)
        self.assertIn("בקשה לצוות", page)
        self.assertIn('type="checkbox"', page)
        self.assertIn("אני מסכים", page)
        self.assertNotIn("checked", page)
        self.assertNotIn(marker, page)

    def test_self_host_hook_is_absent(self):
        os.environ["SELF_HOST"] = "1"
        os.environ["MANAGER_URL"] = "http://127.0.0.1:8850"
        os.environ["MANAGER_TOKEN"] = "test-token"
        (Path(self.home) / ".manager_token").write_text("test-token\n", encoding="utf-8")
        calls = []
        result = setup_web.submit_team_request("תוסיף חידה", "yes", sender=lambda payload: calls.append(payload), home=self.home)
        self.assertTrue(result["absent"])
        self.assertFalse(result["sent"])
        self.assertEqual(calls, [])
        self.assertIsNone(setup_web.build_team_payload("תוסיף חידה", consent="yes", home=self.home))
        self.assertEqual(setup_web.team_block(), "")
        page = setup_web.view_done(STATUS)
        self.assertNotIn("בקשה לצוות", page)
        self.assertNotIn("אני מסכים", page)
        self.assertIn("פתח גישה לתמיכה", page)
        os.environ["SELF_HOST"] = "0"
        os.environ["ASSISTANT_MODE"] = "self-host"
        self.assertFalse(setup_web.team_hook_available())

    def test_sender_posts_summary_only(self):
        self._hosted()
        captured = {}

        def opener(req, timeout=10):
            captured["url"] = req.full_url
            captured["data"] = req.data.decode("utf-8")
            captured["token"] = req.get_header("X-manager-token")

            class Response:
                def read(self, _n=2000):
                    return b'{"ok": true}'

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            return Response()

        setup_web._default_team_sender(
            {"summary": "תוסיף חידה", "messages": "PRIVATECHATMARKER", "slug": "noa"},
            opener=opener,
        )
        self.assertEqual(json.loads(captured["data"]), {"summary": "תוסיף חידה", "slug": "noa"})
        self.assertTrue(captured["url"].endswith("/intake"))
        self.assertEqual(captured["token"], "test-token")
        self.assertNotIn("PRIVATECHATMARKER", captured["data"])


if __name__ == "__main__":
    unittest.main()
