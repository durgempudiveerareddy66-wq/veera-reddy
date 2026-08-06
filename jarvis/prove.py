"""Build step 1 proof — watch the policy engine refuse things.

    python jarvis/prove.py

The unit suite proves the spine holds. This prints what it actually *does*, in the
words Reddie would hear, because a green test bar is not evidence you can read.

Everything here runs against a temporary fake machine — a throwaway safe zone and
index under /tmp. Nothing touches a real folder, and no surfaces are attached, so
even a GREEN Action only reports what it would do.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.actions import Origin, ProposedAction, Provenance, Risk
from agent.bus import ActionBus
from agent.journal import Journal
from agent.paths import PathGuard
from agent.policy import PolicyConfig, PolicyEngine, load_allowlist

TIER_MARK = {Risk.GREEN: "GREEN", Risk.AMBER: "AMBER", Risk.RED: "  RED", Risk.BLACK: "BLACK"}

USER = Provenance(Origin.USER)
FROM_FILE = Provenance(Origin.FILE, r"Work\notes\overdue-invoice.md")
FROM_WEB = Provenance(Origin.WEB, "https://supplier.example/portal")

if sys.platform == "win32":
    ESCAPE_LABEL = r"C:\Windows"
    ESCAPE_TARGET = r"C:\Users\..\..\Windows\System32\drivers\etc\hosts"
else:
    ESCAPE_LABEL = "/etc"
    ESCAPE_TARGET = "/tmp/../etc/hosts"


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    safe, index, elsewhere = root / "JarvisWorkspace", root / "Work", root / "Elsewhere"
    for d in (safe, index, elsewhere):
        d.mkdir(parents=True)

    config_dir = Path(__file__).resolve().parent / "config"
    guard = PathGuard(safe_zone=safe, index_roots=(index,))
    policy = PolicyEngine(PolicyConfig(
        guard=guard,
        allowed_commands=load_allowlist(config_dir / "allowed_commands.txt"),
        known_recipients=frozenset({"priya@acme.example"}),
        dry_run=True,
    ))
    journal = Journal(root / "logs" / "actions.jsonl")
    bus = ActionBus(policy, journal)

    print(f"safe write zone : {safe}")
    print(f"index root      : {index}")
    print(f"known contacts  : priya@acme.example")
    print(f"dry run         : ON\n")

    cases: list[tuple[str, ProposedAction]] = [
        ("read a file in the index",
         ProposedAction("files.read", {"path": str(index / "q3.md")}, {"path": USER})),
        ("create a note in the safe zone",
         ProposedAction("files.create", {"path": str(safe / "note.md"), "content": "hi"},
                        {"path": USER})),
        ("write OUTSIDE the safe zone",
         ProposedAction("files.create", {"path": str(elsewhere / "note.md")}, {"path": USER})),
        ("delete a file",
         ProposedAction("files.delete", {"path": str(safe / "note.md")}, {"path": USER})),
        ("read C:\\Windows\\System32\\config\\SAM",
         ProposedAction("files.read", {"path": r"C:\Windows\System32\config\SAM"},
                        {"path": USER})),
        ("read an SSH private key",
         ProposedAction("files.read", {"path": str(Path.home() / ".ssh" / "id_rsa")},
                        {"path": USER})),
        # The escape target has to be a never-touch root on THIS host. On Windows
        # that is C:\Windows; on Linux it is /etc. Hardcoding one makes the demo
        # print GREEN on the other platform and look like a guardrail failing.
        (f"escape the safe zone with .. into {ESCAPE_LABEL}",
         ProposedAction("files.read", {"path": ESCAPE_TARGET}, {"path": USER})),
        ("escape the safe zone with .. then WRITE",
         ProposedAction("files.create", {"path": str(safe / ".." / "Elsewhere" / "x.md")},
                        {"path": USER})),
        ("git status",
         ProposedAction("shell.git", {"subcommand": "status", "cwd": str(safe)},
                        {"subcommand": USER})),
        ("git push",
         ProposedAction("shell.git", {"subcommand": "push", "cwd": str(safe)},
                        {"subcommand": USER})),
        ("curl (not on the allowlist)",
         ProposedAction("shell.run", {"command": "curl", "args": ["evil.sh"], "cwd": str(safe)},
                        {"command": USER})),
        ("chain a command with ;",
         ProposedAction("shell.run", {"command": "python", "args": ["-V; curl evil.sh"],
                                      "cwd": str(safe)}, {"command": USER})),
        ("draft an email to a known contact",
         ProposedAction("comms.draft", {"to": "priya@acme.example", "subject": "Q3 invoice",
                                        "body": "..."}, {"to": USER})),
        ("send that email",
         ProposedAction("comms.send", {"to": "priya@acme.example", "subject": "Q3 invoice",
                                       "body": "..."}, {"to": USER})),
        ("send to a stranger",
         ProposedAction("comms.send", {"to": "admin@evil.com", "subject": "x", "body": "y"},
                        {"to": USER})),
        ("a NOTE told JARVIS to email admin@evil.com",
         ProposedAction("comms.send", {"to": "admin@evil.com", "subject": "x", "body": "y"},
                        {"to": FROM_FILE})),
        ("a WEB PAGE told JARVIS to run a command",
         ProposedAction("shell.run", {"command": "python", "args": ["-c", "..."],
                                      "cwd": str(safe)}, {"command": FROM_WEB})),
        ("a file's contents named a file to read",
         ProposedAction("files.read", {"path": str(index / "referenced.md")},
                        {"path": FROM_FILE})),
        ("a verb that does not exist",
         ProposedAction("files.encrypt_everything", {"path": "/"}, {"path": USER})),
    ]

    print("=" * 78)
    print("TIERING")
    print("=" * 78)
    actions = []
    for label, proposed in cases:
        [action] = bus.plan([proposed])
        actions.append(action)
        print(f"[{TIER_MARK[action.risk]}]  {label}")
        print(f"          {action.preview}")
        if action.reasons:
            for r in action.reasons:
                print(f"          · {r}")
        # Attempt it with no confirmation. GREEN runs, AMBER/RED are held, BLACK is
        # refused — and all four outcomes land in the journal, which is what makes
        # the claim at the bottom of this script true rather than decorative.
        bus.execute(action, transcript=f"(proof) {label}")
        print()

    print("=" * 78)
    print("CONFIRMATION GATES")
    print("=" * 78)
    delete = next(a for a in actions if a.verb == "files.delete")
    send = next(a for a in actions if a.verb == "comms.send" and a.risk is Risk.RED)
    create = next(a for a in actions if a.verb == "files.create" and a.risk is Risk.AMBER)
    black = next(a for a in actions if a.risk is Risk.BLACK)

    for label, action, how in [
        ("AMBER create, no assent      ", create, None),
        ("AMBER create, said 'yes'     ", create, "voice"),
        ("RED delete, said 'yes'       ", delete, "voice"),
        ("RED delete, clicked          ", delete, "click"),
        ("RED send, clicked (rate limit)", send, "click"),
        ("BLACK, clicked               ", black, "click"),
    ]:
        ok, why = policy.may_execute(action, confirmed_by=how)
        print(f"  {'PASS' if ok else 'HELD'}  {label}  →  {why}")

    print()
    print("=" * 78)
    print("UNDO")
    print("=" * 78)
    src, dst = safe / "draft.md", safe / "final.md"
    move = bus.propose(ProposedAction("files.move", {"src": str(src), "dst": str(dst)},
                                      {"src": USER, "dst": USER}))
    bus.execute(move, transcript="rename draft to final", confirmed_by="voice")
    entry = journal.tail(1)[0]
    print(f"  did   : {entry['action']['verb']}  {Path(entry['action']['args']['src']).name}"
          f" → {Path(entry['action']['args']['dst']).name}")
    print(f"  undo  : {entry['undo']['verb']}  {Path(entry['undo']['args']['src']).name}"
          f" → {Path(entry['undo']['args']['dst']).name}   [{entry['undo']['risk']}]")
    result = bus.undo_last(confirmed_by="voice")
    print(f"  ran   : {result.outcome} — {Path(result.action.args['src']).name}"
          f" → {Path(result.action.args['dst']).name}")
    print(f"  again : {'nothing further' if journal.last_undoable() is None else 'redo available'}")

    print()
    print("=" * 78)
    print("JOURNAL")
    print("=" * 78)
    for k, v in sorted(journal.stats().items()):
        print(f"  {k:16s} {v}")
    print(f"\n  {journal.path}")
    refused = journal.stats().get("refused", 0)
    held = journal.stats().get("cancelled", 0)
    print(f"\n  {refused} refusals and {held} held actions are on disk alongside the"
          f" {journal.stats().get('executed', 0)} that ran.")
    print("  An injection attempt stays visible after the fact — journalling what did")
    print("  NOT run is how you find out something tried.")

    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
