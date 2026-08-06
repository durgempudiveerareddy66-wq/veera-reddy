"""Step 2's text box — drive the file and app surfaces by typing.

    python jarvis/run.py

There is no model here yet. This is a fixed command parser, and it says so, because
§4 is explicit that keyword matching must never impersonate the planner. Grok —
Gemini, in this build — arrives with the HUD. What this proves is that the Action
bus, the policy engine, the confirmation gates and the journal work end to end
against a real filesystem.

Dry-run is ON unless you set JARVIS_DRYRUN=0 in .env. Leave it on for a while.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.actions import Origin, ProposedAction, Provenance, Risk
from agent.runtime import build

USER = Provenance(Origin.USER)      # you typed it, so it is not tainted

BANNER = r"""
   J A R V I S   ·   step 2   ·   files + apps
   type 'help' for commands, 'quit' to leave
"""

HELP = """
  find <query>              search the index by filename
  list <path>               list a folder
  read <path>               read a text file
  open <path>               open in the default app
  create <path> [text]      make a new file
  write <path> <text>       replace a file's contents
  move <src> <dst>          move or rename
  copy <src> <dst>          copy
  zip <src> <dst>           compress
  delete <path>             delete (moves to trash, recoverable)
  launch <app>              start an application
  windows                   list open windows
  focus <window>            bring a window to the front

  undo                      reverse the last reversible action
  journal                   show the last 10 entries
  where                     show the safe zone and index roots
  dryrun                    show whether dry-run is on
"""

# verb, then how to turn the typed arguments into Action args.
COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "find":   ("files.find",   ("query",)),
    "list":   ("files.list",   ("path",)),
    "read":   ("files.read",   ("path",)),
    "open":   ("files.open",   ("path",)),
    "create": ("files.create", ("path", "content")),
    "write":  ("files.write",  ("path", "content")),
    "move":   ("files.move",   ("src", "dst")),
    "rename": ("files.rename", ("src", "dst")),
    "copy":   ("files.copy",   ("src", "dst")),
    "zip":    ("files.zip",    ("src", "dst")),
    "delete": ("files.delete", ("path",)),
    "launch": ("apps.launch",  ("app",)),
    "focus":  ("apps.focus",   ("window",)),
    "windows": ("apps.list_windows", ()),
}

TIER = {Risk.GREEN: "GREEN", Risk.AMBER: "AMBER", Risk.RED: "RED", Risk.BLACK: "BLACK"}


def confirm(action) -> str | None:
    """Ask, the way the HUD will. Anything that isn't assent is a cancel."""
    print(f"\n  [{TIER[action.risk]}]  {action.preview}")
    for r in action.reasons:
        print(f"         · {r}")

    if action.risk is Risk.AMBER:
        answer = input("         yes / no > ").strip().lower()
        return "voice" if answer in ("y", "yes", "ok", "go", "do it") else None

    # RED. Voice-equivalent assent is not enough — §3. A typed code stands in for
    # the HUD's click, and it is deliberately not something you can say by accident.
    code = f"{action.id[:4].upper()}"
    print(f"         RED. Type the code {code} to confirm. Anything else cancels.")
    return "code" if input("         code > ").strip().upper() == code else None


def show(result) -> None:
    if result.ok:
        print(f"         done — {result.value}")
    else:
        print(f"         {result.outcome} — {result.message}")


def main() -> int:
    rt = build()
    print(BANNER)
    print(f"  safe write zone : {rt.guard.safe_zone}")
    print(f"  index roots     : {', '.join(str(r) for r in rt.guard.index_roots)}")
    print(f"  surfaces        : {', '.join(rt.attached)}")
    print(f"  dry run         : {'ON — nothing will really happen' if rt.dry_run else 'OFF'}")
    for n in rt.notes:
        print(f"  ! {n}")
    print()

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("quit", "exit"):
            return 0
        if line == "help":
            print(HELP)
            continue
        if line == "where":
            print(f"  safe : {rt.guard.safe_zone}")
            for r in rt.guard.index_roots:
                print(f"  index: {r}")
            continue
        if line == "dryrun":
            print(f"  dry run is {'ON' if rt.dry_run else 'OFF'} "
                  f"(set JARVIS_DRYRUN in .env)")
            continue
        if line == "journal":
            for e in rt.journal.tail(10):
                print(f"  {e['iso']}  {e['outcome']:9s} {e['action']['risk']:5s} "
                      f"{e['action']['verb']}")
            continue
        if line == "undo":
            show(rt.bus.undo_last(confirmed_by="click"))
            continue

        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"  couldn't parse that: {e}")
            continue

        cmd, rest = parts[0].lower(), parts[1:]
        if cmd not in COMMANDS:
            print(f"  I don't have a command called {cmd!r}. This is a fixed parser, "
                  f"not the planner — 'help' lists everything it knows.")
            continue

        verb, argnames = COMMANDS[cmd]
        args: dict[str, str] = {}
        for i, name in enumerate(argnames):
            if i == len(argnames) - 1 and name in ("content", "query"):
                args[name] = " ".join(rest[i:]) if len(rest) > i else ""
            elif i < len(rest):
                args[name] = rest[i]
        prov = {k: USER for k in args}

        action = rt.bus.propose(ProposedAction(verb, args, prov))

        if action.risk is Risk.BLACK:
            print(f"\n  [BLACK]  {action.preview}")
            rt.bus.execute(action, transcript=line)
            print()
            continue

        if action.risk is Risk.GREEN:
            show(rt.bus.execute(action, transcript=line))
            print()
            continue

        how = confirm(action)
        if how is None:
            print("         cancelled.")
            rt.bus.execute(action, transcript=line, confirmed_by=None)
            print()
            continue
        show(rt.bus.execute(action, transcript=line, confirmed_by=how))
        print()


if __name__ == "__main__":
    raise SystemExit(main())
