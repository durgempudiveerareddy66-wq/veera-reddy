"""The shell surface — running commands, deliberately built last.

Everything here follows from one decision: **allowlist, not denylist.** A denylist
is defeated by encoding, chaining and aliases — you cannot enumerate every
spelling of a bad command, but you can enumerate the good ones. Anything not in
``config/allowed_commands.txt`` is BLACK: refused outright, not escalated to a
confirmation Reddie might wave through at the end of a long day.

The other three rules, each of which closes a specific escape:

*   ``shell=False``, always, with args as a list. There is no string for a shell
    to reinterpret, so ``;``, ``|``, ``$()`` and backticks are inert characters
    rather than syntax. The policy engine refuses them anyway, one layer up.
*   ``cwd`` pinned to the safe write zone unless Reddie named somewhere else.
*   A 60-second timeout, and output captured and returned rather than dropped.

Note what this module does NOT contain: any code path that builds a command from
a string. If a verb wanted that, it would not be addable here without rewriting
the executor.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import registry
from ..paths import PathGuard, Zone

TIMEOUT_S = 60
MAX_OUTPUT = 40_000

# Flags that turn an allowlisted interpreter into arbitrary code execution.
#
# This is the hole an allowlist of command NAMES cannot see. `python` is a
# perfectly reasonable entry — running a script in the safe zone is the point.
# But `python -c "import os; os.system(...)"` is a shell with extra steps, and
# it is spelled with a command that is on the list. Found by the step-6 tests,
# where my own test case reached for `python -c` without thinking.
#
# Running a script FILE is still allowed; the file has to exist somewhere JARVIS
# was permitted to write, which is a boundary that already means something.
_CODE_FLAGS: dict[str, tuple[str, ...]] = {
    "python":     ("-c", "-m"),
    "python3":    ("-c", "-m"),
    "py":         ("-c", "-m"),
    "node":       ("-e", "--eval", "-p", "--print"),
    "powershell": ("-c", "-command", "-encodedcommand", "-e"),
    "pwsh":       ("-c", "-command", "-encodedcommand", "-e"),
    "cmd":        ("/c", "/k"),
    "perl":       ("-e",),
    "ruby":       ("-e",),
    "php":        ("-r",),
}

_GUARD: PathGuard | None = None
_ALLOWED: frozenset[str] = frozenset()


class ShellRefused(Exception):
    """The executor's own refusal, independent of the policy engine."""


def configure(guard: PathGuard, allowed: frozenset[str]) -> None:
    global _GUARD, _ALLOWED
    _GUARD, _ALLOWED = guard, allowed


def _cwd(raw: str | None) -> Path:
    """Pin the working directory. Defaults to the safe zone, never to os.getcwd()."""
    if _GUARD is None:
        raise ShellRefused("shell surface has no PathGuard — refusing to run anything")
    if not raw:
        return _GUARD.safe_zone
    zone, p, why = _GUARD.zone(raw)
    if zone is Zone.BLACK:
        raise ShellRefused(why)
    if not p.is_dir():
        raise ShellRefused(f"{p} is not a folder")
    return p


