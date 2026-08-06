"""Build step 5 — the browser surface.

The page-danger gate was verified against a real Chrome over CDP during the
build: a login page refused fill and click, a checkout page refused fill, and a
submit button on a form JARVIS had not filled was refused until it had. These
tests re-prove that logic against fake page objects so the suite needs no browser.

    python jarvis/tests/test_browser.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.actions import Origin, ProposedAction, Provenance, Risk
from agent.bus import ActionBus
from agent.journal import Journal
from agent.paths import PathGuard
from agent.policy import PolicyConfig, PolicyEngine
from agent.surfaces import browser as B

USER = Provenance(Origin.USER)
FROM_WEB = Provenance(Origin.WEB, "https://supplier.example/portal")


class FakePage:
    """Just enough of a Playwright page to exercise the gate."""

    def __init__(self, url="https://example.test/page", selectors=()):
        self.url = url
        self._sel = set(selectors)

    def query_selector(self, sel):
        return object() if any(s in sel for s in self._sel) else None

    def query_selector_all(self, sel):
        return [object()] if self.query_selector(sel) else []


class TestPageDanger(unittest.TestCase):
    def test_an_ordinary_page_is_not_dangerous(self):
        self.assertEqual(B.page_danger(FakePage()), [])

    def test_a_password_field_makes_the_page_black(self):
        p = FakePage(selectors=["input[type=password]"])
        self.assertIn("a password field", B.page_danger(p))

    def test_a_card_field_makes_the_page_black(self):
        p = FakePage(selectors=["cc-"])
        self.assertIn("a payment card field", B.page_danger(p))

    def test_an_oauth_consent_form_makes_the_page_black(self):
        p = FakePage(selectors=["data-consent"])
        self.assertIn("an OAuth consent screen", B.page_danger(p))

    def test_a_suspicious_url_alone_still_flags(self):
        # A URL is a guess, so it never *clears* a page — but it can condemn one.
        self.assertTrue(B.page_danger(FakePage(url="https://bank.test/transfer")))

    def test_the_dom_is_what_decides_not_the_url(self):
        # An innocuous URL with a password field on it is still refused. This is
        # the case a URL allowlist would wave through.
        p = FakePage(url="https://blog.test/hello", selectors=["input[type=password]"])
        self.assertEqual(B.page_danger(p), ["a password field"])

    def test_require_safe_page_raises_and_names_the_reason(self):
        p = FakePage(selectors=["input[type=password]"])
        with self.assertRaises(B.PageRefused) as ctx:
            B._require_safe_page(p, "click")
        self.assertIn("a password field", str(ctx.exception))
        self.assertIn("no phrasing changes that", str(ctx.exception))


class TestConnectionAdvice(unittest.TestCase):
    def test_the_launch_command_carries_the_debug_port(self):
        self.assertIn(f"--remote-debugging-port={B.CDP_PORT}", B.launch_command())

    def test_it_uses_a_dedicated_profile_by_default(self):
        self.assertIn("--user-data-dir", B.launch_command())
        self.assertNotIn("Default", B.launch_command())

    def test_the_failure_message_explains_the_already_running_case(self):
        """§9.2 — don't just error, say what to do.

        The trap: a Chrome already running without the port means launching
        another one silently joins the existing process and the port never
        opens. "Just start Chrome" is actively wrong advice here.
        """
        import os
        old = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        try:
            with self.assertRaises(B.BrowserUnavailable) as ctx:
                B._BROWSER = None
                B.CDP_URL_BACKUP = B.CDP_URL
                B.CDP_URL = "http://127.0.0.1:1"        # nothing listens here
                B.connect()
        finally:
            B.CDP_URL = getattr(B, "CDP_URL_BACKUP", B.CDP_URL)
        msg = str(ctx.exception)
        self.assertIn("close Chrome completely", msg)
        self.assertIn("--remote-debugging-port", msg)


class TestTierAndTaint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.safe = root / "ws"
        self.safe.mkdir()
        guard = PathGuard(safe_zone=self.safe)
        B.configure(guard)
        self.policy = PolicyEngine(PolicyConfig(guard=guard, dry_run=True))
        self.bus = ActionBus(self.policy, Journal(root / "l.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_reading_a_page_is_green(self):
        a = self.bus.propose(ProposedAction("browser.read_page", {}, {}))
        self.assertEqual(a.risk, Risk.GREEN)

    def test_clicking_is_amber(self):
        a = self.bus.propose(ProposedAction("browser.click", {"selector": "#go"},
                                            {"selector": USER}))
        self.assertEqual(a.risk, Risk.AMBER)

    def test_a_selector_that_came_from_the_page_is_escalated(self):
        # §8: an argument derived from page content is escalated and its source named.
        a = self.bus.propose(ProposedAction("browser.click", {"selector": "#pay-now"},
                                            {"selector": FROM_WEB}))
        self.assertEqual(a.risk, Risk.RED)
        self.assertIn("supplier.example", a.preview)

    def test_a_download_path_outside_the_safe_zone_is_red(self):
        a = self.bus.propose(ProposedAction(
            "browser.download",
            {"url": "https://x.test/f.pdf", "dst": str(Path(self.tmp.name) / "elsewhere.pdf")},
            {"url": USER, "dst": USER}))
        self.assertEqual(a.risk, Risk.RED)

    def test_a_page_cannot_choose_where_a_download_lands(self):
        a = self.bus.propose(ProposedAction(
            "browser.download",
            {"url": "https://x.test/f.pdf", "dst": str(Path(self.tmp.name) / "elsewhere.pdf")},
            {"url": FROM_WEB, "dst": FROM_WEB}))
        self.assertEqual(a.risk, Risk.BLACK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
