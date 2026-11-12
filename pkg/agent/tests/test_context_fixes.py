"""Context/logic fixes: pending-doc guard, tool trace in history, reminder rendering + history,
failed-payment filter, kid/owner separation, history window.

The static tests always run. The runtime tests load the real agent offline (temporary AGENT_HOME,
no network, no WhatsApp: the bridge call is replaced by a recorder) and are skipped when the agent's
Python dependencies are not installed.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

AGENT_PY = Path(__file__).resolve().parents[1] / "agent.py"
SRC = AGENT_PY.read_text(encoding="utf-8")
TOKENS = {"__BOT__": "עוזר", "__OWNER__": "בעלים", "__PARTNER__": "בן זוג", "__TZ__": "Asia/Jerusalem"}
DEPS = all(importlib.util.find_spec(m) for m in ("telebot", "openai", "firecrawl", "requests"))
OWNER, KID, KID_PHONE = "5550100", "ילד", "5550177"
KID_GROUP = "120363000000000001@g.us"


def _render(src: str) -> str:
    for k, v in TOKENS.items():
        src = src.replace(k, v)
    return src


class StaticTests(unittest.TestCase):
    def test_compiles_after_render(self):
        compile(_render(SRC), "agent_rendered", "exec")

    def test_block_present_and_generic(self):
        a = SRC.index("# ============ CONTEXT & LOGIC FIXES")
        b = SRC.index("# ============ END CONTEXT & LOGIC FIXES")
        block = SRC[a:b]
        for word in ("@lid", "972"):
            self.assertNotIn(word, block)
        root = Path(__file__).resolve().parents[3]
        scanner = root / "scripts" / "secret_scan.py"
        spec = importlib.util.spec_from_file_location("secret_scan", scanner)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for term, is_ascii in module.load_denylist(module.denylist_path()):
            hay = block.lower() if is_ascii else block
            needle = term.lower() if is_ascii else term
            self.assertNotIn(needle, hay, "denylist term in context block")

    def test_history_window_for_text_turns(self):
        self.assertIn("_wa_run_agent(key, effective, wbot, msg, eff_group, limit=12)", SRC)
        self.assertNotIn("_wa_run_agent(key, effective, wbot, msg, eff_group, limit=6)", SRC)

    def test_reminder_loop_uses_renderer_and_saves_history(self):
        loop = SRC[SRC.index("def reminder_loop"):SRC.index("threading.Thread(target=reminder_loop")]
        self.assertIn('_reminder_text"](rid, chat_id, content)', loop)
        self.assertIn('save_message(chat_id, "assistant", _rtxt)', loop)

    def test_kid_owner_stays_in_kid_flow(self):
        self.assertIn('_family_owner_person(p) != cfg["role"].split(":", 1)[1]', SRC)


@unittest.skipUnless(DEPS, "agent dependencies (telebot, openai, firecrawl) not installed")
class RuntimeTests(unittest.TestCase):
    G: dict = {}
    SENT: list = []

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        h = cls.home.name
        os.makedirs(os.path.join(h, "data"), exist_ok=True)
        os.environ.update({
            "AGENT_HOME": h, "OPENAI_API_KEY": "offline-test", "BOT_NAME": "עוזר", "OWNER_NAME": "בעלים",
            "OWNER_PHONE": OWNER, "EXTRA_OWNERS": f"{KID}:{KID_PHONE}", "WA_BRIDGE_URL": "http://127.0.0.1:9",
            "TELEGRAM_POLLING": "0", "AGENT_DRY_RUN": "", "FAMILY_DRY_RUN": "1", "FAMILY_SCHED_ENABLED": "0",
        })
        src = _render(SRC)
        src = src[:src.index('\nif os.environ.get("AGENT_DRY_RUN") == "1":\n    try:   # offline self-test')]
        blocked = {"reminder_loop", "run", "serve_forever", "_wa_worker", "_wa_startup_group_scan",
                   "_scheduler_loop", "_health_loop", "_release_loop", "_drive_backfill_loop"}
        ostart = threading.Thread.start

        def start(self_t):
            t = getattr(self_t, "_target", None)
            if t is not None and getattr(t, "__name__", "") in blocked:
                return None
            return ostart(self_t)
        threading.Thread.start = start
        try:
            g = {"__name__": "agent_offline"}
            sys.path.insert(0, str(AGENT_PY.parent))
            exec(compile(src, "agent_offline", "exec"), g)
        finally:
            threading.Thread.start = ostart
        g["_wa_api"] = lambda path, payload, timeout=60: cls.SENT.append((path, payload)) or {"ok": True}
        cls.G = g

    @classmethod
    def tearDownClass(cls):
        try:
            cls.G["conn"].close()
        except Exception:
            pass
        cls.home.cleanup()

    def test_pending_doc_does_not_hijack_other_answers(self):
        G = self.G
        conn = G["conn"]
        key = "wa:5550100@s.whatsapp.net"
        cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_docs)")]
        vals = {c: "" for c in cols if c not in ("id",)}
        vals.update({"chat_id": key, "status": "pending", "title": "חוזה", "tags": "בית", "created_at": datetime.now().isoformat()})
        conn.execute(f"INSERT INTO pending_docs ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
        conn.commit()
        G["save_message"](key, "user", "תנסח לי הודעה")
        G["save_message"](key, "assistant", "טיוטה: ...")
        self.assertIsNone(G["_handle_pending_doc_reply"](key, "כן"))
        self.assertIsNone(G["_handle_pending_doc_reply"](key, "קצר יותר"))
        self.assertIn("tool_pending_doc", G["TOOL_FUNCTIONS"])

    def test_tool_trace_is_stored_and_stripped(self):
        G = self.G
        key = "wa:trace@s.whatsapp.net"
        G["_c12_tls"].key, G["_c12_tls"].items = key, []
        dict.__setitem__(G["TOOL_FUNCTIONS"], "tool_create_reminder_fake", lambda **kw: "✅ תזכורת #7 נקבעה")
        G["TOOL_FUNCTIONS"].get("tool_create_reminder_fake")()
        G["save_message"](key, "assistant", "קבעתי")
        row = G["conn"].execute("SELECT content FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()[0]
        self.assertIn("⟦כלים בתור הזה: tool_create_reminder_fake ✅", row)
        self.assertEqual(G["_C12_TRACE_RX"].sub("", row).rstrip(), "קבעתי")

    def test_reminder_instruction_renders_report_not_instructions(self):
        G = self.G
        G["web_search"] = lambda q: "היום 14-23 מעלות, ללא גשם"
        G["_fam_llm"] = lambda *a, **k: "14–23°C, בלי גשם"
        raw = "דוח בוקר לקבוצת המשפחה: בדוק בזמן השליחה ב-web_search את מזג האוויר וב-tool_list_activities את החוגים"
        txt, plain = G["_reminder_text"](1, "wa:fam@g.us", raw)
        self.assertTrue(plain)
        self.assertNotIn("web_search", txt)
        self.assertIn("מזג האוויר", txt)
        txt2, _ = G["_reminder_text"](2, "wa:x", "לקנות חלב tool_create_reminder")
        self.assertNotIn("tool_", txt2)
        self.assertIn("לקנות חלב", txt2)

    def test_fired_reminder_is_saved_to_chat_history(self):
        G = self.G
        conn = G["conn"]
        key = "wa:5550199@s.whatsapp.net"
        conn.execute("INSERT INTO reminders (chat_id, content, fire_at, recurrence, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
                     (key, "לעבור על החוזה", (datetime.now() - timedelta(minutes=1)).isoformat(), None, datetime.now().isoformat()))
        conn.commit()
        t = threading.Thread(target=G["reminder_loop"], daemon=True)
        t.start()
        for _ in range(40):
            row = conn.execute("SELECT content FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if row:
                break
            time.sleep(0.25)
        self.assertTrue(row and row[0].startswith("⏰ תזכורת #") and "לעבור על החוזה" in row[0])
        self.assertTrue(any(p == "/send/text" and "לעבור על החוזה" in d.get("text", "") for p, d in self.SENT))

    def test_failed_payment_is_not_a_bill(self):
        G = self.G
        for subj in ("Couldn't process payment", "₪62.67 payment to X was unsuccessful again", "עדכון חשוב: לא הצלחנו לחייב את כרטיס האשראי שלך"):
            self.assertEqual(G["_extract_bill"]({"subject": subj, "snippet": "", "from": "", "date": "", "attachments": [], "body": ""}), {"is_bill": False})

    def test_kid_listed_as_owner_is_still_the_kid_in_own_group(self):
        G = self.G
        G["_chat_cfg_set"](KID_GROUP, name="קבוצת הילד", allowed=1, respond_all=1, persona="אתה חבר של ילד", role=f"kid:{KID}")
        seen = {}
        G["_wa_run_agent"] = lambda key, text, wbot, msg, is_group, limit=6, prefix="": seen.update(persona=G["_wa_persona_ref"][0], key=key)
        p = {"chat_id": KID_GROUP, "is_group": True, "type": "text", "text": "הגעתי לבית הספר", "sender": f"{KID_PHONE}@s.whatsapp.net",
             "sender_pn": f"{KID_PHONE}@s.whatsapp.net", "sender_name": KID, "group_name": "קבוצת הילד", "message_id": "T1"}
        self.assertEqual(G["_family_owner_person"](p), KID)
        G["_wa_process_inner"](p)
        self.assertTrue(seen.get("persona"), seen)


if __name__ == "__main__":
    unittest.main()