def _run(argv: list[str], cwd: Path) -> dict[str, Any]:
    """The single place a subprocess is created. shell=False, no exceptions."""
    # Defence in depth: the policy engine already refused metacharacters, and
    # shell=False makes them inert. Both, because either alone is one mistake
    # away from a command line.
    for tok in argv:
        if any(c in tok for c in ";&|<>`$\n\r"):
            raise ShellRefused(
                f"shell metacharacters in {tok!r} — commands here are argv, not strings"
            )
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), shell=False, capture_output=True, text=True,
            timeout=TIMEOUT_S,
            # A command must not inherit JARVIS's own secrets.
            env={k: v for k, v in os.environ.items()
                 if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))},
        )
    except FileNotFoundError:
        raise ShellRefused(f"{argv[0]!r} is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise ShellRefused(f"{argv[0]!r} did not finish within {TIMEOUT_S}s — killed") from None
    return {
        "argv": argv,
        "cwd": str(cwd),
        "exit": proc.returncode,
        "stdout": proc.stdout[:MAX_OUTPUT],
        "stderr": proc.stderr[:MAX_OUTPUT],
        # Command output is content JARVIS read, not something Reddie said (§8).
        "untrusted": True,
        "source": f"shell: {' '.join(argv)}",
    }


def run(command: str, args: Any = (), cwd: str | None = None, **_: Any) -> tuple[Any, dict]:
    cmd = str(command).strip()
    if cmd.lower() not in _ALLOWED:
        raise ShellRefused(
            f"{cmd!r} is not on the allowlist. Add it to config/allowed_commands.txt "
            "if you actually want JARVIS able to run it."
        )
    argv = [cmd] + [str(a) for a in (args or ())]
    _refuse_inline_code(cmd, argv[1:])
    return _run(argv, _cwd(cwd)), {}


def _refuse_inline_code(cmd: str, args: list[str]) -> None:
    """Refuse the flags that make an allowlisted interpreter a general shell."""
    flags = _CODE_FLAGS.get(Path(cmd).stem.lower())
    if not flags:
        return
    for a in args:
        if a.lower() in flags:
            raise ShellRefused(
                f"`{cmd} {a}` runs code given on the command line, which makes the "
                f"allowlist meaningless — {cmd} is allowed so it can run a script "
                "in your safe zone, not so it can be a shell. Put the code in a "
                "file and run the file."
            )


def git(subcommand: str, args: Any = (), cwd: str | None = None, **_: Any) -> tuple[Any, dict]:
    sub = str(subcommand).strip().lower()
    if "git" not in _ALLOWED:
        raise ShellRefused("git is not on the allowlist")
    if f"git {sub}" not in _ALLOWED and sub not in ("push", "reset", "clean", "rebase"):
        raise ShellRefused(f"`git {sub}` is not on the allowlist")
    argv = ["git", sub] + [str(a) for a in (args or ())]
    return _run(argv, _cwd(cwd)), {}


def scaffold(template: str, dst: str, **_: Any) -> tuple[Any, dict]:
    """Create a project skeleton. Files only — no package manager, no network.

    Deliberately not `npm create` or `npx`: those download and execute arbitrary
    code from the internet, which is not something an allowlist of command names
    can meaningfully constrain.
    """
    if _GUARD is None:
        raise ShellRefused("shell surface has no PathGuard")
    zone, root, why = _GUARD.zone(dst)
    if zone is Zone.BLACK:
        raise ShellRefused(why)
    if zone is not Zone.SAFE:
        raise ShellRefused(f"{root} is outside the safe write zone")
    if root.exists() and any(root.iterdir()):
        raise ShellRefused(f"{root} already exists and is not empty")

    t = str(template).strip().lower()
    files = _TEMPLATES.get(t)
    if files is None:
        raise ShellRefused(
            f"no template called {t!r}. Known: {', '.join(sorted(_TEMPLATES))}"
        )
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(str(p))
    return {"scaffolded": t, "at": str(root), "files": written}, {}


_TEMPLATES: dict[str, dict[str, str]] = {
    "landing": {
        "index.html": (
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            "<title>New landing page</title>\n<link rel=\"stylesheet\" href=\"style.css\">\n"
            "</head>\n<body>\n<main>\n  <h1>Headline goes here</h1>\n"
            "  <p>One sentence saying what this is.</p>\n</main>\n</body>\n</html>\n"
        ),
        "style.css": (
            ":root{--ink:#111;--bg:#fff}\n*{box-sizing:border-box;margin:0}\n"
            "body{font:16px/1.6 system-ui,sans-serif;color:var(--ink);background:var(--bg)}\n"
            "main{max-width:64ch;margin:12vh auto;padding:0 6vw}\n"
            "h1{font-size:clamp(2rem,6vw,3.5rem);line-height:1.1;margin-bottom:.5em}\n"
        ),
    },
    "python": {
        "main.py": '"""New project."""\n\n\ndef main() -> int:\n    print("hello")\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n',
        "README.md": "# New project\n\n    python main.py\n",
        ".gitignore": "__pycache__/\n*.pyc\n.venv/\n.env\n",
    },
    "node": {
        "index.js": "console.log('hello');\n",
        "package.json": '{\n  "name": "new-project",\n  "version": "0.1.0",\n  "private": true,\n  "type": "module",\n  "scripts": { "start": "node index.js" }\n}\n',
        ".gitignore": "node_modules/\n.env\n",
    },
}


def install(package: str, **_: Any) -> tuple[Any, dict]:
    """Installing software is RED, and this executor still refuses by default.

    §3 lists installing software as RED, so a confirmation could release it. But
    a confirmed install runs arbitrary setup code as Reddie, which is a larger
    grant than any other verb here. It stays off until he turns it on knowingly.
    """
    if os.environ.get("JARVIS_ALLOW_INSTALL", "0") != "1":
        raise ShellRefused(
            f"installing {package!r} is off. A package install runs arbitrary "
            "setup code as you. Set JARVIS_ALLOW_INSTALL=1 in .env if you mean it."
        )
    return _run([sys.executable, "-m", "pip", "install", str(package)],
                _cwd(None)), {}


def available() -> tuple[bool, str]:
    if not _ALLOWED:
        return False, "config/allowed_commands.txt is empty — every command is BLACK"
    return True, f"{len(_ALLOWED)} allowlisted"


def attach_all() -> None:
    for verb, fn in {
        "shell.run": run, "shell.git": git,
        "shell.scaffold": scaffold, "shell.install": install,
    }.items():
        registry.attach(verb, fn)
