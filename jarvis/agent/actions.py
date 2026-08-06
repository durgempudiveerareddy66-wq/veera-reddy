"""The Action — the only thing the model is allowed to produce.

The planner does not run code, write shell strings, or touch the OS. It emits
:class:`ProposedAction` objects naming a verb from the registry. Everything after
that is JARVIS's own machinery: the policy engine classifies, the executor runs,
the journal records.

Note what is *absent* from :class:`ProposedAction`: a risk tier. The model has no
field in which to express an opinion about how dangerous its own suggestion is,
because it does not get one. Risk arrives only when the policy engine returns a
:class:`Action`, and it is computed from the Action's shape.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Risk(Enum):
    """The four tiers of §3. Ordered, so escalation is arithmetic."""

    GREEN = 0   # runs immediately, announced after the fact
    AMBER = 1   # spoken preview + card, one word of assent releases it
    RED = 2     # click or typed code required; voice alone is never sufficient
    BLACK = 3   # refused, always, no phrasing override

    def __lt__(self, other: "Risk") -> bool:
        return self.value < other.value

    def __ge__(self, other: "Risk") -> bool:
        return self.value >= other.value

    def escalate(self, steps: int = 1, floor: "Risk | None" = None) -> "Risk":
        """Raise by ``steps`` tiers, never above BLACK, never below ``floor``."""
        out = Risk(min(self.value + steps, Risk.BLACK.value))
        if floor is not None and out.value < floor.value:
            out = floor
        return out


class Surface(Enum):
    FILES = "files"
    APPS = "apps"
    BROWSER = "browser"
    SHELL = "shell"
    COMMS = "comms"
    META = "meta"      # undo, brief, plan — JARVIS talking about itself


class Origin(Enum):
    """Where an argument value came from. Drives taint escalation in §8."""

    USER = "user"              # Reddie said it, by voice or keyboard
    MODEL = "model"            # the planner composed it from user intent
    FILE = "file"              # read out of an indexed file — UNTRUSTED
    WEB = "web"                # scraped from a page — UNTRUSTED
    EMAIL = "email"            # read out of a message — UNTRUSTED
    SHELL_OUT = "shell_out"    # captured from command output — UNTRUSTED

    @property
    def untrusted(self) -> bool:
        return self in (Origin.FILE, Origin.WEB, Origin.EMAIL, Origin.SHELL_OUT)


@dataclass(frozen=True)
class Provenance:
    """Where one argument's value came from, and from exactly which source."""

    origin: Origin
    source: str = ""   # the file path, URL or command that produced it

    @property
    def untrusted(self) -> bool:
        return self.origin.untrusted

    def describe(self) -> str:
        if self.origin is Origin.USER:
            return "you said so"
        if self.origin is Origin.MODEL:
            return "composed from what you asked"
        return f"{self.origin.value}: {self.source}"


@dataclass
class ProposedAction:
    """What the planner emits. No risk field, deliberately."""

    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Provenance] = field(default_factory=dict)

    def origin_of(self, arg: str) -> Provenance:
        return self.provenance.get(arg, Provenance(Origin.MODEL))

    def tainted_args(self) -> dict[str, Provenance]:
        return {k: p for k, p in self.provenance.items() if p.untrusted}


@dataclass
class Action:
    """A classified, executable Action. Only the policy engine builds these."""

    verb: str
    surface: Surface
    args: dict[str, Any]
    risk: Risk
    preview: str                  # one human sentence, real paths, real names
    reversible: bool
    undo: Optional["Action"] = None
    provenance: dict[str, Provenance] = field(default_factory=dict)
    tainted: bool = False
    taint_sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()  # why this tier — shown on the HUD card
    refusal_reason: str = ""      # populated when risk is BLACK
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    @property
    def refused(self) -> bool:
        return self.risk is Risk.BLACK

    @property
    def needs_confirmation(self) -> bool:
        return self.risk in (Risk.AMBER, Risk.RED)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form, for the journal and the HUD's SSE stream."""
        d: dict[str, Any] = {
            "id": self.id,
            "verb": self.verb,
            "surface": self.surface.value,
            "args": _jsonable(self.args),
            "risk": self.risk.name,
            "preview": self.preview,
            "reversible": self.reversible,
            "tainted": self.tainted,
            "taint_sources": list(self.taint_sources),
            "reasons": list(self.reasons),
            "provenance": {
                k: {"origin": p.origin.value, "source": p.source}
                for k, p in self.provenance.items()
            },
            "created_at": self.created_at,
        }
        if self.refusal_reason:
            d["refusal_reason"] = self.refusal_reason
        if self.undo is not None:
            d["undo"] = self.undo.to_dict()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _jsonable(v: Any) -> Any:
    """Args may hold Paths and Enums; the journal must stay plain JSON."""
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


class UnknownVerb(Exception):
    """The planner named a verb that is not in the registry.

    §2: *an unknown verb is a refusal, not an improvisation.* This is raised
    rather than guessed at, and surfaces to Reddie as a refusal with the verb
    named, so a hallucinated capability is visible instead of silently dropped.
    """

    def __init__(self, verb: str):
        super().__init__(f"unknown verb {verb!r} — refusing rather than improvising")
        self.verb = verb
