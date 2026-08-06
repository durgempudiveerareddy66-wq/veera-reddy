"""The journal — an append-only record of everything JARVIS actually did.

Every executed Action lands in ``logs/actions.jsonl`` with the transcript that
caused it, the arguments it ran with, where each argument came from, how long it
took, and the inverse Action needed to put things back.

Two properties are load-bearing:

*   **Append-only.** Entries are never rewritten. An undo does not edit the
    original entry; it appends a new one that points back at it. The history of
    what happened stays true even after the effects are reversed.
*   **Refusals are recorded too.** A BLACK Action that never ran is still journalled,
    because "what did it try to do" matters as much as "what did it do" — that
    record is how a prompt-injection attempt becomes visible after the fact.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .actions import Action, Risk


@dataclass
class Entry:
    """One journal line."""

    ts: float
    transcript: str
    action: dict[str, Any]
    outcome: str                  # executed | refused | cancelled | timed_out | failed
    result: Any = None
    error: str = ""
    duration_ms: int = 0
    dry_run: bool = True
    confirmed_by: str = ""
    undo: Optional[dict[str, Any]] = None
    undoes: str = ""              # id of the entry this one reverses
    id: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
                "id": self.id,
                "transcript": self.transcript,
                "outcome": self.outcome,
                "confirmed_by": self.confirmed_by,
                "dry_run": self.dry_run,
                "duration_ms": self.duration_ms,
                "action": self.action,
                "result": self.result,
                "error": self.error,
                "undo": self.undo,
                "undoes": self.undoes,
            },
            ensure_ascii=False,
        )


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writing -----------------------------------------------------------

    def append(self, entry: Entry) -> Entry:
        if not entry.id:
            entry.id = entry.action.get("id", "") or f"e{int(entry.ts*1000):x}"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())      # a crash mid-action must not lose the record
        return entry

    def record(
        self,
        action: Action,
        *,
        transcript: str,
        outcome: str,
        result: Any = None,
        error: str = "",
        duration_ms: int = 0,
        dry_run: bool = True,
        confirmed_by: str = "",
        undo: Action | None = None,
        undoes: str = "",
    ) -> Entry:
        return self.append(
            Entry(
                ts=time.time(),
                transcript=transcript,
                action=action.to_dict(),
                outcome=outcome,
                result=result,
                error=error,
                duration_ms=duration_ms,
                dry_run=dry_run,
                confirmed_by=confirmed_by,
                undo=undo.to_dict() if undo is not None else None,
                undoes=undoes,
                id=action.id,
            )
        )

    # -- reading -----------------------------------------------------------

    def entries(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn final line must not break history

    def last_undoable(self) -> Optional[dict[str, Any]]:
        """The most recent executed, reversible entry that hasn't been undone yet."""
        all_entries = list(self.entries())
        already_undone = {e["undoes"] for e in all_entries if e.get("undoes")}
        for e in reversed(all_entries):
            if e.get("outcome") != "executed":
                continue
            if not e.get("undo"):
                continue
            if e["id"] in already_undone:
                continue
            return e
        return None

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return list(self.entries())[-n:]

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries():
            counts[e.get("outcome", "?")] = counts.get(e.get("outcome", "?"), 0) + 1
            risk = (e.get("action") or {}).get("risk")
            if risk:
                counts[f"risk_{risk}"] = counts.get(f"risk_{risk}", 0) + 1
        return counts
