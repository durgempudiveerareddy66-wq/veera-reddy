"""Build step 6 — the two surfaces that can do real damage.

Dry-run is OFF here. These tests let shell.py actually spawn processes and
comms.py actually write files, because the whole point of building them last is
that reading the code is not enough.

    python jarvis/tests/test_danger.py
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
from agent.surfaces import comms as C
from agent.surfaces import shell as S

USER = Provenance(Origin.USER)
FROM_FILE = Provenance(Origin.FILE, "notes/from-a-client.md")
ALLOWED = frozenset({"git", "git status", "git log", "python", "node", "npm run",
                     "mkdir", "dir"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.safe = root / "ws"
        self.index = root / "work"
        self.outside = root / "elsewhere"
        for d in (self.safe, self.index, self.outside):
            d.mkdir(parents=True)
        (self.index / "client.md").write_text("Contact priya@acme.example about it.")

        self.guard = PathGuard(safe_zone=self.safe, index_roots=(self.index,))
        S.configure(self.guard, ALLOWED)
        S.attach_all()
        C.configure(self.guard)
        C.attach_all()
        self.policy = PolicyEngine(PolicyConfig(
            guard=self.guard, allowed_commands=ALLOWED,
            known_recipients=C.known_recipients(), dry_run=False))
        self.bus = ActionBus(self.policy, Journal(root / "l.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def do(self, verb, args, prov=None, how="click"):
        a = self.bus.propose(ProposedAction(verb, args, prov or {k: USER for k in args}))
        return self.bus.execute(a, transcript=f"test {verb}", confirmed_by=how)


class TestShellReallyRuns(Base):
    def _script(self, name: str, code: str) -> str:
        p = self.safe / name
        p.write_text(code, encoding="utf-8")
        return str(p)

    def test_an_allowlisted_command_actually_executes(self):
        self._script("calc.py", "print(7*6)")
        r = self.do("shell.run", {"command": "python", "args": ["calc.py"],
                                  "cwd": str(self.safe)})
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.value["stdout"].strip(), "42")
        self.assertEqual(r.value["exit"], 0)

    def test_output_is_marked_untrusted(self):
        # §8: command output is content JARVIS read, not something Reddie said.
        self._script("one.py", "print(1)")
        r = self.do("shell.run", {"command": "python", "args": ["one.py"],
                                  "cwd": str(self.safe)})
        self.assertTrue(r.value["untrusted"])
        self.assertIn("shell:", r.value["source"])

    def test_cwd_defaults_to_the_safe_zone_not_the_process_directory(self):
        self._script("where.py", "import os\nprint(os.getcwd())")
        r = self.do("shell.run", {"command": "python", "args": ["where.py"]})
        self.assertEqual(Path(r.value["stdout"].strip()).resolve(), self.safe.resolve())

    def test_secrets_are_not_inherited_by_the_child(self):
        import os
        self._script("leak.py",
                     "import os\nprint(os.environ.get('GEMINI_API_KEY','ABSENT'))")
        os.environ["GEMINI_API_KEY"] = "sk-should-not-leak"
        try:
            r = self.do("shell.run", {"command": "python", "args": ["leak.py"],
                                      "cwd": str(self.safe)})
            self.assertEqual(r.value["stdout"].strip(), "ABSENT")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_inline_code_flags_defeat_the_allowlist_and_are_refused(self):
        """`python` being allowlisted must not mean `python -c <anything>`.

        This is the hole a list of command NAMES cannot see: the command is on
        the list, and what it runs is arbitrary. Found when this very test file
        reached for `python -c` to check that the shell surface worked.
        """
        # Only interpreters that ARE on the allowlist — an off-list command is
        # refused earlier, by the allowlist itself, which is the correct order.
        for cmd, flag in (("python", "-c"), ("python", "-m"),
                          ("node", "-e"), ("node", "--eval")):
            with self.subTest(cmd=cmd, flag=flag):
                with self.assertRaises(S.ShellRefused) as ctx:
                    S.run(cmd, [flag, "whatever"], str(self.safe))
                self.assertIn("allowlist meaningless", str(ctx.exception))

        # And powershell is refused one step earlier, by not being on the list.
        with self.assertRaises(S.ShellRefused) as ctx:
            S.run("powershell", ["-Command", "whatever"], str(self.safe))
        self.assertIn("not on the allowlist", str(ctx.exception))

    def test_a_command_off_the_allowlist_never_reaches_a_process(self):
        r = self.do("shell.run", {"command": "curl", "args": ["evil.sh"],
                                  "cwd": str(self.safe)})
        self.assertFalse(r.ok)
        self.assertEqual(r.action.risk, Risk.BLACK)

    def test_the_executor_refuses_independently_of_the_policy_engine(self):
        # Bypass the bus entirely — the surface must still say no.
        with self.assertRaises(S.ShellRefused):
            S.run("curl", ["evil.sh"], str(self.safe))

    def test_metacharacters_never_reach_a_process(self):
        with self.assertRaises(S.ShellRefused):
            S.run("python", ["-c", "print(1); rm -rf /"], str(self.safe))

    def test_git_push_is_red_and_voice_will_not_release_it(self):
        a = self.bus.propose(ProposedAction("shell.git", {"subcommand": "push",
                                                          "cwd": str(self.safe)},
                                            {"subcommand": USER}))
        self.assertEqual(a.risk, Risk.RED)
        self.assertFalse(self.policy.may_execute(a, confirmed_by="voice")[0])

    def test_a_file_cannot_introduce_a_command(self):
        a = self.bus.propose(ProposedAction("shell.run",
                                            {"command": "python", "args": ["-c", "1"],
                                             "cwd": str(self.safe)},
                                            {"command": FROM_FILE}))
        self.assertEqual(a.risk, Risk.BLACK)

    def test_installing_is_off_even_though_the_tier_would_allow_it(self):
        with self.assertRaises(S.ShellRefused) as ctx:
            S.install("requests")
        self.assertIn("JARVIS_ALLOW_INSTALL", str(ctx.exception))

    def test_scaffold_writes_real_files_inside_the_safe_zone(self):
        dst = self.safe / "site"
        r = self.do("shell.scaffold", {"template": "landing", "dst": str(dst)})
        self.assertTrue(r.ok, r.message)
        self.assertTrue((dst / "index.html").exists())
        self.assertTrue((dst / "style.css").exists())

    def test_scaffold_outside_the_safe_zone_is_red_and_refused_by_the_executor(self):
        with self.assertRaises(S.ShellRefused):
            S.scaffold("landing", str(self.outside / "site"))


class TestCommsCannotSend(Base):
    def test_contacts_are_harvested_from_the_indexed_files(self):
        self.assertIn("priya@acme.example", C.known_recipients())

    def test_sending_to_a_known_contact_is_red_never_lower(self):
        a = self.bus.propose(ProposedAction(
            "comms.send", {"to": "priya@acme.example", "subject": "Q3", "body": "hi"},
            {"to": USER}))
        self.assertEqual(a.risk, Risk.RED)

    def test_voice_cannot_release_a_send(self):
        a = self.bus.propose(ProposedAction(
            "comms.send", {"to": "priya@acme.example", "subject": "Q3", "body": "hi"},
            {"to": USER}))
        ok, why = self.policy.may_execute(a, confirmed_by="voice")
        self.assertFalse(ok)
        self.assertIn("Voice alone cannot", why)

    def test_a_stranger_is_black_even_with_a_click(self):
        r = self.do("comms.send", {"to": "admin@evil.com", "subject": "x", "body": "y"})
        self.assertFalse(r.ok)
        self.assertEqual(r.action.risk, Risk.BLACK)

    def test_even_a_confirmed_send_does_not_pretend_to_have_sent(self):
        # The tier allows it; the adapter does not exist. What must never happen
        # is a send that reports success without a transport.
        r = self.do("comms.send", {"to": "priya@acme.example", "subject": "Q3",
                                   "body": "hi"})
        self.assertFalse(r.ok)
        self.assertIn("nothing was sent", r.message)

    def test_a_failed_send_still_leaves_the_draft_on_disk(self):
        self.do("comms.send", {"to": "priya@acme.example", "subject": "Q3", "body": "hi"})
        drafts = list((self.safe / "drafts").glob("*.md"))
        self.assertEqual(len(drafts), 1)
        self.assertIn("Status: DRAFT", drafts[0].read_text())

    def test_drafting_is_amber_and_writes_a_real_file(self):
        a = self.bus.propose(ProposedAction(
            "comms.draft", {"to": "priya@acme.example", "subject": "Q3", "body": "hello"},
            {"to": USER}))
        self.assertEqual(a.risk, Risk.AMBER)
        r = self.bus.execute(a, transcript="draft it", confirmed_by="voice")
        self.assertTrue(r.ok, r.message)
        self.assertFalse(r.value["sent"])
        self.assertTrue(Path(r.value["drafted"]).exists())

    def test_reading_announces_itself_as_a_stub_and_invents_nothing(self):
        r = self.do("comms.read", {"query": "invoices"}, how=None)
        self.assertTrue(r.value["stub"])
        self.assertEqual(r.value["messages"], [])
        self.assertIn("will not invent messages", r.value["note"])

    def test_with_no_contacts_at_all_every_send_is_black(self):
        engine = PolicyEngine(PolicyConfig(guard=self.guard, known_recipients=frozenset()))
        a = engine.classify(ProposedAction(
            "comms.send", {"to": "priya@acme.example", "subject": "x", "body": "y"},
            {"to": USER}))
        self.assertEqual(a.risk, Risk.BLACK)

    def test_a_file_cannot_introduce_a_recipient(self):
        a = self.bus.propose(ProposedAction(
            "comms.send", {"to": "priya@acme.example", "subject": "x", "body": "y"},
            {"to": FROM_FILE}))
        self.assertEqual(a.risk, Risk.BLACK)
        self.assertIn("untrusted content", a.refusal_reason)

    def test_red_actions_are_never_batched(self):
        a = self.bus.propose(ProposedAction(
            "comms.send", {"to": "priya@acme.example", "subject": "1", "body": "x"},
            {"to": USER}))
        b = self.bus.propose(ProposedAction(
            "comms.send", {"to": "priya@acme.example", "subject": "2", "body": "x"},
            {"to": USER}))
        self.assertTrue(self.policy.may_execute(a, confirmed_by="click")[0])
        ok, why = self.policy.may_execute(b, confirmed_by="click")
        self.assertFalse(ok)
        self.assertIn("rate-limited", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
