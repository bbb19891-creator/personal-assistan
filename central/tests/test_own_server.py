"""Own-server page: one install line, no token, no registration, no network."""
import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "central"))
sys.path.insert(0, str(ROOT / "scripts"))

import onboard
import secret_scan

LINE = "curl -fsSL https://example.invalid/install.sh | bash"


def call(path, method="GET", body=b"", headers=None):
    handler = onboard.H.__new__(onboard.H)
    handler.path = path
    handler.command = method
    handler.headers = headers or {}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 9)
    sent = {}

    def send(code, payload, ctype="text/html; charset=utf-8"):
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        sent["code"] = code
        sent["body"] = payload
        sent["ctype"] = ctype

    handler.send = send
    if method == "GET":
        handler.do_GET()
    else:
        handler.do_POST()
    return sent


class OwnServerTests(unittest.TestCase):
    def setUp(self):
        self._env = {
            "PUBLIC_INSTALL_URL": os.environ.get("PUBLIC_INSTALL_URL"),
            "ONBOARD_SPOOL": os.environ.get("ONBOARD_SPOOL"),
        }
        os.environ["PUBLIC_INSTALL_URL"] = "https://example.invalid/install.sh"

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_install_line_from_env_at_runtime(self):
        self.assertEqual(onboard.install_line(), LINE)
        os.environ["PUBLIC_INSTALL_URL"] = "https://example.invalid"
        self.assertEqual(onboard.install_line(), LINE)
        os.environ["PUBLIC_INSTALL_URL"] = "https://example.invalid/"
        self.assertEqual(onboard.install_line(), LINE)
        os.environ["PUBLIC_INSTALL_URL"] = "https://cdn.example.invalid/a/install.sh"
        self.assertEqual(
            onboard.install_line(),
            "curl -fsSL https://cdn.example.invalid/a/install.sh | bash",
        )

    def test_bad_urls_show_no_line(self):
        with_userinfo = "https://" + "user" + "@" + "example.invalid/install.sh"
        samples = [
            "",
            "http://example.invalid/install.sh",
            "https://example.invalid/install.sh?x=1",
            with_userinfo,
            "https://example.invalid/other.sh",
            "https://example.invalid/install.sh/extra",
            "javascript:alert(1)",
            "https://example.invalid/a",
        ]
        for sample in samples:
            os.environ["PUBLIC_INSTALL_URL"] = sample
            self.assertEqual(onboard.public_install_url(), "", sample)
            self.assertNotIn("curl -fsSL", onboard.own_server_body())
            self.assertIn("השורה עדיין לא מוכנה", onboard.own_server_body())

    def test_page_copy_and_no_registration(self):
        body = onboard.own_server_body()
        self.assertIn(f'<pre class="cmd">{LINE}</pre>', body)
        self.assertIn("שרת משלך", body)
        self.assertIn("חשבון שלך ב-Hetzner", body)
        self.assertIn("לא רושמת אותך אצלנו", body)
        self.assertNotIn("<form", body)
        self.assertNotIn("fetch(", body)
        self.assertNotIn("/api/provision", body)
        self.assertNotIn("/api/create", body)
        self.assertNotIn("API token", body)
        self.assertNotIn("טוקן", body)
        self.assertNotIn("עוד מעט", body)
        page = onboard.page(body, show_steps=False)
        self.assertNotIn('<div class="steps">', page)

    def test_handler_does_not_register_or_provision(self):
        spool = Path(self.id().replace(".", "_"))
        # keep the spool path inside the system temp via the env only; the handler must not create it
        os.environ["ONBOARD_SPOOL"] = "/tmp/onboard-spool-unused-test"
        before = os.path.exists(os.environ["ONBOARD_SPOOL"])
        sent = call("/own-server")
        self.assertEqual(sent["code"], 200)
        self.assertIn(LINE, sent["body"])
        self.assertNotIn("/api/provision", sent["body"])
        self.assertEqual(os.path.exists(os.environ["ONBOARD_SPOOL"]), before)
        denied = call("/api/provision", method="POST", body=b"{}", headers={"Content-Length": "2"})
        self.assertEqual(denied["code"], 404)
        job = call("/api/job/abcdefgh")
        self.assertEqual(job["code"], 404)
        self.assertNotIn('"ready"', job["body"])
        package = call("/pkg/agent-template.tar.gz")
        self.assertEqual(package["code"], 404)
        self.assertFalse(spool.exists())

    def test_source_has_no_hetzner_client(self):
        src = (ROOT / "central" / "onboard.py").read_text(encoding="utf-8")
        self.assertNotIn("api.hetzner.cloud", src)
        self.assertNotIn("/api/provision", src)
        self.assertNotIn("urlopen", src)
        self.assertNotIn("API token", src)

    def test_rendered_page_has_no_denylist_terms(self):
        body = onboard.own_server_body()
        page = onboard.page(body, show_steps=False)
        for term, is_ascii in secret_scan.load_denylist(secret_scan.denylist_path()):
            haystack = page.lower() if is_ascii else page
            needle = term.lower() if is_ascii else term
            if needle in haystack:
                self.fail("own-server page contains a denylist term")


if __name__ == "__main__":
    unittest.main()
