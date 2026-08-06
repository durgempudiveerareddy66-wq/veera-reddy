"""The files surface — the first thing JARVIS is given hands for.

Every executor here re-checks its own paths against the PathGuard before doing
anything. The policy engine has already classified the Action and the bus has
already gated it, so this is redundant — deliberately. Defence in depth means the
component that actually writes to disk refuses on its own authority, not because
it trusts a caller upstream to have asked. If someone ever constructs an Action
without going through the policy engine, this layer still says no.

Executors return ``(result, before)``. ``before`` is the prior state the undo
record needs — the old contents of an overwritten file, where a deleted one was
moved to. Verbs that cannot capture it honestly return ``{}`` and are recorded as
irreversible rather than pretending.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import registry
from ..paths import PathGuard, Zone

# Set by configure() at startup. No default — a surface with no guard must not run.
_GUARD: PathGuard | None = None

MAX_READ_BYTES = 200_000        # a file bigger than this is summarised, not inhaled
TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".json", ".csv", ".yaml", ".yml",
                 ".html", ".css", ".xml", ".ini", ".cfg", ".toml", ".log", ".sql"}


class Refused(Exception):
    """The executor's own refusal. Distinct from a policy refusal upstream."""


def configure(guard: PathGuard) -> None:
    global _GUARD
    _GUARD = guard


def _guard() -> PathGuard:
    if _GUARD is None:
        raise Refused("files surface has no PathGuard — refusing to touch anything")
    return _GUARD


def _check(raw: str, *, writing: bool) -> Path:
    """Resolve and re-verify. Every executor calls this before it acts."""
    zone, p, why = _guard().zone(raw)
    if zone is Zone.BLACK:
        raise Refused(why)
    if writing and zone is not Zone.SAFE:
        raise Refused(f"{p} is outside the safe write zone")
    return p


def _trash() -> Path:
    t = _guard().safe_zone / ".jarvis_trash"
    t.mkdir(parents=True, exist_ok=True)
    return t


# --- read-only verbs (GREEN) -------------------------------------------------

def find(query: str, **_: Any) -> tuple[Any, dict]:
    """Search the index roots by filename. Content search comes with the vault index."""
    q = query.lower()
    hits: list[str] = []
    for root in _guard().index_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and q in p.name.lower():
                if not _guard().is_black(p)[0]:
                    hits.append(str(p))
            if len(hits) >= 200:
                break
    return {"query": query, "count": len(hits), "paths": hits[:200]}, {}


def list_(path: str, **_: Any) -> tuple[Any, dict]:
    p = _check(path, writing=False)
    if not p.is_dir():
        raise Refused(f"{p} is not a folder")
    entries = []
    for child in sorted(p.iterdir()):
        if _guard().is_black(child)[0]:
            continue        # never even name a credential file in a listing
        entries.append({
            "name": child.name,
            "dir": child.is_dir(),
            "bytes": child.stat().st_size if child.is_file() else None,
        })
    return {"path": str(p), "entries": entries}, {}


def read(path: str, **_: Any) -> tuple[Any, dict]:
    """Read a file. The result is UNTRUSTED — §8 — and says so in its own payload.

    Whatever comes back here is content, not instruction. The flag travels with
    the text so the planner cannot lose track of where it came from.
    """
    p = _check(path, writing=False)
    if not p.is_file():
        raise Refused(f"{p} is not a file")
    size = p.stat().st_size
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return {"path": str(p), "bytes": size, "untrusted": True,
                "text": "", "note": f"{p.suffix or 'no extension'} is not a text format "
                                    "this surface reads"}, {}
    raw = p.read_text(encoding="utf-8", errors="replace")
    truncated = len(raw.encode("utf-8")) > MAX_READ_BYTES
    if truncated:
        raw = raw[: MAX_READ_BYTES // 2]
    return {"path": str(p), "bytes": size, "truncated": truncated,
            "untrusted": True, "source": str(p), "text": raw}, {}


def open_(path: str, **_: Any) -> tuple[Any, dict]:
    """Hand the file to whatever the OS thinks owns it."""
    p = _check(path, writing=False)
    if not p.exists():
        raise Refused(f"{p} does not exist")
    if sys.platform == "win32":
        os.startfile(str(p))            # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(p)], check=False)
    else:
        subprocess.run(["xdg-open", str(p)], check=False)
    return {"opened": str(p)}, {}


# --- writing verbs (AMBER inside the safe zone, RED outside) -----------------

def create(path: str, content: str = "", **_: Any) -> tuple[Any, dict]:
    p = _check(path, writing=True)
    if p.exists():
        # Creating is not overwriting. If Reddie meant to replace it, that is
        # files.write, and it will capture the old contents so undo can work.
        raise Refused(f"{p} already exists — use write if you meant to replace it")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"created": str(p), "bytes": len(content.encode("utf-8"))}, {}


