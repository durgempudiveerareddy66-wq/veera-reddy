"""The comms surface — the one that can embarrass Reddie in front of a client.

§6 sets the shape and this module does not soften it:

    Reading is GREEN. Drafting is AMBER. Sending is RED, always, with no
    exception and no batching. Building the draft is the value; pressing send
    is Reddie's.

Every adapter here is a **stub** until the Step 0 interview answers question 7 —
which email and messaging systems he actually uses, and which of them JARVIS can
reach. A stub announces itself as a stub, every single time it is used, in the
return payload and in the HUD. It never invents an inbox, a message or a contact,
because a fabricated client email is worse than no email at all.

The contact list is the load-bearing part. §3 makes sending to a recipient who
appears nowhere in Reddie's files or contacts BLACK, and with comms unconfigured
that set is **empty** — so every send is BLACK. That is the correct failure
direction: an agent that cannot reach anyone is a nuisance, an agent that emails
the wrong person is a disaster.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .. import registry
from ..paths import PathGuard

_GUARD: PathGuard | None = None

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class CommsUnavailable(Exception):
    """No real adapter is configured, with a sentence saying what is missing."""


def configure(guard: PathGuard) -> None:
    global _GUARD
    _GUARD = guard


# --- contacts ----------------------------------------------------------------

def known_recipients() -> frozenset[str]:
    """Every address that appears in the indexed files.

    This is what makes §3's unknown-recipient rule enforceable rather than
    decorative. An address JARVIS has never seen in Reddie's own material cannot
    be sent to, whatever the planner or a web page suggests.
    """
    if _GUARD is None:
        return frozenset()
    found: set[str] = set()
    for root in _GUARD.index_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            try:
                if not p.is_file() or p.suffix.lower() not in (".md", ".txt"):
                    continue
                if _GUARD.is_black(p)[0]:
                    continue
                found.update(m.group(0).lower()
                             for m in EMAIL_RE.finditer(
                                 p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return frozenset(found)


# --- drafts ------------------------------------------------------------------

def _drafts_dir() -> Path:
    d = _GUARD.safe_zone / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read(query: str = "", **_: Any) -> tuple[Any, dict]:
    """GREEN. A stub until question 7 is answered — and it says so."""
    return {
        "stub": True,
        "adapter": "none configured",
        "query": query,
        "messages": [],
        "note": (
            "This is a STUB. No mail or chat system is connected, so there is "
            "nothing to read. JARVIS will not invent messages. Answer question 7 "
            "of the interview (which apps you use, and which I can reach) and a "
            "real adapter replaces this."
        ),
        "untrusted": True,
    }, {}


def draft(to: str, subject: str, body: str, **_: Any) -> tuple[Any, dict]:
    """AMBER. Writes a real file into the safe zone. Nothing is sent."""
    if _GUARD is None:
        raise CommsUnavailable("comms surface has no PathGuard")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_to = re.sub(r"[^\w.@+-]", "_", str(to))[:60]
    path = _drafts_dir() / f"{stamp}-{safe_to}.md"
    path.write_text(
        f"To: {to}\nSubject: {subject}\nDate: {time.strftime('%Y-%m-%d %H:%M')}\n"
        f"Status: DRAFT — not sent, and JARVIS cannot send it\n\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return {
        "drafted": str(path),
        "to": to,
        "subject": subject,
        "chars": len(str(body)),
        "sent": False,
        "note": "Draft saved. Open it, read it, and send it yourself.",
    }, {"existed": False}


def send(to: str, subject: str, body: str, **_: Any) -> tuple[Any, dict]:
    """RED at the policy layer — and refused here too, until an adapter exists.

    With no adapter configured there is nothing to send *through*. Rather than
    fail somewhere deep in a transport, this stops at the top and does the useful
    thing instead: saves the draft, and tells Reddie where it is.

    §6's fallback is to compose in the app's UI and stop at the send button. That
    arrives with the real adapter. What must never happen is a send that appears
    to succeed without one.
    """
    result, _before = draft(to, subject, body)
    raise CommsUnavailable(
        f"no mail adapter is configured, so nothing was sent. The draft is at "
        f"{result['drafted']} — open it and send it yourself. "
        "Answer question 7 of the interview and JARVIS can do the composing, "
        "still stopping at the send button."
    )


def invite(to: str, when: str, subject: str, **_: Any) -> tuple[Any, dict]:
    raise CommsUnavailable(
        "no calendar adapter is configured. JARVIS will not invent an invite. "
        "Answer question 7 of the interview."
    )


def available() -> tuple[bool, str]:
    n = len(known_recipients())
    return False, (
        f"STUB — no mail or chat adapter configured. {n} contact(s) found in your "
        f"files; with none, every send is BLACK."
    )


def attach_all() -> None:
    for verb, fn in {
        "comms.read": read, "comms.draft": draft,
        "comms.send": send, "comms.invite": invite,
    }.items():
        registry.attach(verb, fn)
