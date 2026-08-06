"""Build step 1 — proving the spine against fake Actions, before it has hands.

§10: *Prove tiering and the undo record against fake Actions first. This is the
spine; nothing else gets built until it holds.*

Every test here answers one question: can a plausible-looking proposal get further
than it should? Stdlib ``unittest`` only — no new dependencies at this step.

    python -m unittest discover -s jarvis/tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.actions import Origin, ProposedAction, Provenance, Risk, UnknownVerb
from agent.bus import ActionBus
from agent.journal import Journal
from agent.paths import PathGuard, Zone, is_black_windows_path
from agent.policy import PolicyConfig, PolicyEngine, RED_MIN_INTERVAL_S

USER = Provenance(Origin.USER)
FROM_FILE = Provenance(Origin.FILE, "notes/overdue.md")
FROM_WEB = Provenance(Origin.WEB, "https://supplier.example/invoice")

ALLOWED = frozenset({"git", "git status", "git add", "git commit", "git diff",
                     "git log", "python", "node", "npm run", "mkdir", "dir"})


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.safe = root / "JarvisWorkspace"
        self.index = root / "Work"
        self.outside = root / "Elsewhere"
        for d in (self.safe, self.index, self.outside):
            d.mkdir(parents=True)
        self.guard = PathGuard(safe_zone=self.safe, index_roots=(self.index,))
        self.config = PolicyConfig(
            guard=self.guard, allowed_commands=ALLOWED,
            known_recipients=frozenset({"priya@acme.example"}),
            dry_run=True, yolo=False,
        )
        self.policy = PolicyEngine(self.config)
        self.journal = Journal(root / "logs" / "actions.jsonl")
        self.bus = ActionBus(self.policy, self.journal)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def classify(self, verb: str, args: dict, prov: dict | None = None):
        return self.policy.classify(ProposedAction(verb, args, prov or {}))


# --------------------------------------------------------------------------
class TestRegistryIsClosed(Base):
    """§2: an unknown verb is a refusal, not an improvisation."""

    def test_unknown_verb_raises(self):
        with self.assertRaises(UnknownVerb) as ctx:
            self.classify("files.obliterate", {"path": "x"})
        self.assertIn("files.obliterate", str(ctx.exception))

    def test_unknown_verb_in_a_plan_becomes_a_refusal_card(self):
        plan = self.bus.plan([
            ProposedAction("files.read", {"path": str(self.index / "a.md")}, {"path": USER}),
            ProposedAction("files.encrypt_everything", {"path": "/"}, {"path": USER}),
        ])
        self.assertEqual(plan[0].risk, Risk.GREEN)
        self.assertEqual(plan[1].risk, Risk.BLACK)

    def test_missing_required_arg_is_refused(self):
        a = self.classify("files.move", {"src": str(self.safe / "a")})
        self.assertEqual(a.risk, Risk.BLACK)
        self.assertIn("missing required argument", a.refusal_reason)

    def test_planner_has_no_field_in_which_to_set_risk(self):
        # The structural guarantee behind "the model does not get a vote".
        self.assertNotIn("risk", ProposedAction.__dataclass_fields__)


# --------------------------------------------------------------------------
class TestTiering(Base):
    def test_read_is_green(self):
        a = self.classify("files.read", {"path": str(self.index / "notes.md")}, {"path": USER})
        self.assertEqual(a.risk, Risk.GREEN)

    def test_create_inside_safe_zone_is_amber(self):
        a = self.classify("files.create", {"path": str(self.safe / "new.md")}, {"path": USER})
        self.assertEqual(a.risk, Risk.AMBER)

    def test_write_outside_safe_zone_is_red(self):
        a = self.classify("files.create", {"path": str(self.outside / "new.md")}, {"path": USER})
        self.assertEqual(a.risk, Risk.RED)
        self.assertIn("outside the safe write zone", " ".join(a.reasons))

    def test_reading_outside_the_safe_zone_stays_green(self):
        # Writing outside is RED; reading is not. The tier follows the effect.
        a = self.classify("files.read", {"path": str(self.outside / "x.md")}, {"path": USER})
        self.assertEqual(a.risk, Risk.GREEN)

    def test_delete_is_red(self):
        a = self.classify("files.delete", {"path": str(self.safe / "x.md")}, {"path": USER})
        self.assertEqual(a.risk, Risk.RED)

    def test_send_is_red_even_to_a_known_contact(self):
        a = self.classify("comms.send",
                          {"to": "priya@acme.example", "subject": "Invoice", "body": "hi"},
                          {"to": USER})
        self.assertEqual(a.risk, Risk.RED)

    def test_draft_is_amber(self):
        a = self.classify("comms.draft",
                          {"to": "priya@acme.example", "subject": "Invoice", "body": "hi"},
                          {"to": USER})
        self.assertEqual(a.risk, Risk.AMBER)

    def test_preview_names_the_real_recipient(self):
        a = self.classify("comms.send",
                          {"to": "priya@acme.example", "subject": "Q3", "body": "hi"},
                          {"to": USER})
        self.assertIn("priya@acme.example", a.preview)


# --------------------------------------------------------------------------
class TestBlackPaths(Base):
    def test_windows_system_roots_are_black_on_any_platform(self):
        for p in (r"C:\Windows\System32", r"C:\Windows\System32\drivers\etc\hosts",
                  r"c:\windows\system32", r"C:\Program Files\App\x.exe",
                  r"C:\Program Files (x86)\App", r"C:\ProgramData\thing"):
            with self.subTest(p=p):
                self.assertTrue(is_black_windows_path(p), p)

    def test_short_83_names_are_black_too(self):
        # Found on Windows: C:\Users\VEERAR~1\... and C:\Users\veera reddy\...
        # are the same folder. A string comparison sees two paths; an attacker
        # sees one bypass.
        for p in (r"C:\PROGRA~1\App\x.exe", r"C:\PROGRA~2\App",
                  r"c:\progra~1\app", r"C:\DOCUME~1\user"):
            with self.subTest(p=p):
                self.assertTrue(is_black_windows_path(p), p)

    def test_ordinary_windows_paths_are_not_black(self):
        for p in (r"C:\Users\Reddie\JarvisWorkspace\a.md", r"D:\Work\invoice.pdf",
                  r"C:\WindowsApps-Notes\mine.md"):
            with self.subTest(p=p):
                self.assertFalse(is_black_windows_path(p), p)

    def test_windows_path_argument_is_refused_even_arriving_on_linux(self):
        a = self.classify("files.read", {"path": r"C:\Windows\System32\config\SAM"},
                          {"path": USER})
        self.assertEqual(a.risk, Risk.BLACK)

    def test_credential_files_are_black_anywhere(self):
        for name in ("id_rsa", "server.pem", "vault.kdbx", ".env", "secrets.yaml",
                     "Login Data"):
            with self.subTest(name=name):
                a = self.classify("files.read", {"path": str(self.safe / name)},
                                  {"path": USER})
                self.assertEqual(a.risk, Risk.BLACK, name)

    def test_ssh_directory_is_black(self):
        a = self.classify("files.list", {"path": str(Path.home() / ".ssh")}, {"path": USER})
        self.assertEqual(a.risk, Risk.BLACK)

    def test_traversal_out_of_the_safe_zone_is_caught_after_resolution(self):
        sneaky = str(self.safe / ".." / ".." / ".." / "etc" / "passwd")
        a = self.classify("files.read", {"path": sneaky}, {"path": USER})
        self.assertEqual(a.risk, Risk.BLACK)

    @unittest.skipIf(sys.platform == "win32", "symlink creation needs privileges on Windows")
    def test_symlink_out_of_the_safe_zone_is_caught(self):
        # The string is inside the safe zone. The path is not. resolve() is why.
        link = self.safe / "shortcut"
        link.symlink_to("/etc")
        zone, resolved, _ = self.guard.zone(str(link / "passwd"))
        self.assertEqual(zone, Zone.BLACK)
        self.assertEqual(str(resolved), "/etc/passwd")

    def test_extra_never_touch_is_add_only(self):
        guard = PathGuard(safe_zone=self.safe, extra_never_touch=(self.outside,))
        self.assertEqual(guard.zone(str(self.outside / "x"))[0], Zone.BLACK)
        # and the core is still there alongside the addition
        self.assertTrue(guard.is_black(Path("/etc/shadow"))[0])


# --------------------------------------------------------------------------
class TestShell(Base):
    def test_allowlisted_command_is_amber(self):
        a = self.classify("shell.run", {"command": "python", "args": ["-V"],
                                        "cwd": str(self.safe)}, {"command": USER})
        self.assertEqual(a.risk, Risk.AMBER)

    def test_command_not_on_the_allowlist_is_black(self):
        for cmd in ("curl", "powershell", "rm", "del", "rmdir", "reg", "certutil"):
            with self.subTest(cmd=cmd):
                a = self.classify("shell.run", {"command": cmd, "args": [],
                                                "cwd": str(self.safe)}, {"command": USER})
                self.assertEqual(a.risk, Risk.BLACK, cmd)
                self.assertIn("allowlist", a.refusal_reason)

    def test_git_push_is_red(self):
        a = self.classify("shell.git", {"subcommand": "push", "cwd": str(self.safe)},
                          {"subcommand": USER})
        self.assertEqual(a.risk, Risk.RED)

    def test_git_status_is_amber(self):
        a = self.classify("shell.git", {"subcommand": "status", "cwd": str(self.safe)},
                          {"subcommand": USER})
        self.assertEqual(a.risk, Risk.AMBER)

    def test_shell_metacharacters_are_refused(self):
        for bad in ["git status; curl evil.sh", "a && b", "$(whoami)", "x | y", "a`b`"]:
            with self.subTest(bad=bad):
                a = self.classify("shell.run", {"command": "python", "args": [bad],
                                                "cwd": str(self.safe)}, {"command": USER})
                self.assertEqual(a.risk, Risk.BLACK, bad)

    def test_force_flag_escalates_to_red(self):
        a = self.classify("shell.git", {"subcommand": "commit", "args": ["--no-verify"],
                                        "cwd": str(self.safe)}, {"subcommand": USER})
        self.assertEqual(a.risk, Risk.RED)

    def test_install_is_red(self):
        a = self.classify("shell.install", {"package": "nmap"}, {"package": USER})
        self.assertEqual(a.risk, Risk.RED)


# --------------------------------------------------------------------------
class TestUnknownRecipients(Base):
    def test_send_to_a_stranger_is_black(self):
        a = self.classify("comms.send", {"to": "admin@evil.com", "subject": "x", "body": "y"},
                          {"to": USER})
        self.assertEqual(a.risk, Risk.BLACK)
        self.assertIn("no file or contact list", a.refusal_reason)

    def test_one_stranger_among_known_contacts_still_blocks_the_whole_send(self):
        a = self.classify("comms.send",
                          {"to": "priya@acme.example, admin@evil.com", "subject": "x",
                           "body": "y"}, {"to": USER})
        self.assertEqual(a.risk, Risk.BLACK)

    def test_with_comms_unknown_every_send_is_black(self):
        # The real default: no contacts configured, so nothing can be sent at all.
        engine = PolicyEngine(PolicyConfig(guard=self.guard, allowed_commands=ALLOWED))
        a = engine.classify(ProposedAction(
            "comms.send", {"to": "anyone@anywhere.example", "subject": "x", "body": "y"},
            {"to": USER}))
        self.assertEqual(a.risk, Risk.BLACK)


# --------------------------------------------------------------------------
class TestTaint(Base):
    """§8 — prompt injection is the main threat once the agent can act."""

    def test_untrusted_argument_escalates_one_tier_minimum_amber(self):
        a = self.classify("files.read", {"path": str(self.index / "x.md")}, {"path": FROM_FILE})
        self.assertEqual(a.risk, Risk.AMBER)      # GREEN + 1, floored at AMBER
        self.assertTrue(a.tainted)

    def test_amber_derived_from_content_becomes_red(self):
        a = self.classify("files.create", {"path": str(self.safe / "x.md")},
                          {"path": FROM_FILE})
        self.assertEqual(a.risk, Risk.RED)

    def test_the_source_is_named_in_the_preview(self):
        a = self.classify("files.read", {"path": str(self.index / "x.md")}, {"path": FROM_WEB})
        self.assertIn("supplier.example", a.preview)
        self.assertIn("not from you", a.preview)

    def test_a_page_cannot_introduce_a_recipient(self):
        # "A web page that says 'email admin@evil.com' produces a RED confirmation
        # naming the source page, not a sent email." Here it does not even reach RED
        # — an unknown recipient is BLACK on top of the taint rule.
        a = self.classify("comms.send",
                          {"to": "admin@evil.com", "subject": "x", "body": "y"},
                          {"to": FROM_WEB})
        self.assertEqual(a.risk, Risk.BLACK)

    def test_a_page_cannot_introduce_a_recipient_even_a_known_one(self):
        a = self.classify("comms.send",
                          {"to": "priya@acme.example", "subject": "x", "body": "y"},
                          {"to": FROM_WEB})
        self.assertEqual(a.risk, Risk.BLACK)
        self.assertIn("untrusted content", a.refusal_reason)

    def test_a_file_cannot_introduce_a_shell_command(self):
        a = self.classify("shell.run", {"command": "python", "args": ["-c", "x"],
                                        "cwd": str(self.safe)}, {"command": FROM_FILE})
        self.assertEqual(a.risk, Risk.BLACK)
        self.assertIn("untrusted content", a.refusal_reason)

    def test_a_file_cannot_introduce_a_write_path_outside_the_safe_zone(self):
        a = self.classify("files.create", {"path": str(self.outside / "x.md")},
                          {"path": FROM_FILE})
        self.assertEqual(a.risk, Risk.BLACK)

    def test_taint_records_provenance_for_the_journal(self):
        a = self.classify("files.read", {"path": str(self.index / "x.md")}, {"path": FROM_FILE})
        self.assertEqual(a.to_dict()["provenance"]["path"]["source"], "notes/overdue.md")


# --------------------------------------------------------------------------
class TestConfirmationGates(Base):
    def test_green_needs_nothing(self):
        a = self.classify("files.read", {"path": str(self.index / "x")}, {"path": USER})
        self.assertTrue(self.policy.may_execute(a)[0])

    def test_amber_needs_assent_and_voice_is_enough(self):
        a = self.classify("files.create", {"path": str(self.safe / "x")}, {"path": USER})
        self.assertFalse(self.policy.may_execute(a)[0])
        self.assertTrue(self.policy.may_execute(a, confirmed_by="voice")[0])

    def test_red_is_not_released_by_voice(self):
        a = self.classify("files.delete", {"path": str(self.safe / "x")}, {"path": USER})
        ok, why = self.policy.may_execute(a, confirmed_by="voice")
        self.assertFalse(ok)
        self.assertIn("click or the 4-character code", why)

    def test_red_is_released_by_a_click(self):
        a = self.classify("files.delete", {"path": str(self.safe / "x")}, {"path": USER})
        self.assertTrue(self.policy.may_execute(a, confirmed_by="click")[0])

    def test_red_is_released_by_a_typed_code(self):
        a = self.classify("files.delete", {"path": str(self.safe / "x")}, {"path": USER})
        self.assertTrue(self.policy.may_execute(a, confirmed_by="code")[0])

    def test_red_is_rate_limited_and_never_batched(self):
        a = self.classify("files.delete", {"path": str(self.safe / "a")}, {"path": USER})
        b = self.classify("files.delete", {"path": str(self.safe / "b")}, {"path": USER})
        self.assertTrue(self.policy.may_execute(a, confirmed_by="click")[0])
        ok, why = self.policy.may_execute(b, confirmed_by="click")
        self.assertFalse(ok)
        self.assertIn("rate-limited", why)

    def test_black_is_released_by_nothing(self):
        a = self.classify("files.read", {"path": r"C:\Windows\System32\config\SAM"},
                          {"path": USER})
        for how in (None, "voice", "click", "code"):
            with self.subTest(how=how):
                self.assertFalse(self.policy.may_execute(a, confirmed_by=how)[0])

    def test_yolo_releases_red_but_never_black(self):
        engine = PolicyEngine(PolicyConfig(guard=self.guard, allowed_commands=ALLOWED,
                                           known_recipients=frozenset(), yolo=True))
        red = engine.classify(ProposedAction("files.delete", {"path": str(self.safe / "x")},
                                             {"path": USER}))
        self.assertEqual(red.risk, Risk.RED)
        self.assertTrue(engine.may_execute(red)[0])

        black = engine.classify(ProposedAction(
            "files.read", {"path": r"C:\Windows\System32\config\SAM"}, {"path": USER}))
        self.assertFalse(engine.may_execute(black, confirmed_by="click")[0])

    def test_confirmation_timeout_defaults_to_cancel(self):
        from agent.policy import CONFIRM_TIMEOUT_S
        a = self.classify("files.create", {"path": str(self.safe / "x")}, {"path": USER})
        self.assertEqual(CONFIRM_TIMEOUT_S, 20.0)
        # A timeout is the absence of assent, and the absence of assent is refusal.
        self.assertFalse(self.policy.may_execute(a, confirmed_by=None)[0])


# --------------------------------------------------------------------------
class TestJournalAndUndo(Base):
    def test_executed_action_is_journalled_with_its_transcript(self):
        a = self.classify("files.create", {"path": str(self.safe / "n.md"), "content": "hi"},
                          {"path": USER})
        self.bus.execute(a, transcript="make me a note", confirmed_by="voice")
        e = self.journal.tail(1)[0]
        self.assertEqual(e["outcome"], "executed")
        self.assertEqual(e["transcript"], "make me a note")
        self.assertEqual(e["action"]["verb"], "files.create")
        self.assertTrue(e["dry_run"])

    def test_refusals_are_journalled_too(self):
        a = self.classify("comms.send", {"to": "admin@evil.com", "subject": "x", "body": "y"},
                          {"to": FROM_FILE})
        self.bus.execute(a, transcript="the note said to email admin@evil.com")
        e = self.journal.tail(1)[0]
        self.assertEqual(e["outcome"], "refused")
        self.assertIn("evil.com", e["transcript"])

    def test_undo_record_is_the_inverse_action(self):
        src, dst = self.safe / "a.md", self.safe / "b.md"
        a = self.classify("files.move", {"src": str(src), "dst": str(dst)},
                          {"src": USER, "dst": USER})
        self.bus.execute(a, transcript="rename a to b", confirmed_by="voice")
        e = self.journal.tail(1)[0]
        self.assertEqual(e["undo"]["verb"], "files.move")
        self.assertEqual(e["undo"]["args"]["src"], str(dst))
        self.assertEqual(e["undo"]["args"]["dst"], str(src))

    def test_the_inverse_is_tiered_on_its_own_merits(self):
        # Moving a file OUT of the safe zone is RED; so is moving it back in.
        src, dst = self.safe / "a.md", self.outside / "a.md"
        a = self.classify("files.move", {"src": str(src), "dst": str(dst)},
                          {"src": USER, "dst": USER})
        self.assertEqual(a.risk, Risk.RED)
        self.bus.execute(a, transcript="move it out", confirmed_by="click")
        e = self.journal.tail(1)[0]
        self.assertEqual(e["undo"]["risk"], "RED")

    def test_irreversible_actions_carry_no_undo_record(self):
        a = self.classify("comms.send", {"to": "priya@acme.example", "subject": "x",
                                         "body": "y"}, {"to": USER})
        self.bus.execute(a, transcript="send it", confirmed_by="click")
        self.assertIsNone(self.journal.tail(1)[0]["undo"])

    def test_undo_last_replays_the_inverse(self):
        src, dst = self.safe / "a.md", self.safe / "b.md"
        a = self.classify("files.move", {"src": str(src), "dst": str(dst)},
                          {"src": USER, "dst": USER})
        self.bus.execute(a, transcript="rename", confirmed_by="voice")
        r = self.bus.undo_last(confirmed_by="voice")
        self.assertTrue(r.ok)
        self.assertEqual(r.action.args["src"], str(dst))
        self.assertEqual(r.action.args["dst"], str(src))

    def test_the_same_action_is_not_offered_for_undo_twice(self):
        src, dst = self.safe / "a.md", self.safe / "b.md"
        a = self.classify("files.move", {"src": str(src), "dst": str(dst)},
                          {"src": USER, "dst": USER})
        self.bus.execute(a, transcript="rename", confirmed_by="voice")
        self.assertTrue(self.bus.undo_last(confirmed_by="voice").ok)
        # The reversal is itself reversible, so undo/redo alternates rather than
        # walking the same entry twice — what must never happen is the ORIGINAL
        # entry being offered again.
        first_id = a.id
        nxt = self.journal.last_undoable()
        self.assertNotEqual(nxt["id"] if nxt else None, first_id)

    def test_nothing_to_undo_is_an_honest_answer(self):
        r = self.bus.undo_last(confirmed_by="click")
        self.assertFalse(r.ok)
        self.assertEqual(r.message, "Nothing to undo.")

    def test_journal_survives_a_torn_final_line(self):
        a = self.classify("files.create", {"path": str(self.safe / "n.md")}, {"path": USER})
        self.bus.execute(a, transcript="note", confirmed_by="voice")
        with self.journal.path.open("a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "incomp')
        self.assertEqual(len(self.journal.tail(50)), 1)


# --------------------------------------------------------------------------
class TestDryRun(Base):
    def test_dry_run_is_on_by_default_from_env(self):
        os.environ.pop("JARVIS_DRYRUN", None)
        cfg = PolicyConfig.from_env(self.guard, Path("jarvis/config"))
        self.assertTrue(cfg.dry_run)

    def test_dry_run_marks_the_preview(self):
        a = self.classify("files.create", {"path": str(self.safe / "x")}, {"path": USER})
        self.assertTrue(a.preview.startswith("[DRY RUN]"))

    def test_unattached_surface_fails_loudly_when_not_dry_running(self):
        cfg = PolicyConfig(guard=self.guard, allowed_commands=ALLOWED, dry_run=False)
        bus = ActionBus(PolicyEngine(cfg), self.journal)
        a = bus.propose(ProposedAction("files.create", {"path": str(self.safe / "x")},
                                       {"path": USER}))
        r = bus.execute(a, transcript="make it", confirmed_by="voice")
        self.assertFalse(r.ok)
        self.assertIn("isn't wired up yet", r.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