def write(path: str, content: str, **_: Any) -> tuple[Any, dict]:
    p = _check(path, writing=True)
    existed = p.is_file()
    before: dict[str, Any] = {"existed": existed}
    if existed:
        # Capture first. Without this the inverse cannot be built and the Action
        # is correctly reported as irreversible rather than fake-undoable.
        before["content"] = p.read_text(encoding="utf-8", errors="replace")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"written": str(p), "bytes": len(content.encode("utf-8")),
            "replaced": existed}, before


def move(src: str, dst: str, **_: Any) -> tuple[Any, dict]:
    s = _check(src, writing=True)
    d = _check(dst, writing=True)
    if not s.exists():
        raise Refused(f"{s} does not exist")
    if d.exists():
        raise Refused(f"{d} already exists — refusing to overwrite it by moving")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return {"moved": str(s), "to": str(d)}, {}


rename = move      # same operation; the two verbs exist so previews read naturally


def copy(src: str, dst: str, **_: Any) -> tuple[Any, dict]:
    s = _check(src, writing=False)      # reading the source is enough
    d = _check(dst, writing=True)
    if not s.exists():
        raise Refused(f"{s} does not exist")
    if d.exists():
        raise Refused(f"{d} already exists")
    d.parent.mkdir(parents=True, exist_ok=True)
    if s.is_dir():
        shutil.copytree(str(s), str(d))
    else:
        shutil.copy2(str(s), str(d))
    return {"copied": str(s), "to": str(d)}, {}


def zip_(src: str, dst: str, **_: Any) -> tuple[Any, dict]:
    s = _check(src, writing=False)
    d = _check(dst, writing=True)
    if not s.exists():
        raise Refused(f"{s} does not exist")
    if d.exists():
        raise Refused(f"{d} already exists")
    d.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(d, "w", zipfile.ZIP_DEFLATED) as z:
        if s.is_file():
            z.write(s, s.name)
            count = 1
        else:
            for p in s.rglob("*"):
                if p.is_file() and not _guard().is_black(p)[0]:
                    z.write(p, p.relative_to(s))
                    count += 1
    return {"zipped": str(s), "to": str(d), "files": count}, {}


def delete(path: str, **_: Any) -> tuple[Any, dict]:
    """Delete by moving to trash inside the safe zone.

    Nothing here calls unlink. An agent that can permanently destroy a file on
    one misheard word is not one worth running, so 'delete' means 'move somewhere
    recoverable' and the undo record points back at it.
    """
    p = _check(path, writing=True)
    if not p.exists():
        raise Refused(f"{p} does not exist")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = _trash() / f"{stamp}-{p.name}"
    n = 1
    while target.exists():
        target = _trash() / f"{stamp}-{n}-{p.name}"
        n += 1
    shutil.move(str(p), str(target))
    return {"deleted": str(p), "recoverable_at": str(target)}, {"trashed_to": str(target)}


def attach_all() -> None:
    """Wire these implementations to their registered verbs."""
    for verb, fn in {
        "files.find": find, "files.list": list_, "files.read": read, "files.open": open_,
        "files.create": create, "files.write": write, "files.move": move,
        "files.rename": rename, "files.copy": copy, "files.zip": zip_,
        "files.delete": delete,
    }.items():
        registry.attach(verb, fn)
