"""The Action bus — propose, classify, gate, execute, record.

Nothing in JARVIS runs except through :meth:`ActionBus.execute`, and nothing
reaches :meth:`execute` except a :class:`~agent.actions.Action` that the policy
engine built. That is the whole architecture in one sentence.

In build step 1 no surfaces are attached, so every executor is ``None``. That is
not a stub or a placeholder — it is the correct state for this step, and asking
for an unattached verb produces an honest "that surface isn't wired up yet"
rather than a silent success. The spine is proved before anything is given hands.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import registry, undo as undo_mod
from .actions import Action, ProposedAction, Risk, UnknownVerb
from .journal import Entry, Journal
from .policy import PolicyEngine


class SurfaceNotAttached(Exception):
    """The verb is real and permitted, but its module isn't built yet."""


@dataclass
class Result:
    ok: bool
    outcome: str                 # executed | refused | cancelled | timed_out | failed
    message: str
    value: Any = None
    action: Optional[Action] = None
    entry: Optional[Entry] = None


class ActionBus:
    def __init__(
        self,
        policy: PolicyEngine,
        journal: Journal,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.policy = policy
        self.journal = journal
        self.on_event = on_event or (lambda kind, payload: None)

    # -- planning ----------------------------------------------------------

    def propose(self, proposed: ProposedAction) -> Action:
        """Classify one proposal. Never executes."""
        return self.policy.classify(proposed)

    def plan(self, proposals: list[ProposedAction]) -> list[Action]:
        """Classify a whole multi-step request.

        §2: the full plan is shown before step one runs. Classifying eagerly is
        what makes that possible — Reddie sees that "open the folder and message
        Priya" is two Actions, and that the second one is going to ask.
        """
        out: list[Action] = []
        for p in proposals:
            try:
                out.append(self.propose(p))
            except UnknownVerb as e:
                out.append(self._refusal_action(p, str(e)))
        self.on_event("plan", {"actions": [a.to_dict() for a in out]})
        return out

    def _refusal_action(self, p: ProposedAction, why: str) -> Action:
        from .actions import Surface
        return Action(
            verb=p.verb, surface=Surface.META, args=dict(p.args), risk=Risk.BLACK,
            preview=f"REFUSED — {why}", reversible=False, refusal_reason=why,
        )

    # -- execution ---------------------------------------------------------

    def execute(
        self,
        action: Action,
        *,
        transcript: str = "",
        confirmed_by: str | None = None,
        undoes: str = "",
    ) -> Result:
        """The single chokepoint. Gate, run, journal."""
        allowed, why = self.policy.may_execute(action, confirmed_by=confirmed_by)
        if not allowed:
            outcome = "refused" if action.risk is Risk.BLACK else "cancelled"
            entry = self.journal.record(
                action, transcript=transcript, outcome=outcome, error=why,
                dry_run=self.policy.config.dry_run, confirmed_by=confirmed_by or "",
                undoes=undoes,
            )
            self.on_event("blocked", {"action": action.to_dict(), "reason": why})
            return Result(False, outcome, why, action=action, entry=entry)

        spec = registry.get(action.verb)
        started = time.monotonic()

        if self.policy.config.dry_run:
            value, before = self._dry_run(spec, action)
            error, outcome = "", "executed"
        elif not spec.attached:
            elapsed = int((time.monotonic() - started) * 1000)
            msg = (
                f"{action.verb} is a real verb but the {spec.surface.value} surface "
                f"isn't wired up yet (build step 1 attaches no surfaces)."
            )
            entry = self.journal.record(
                action, transcript=transcript, outcome="failed", error=msg,
                duration_ms=elapsed, dry_run=False, confirmed_by=confirmed_by or "",
                undoes=undoes,
            )
            self.on_event("failed", {"action": action.to_dict(), "error": msg})
            return Result(False, "failed", msg, action=action, entry=entry)
        else:
            try:
                value, before = spec.executor(**action.args)   # type: ignore[misc]
                error, outcome = "", "executed"
            except Exception as e:                              # noqa: BLE001
                value, before, error, outcome = None, {}, f"{type(e).__name__}: {e}", "failed"

        elapsed = int((time.monotonic() - started) * 1000)

        undo_action: Optional[Action] = None
        if outcome == "executed" and spec.reversible:
            inverse = undo_mod.inverse_of(action.verb, action.args, before)
            if inverse is not None:
                # The inverse goes back through the policy engine — undo is not a
                # privileged path, it is just another Action.
                undo_action = self.policy.classify(inverse)

        entry = self.journal.record(
            action, transcript=transcript, outcome=outcome, result=value, error=error,
            duration_ms=elapsed, dry_run=self.policy.config.dry_run,
            confirmed_by=confirmed_by or "", undo=undo_action, undoes=undoes,
        )
        self.on_event(outcome, {"action": action.to_dict(), "result": value})
        return Result(outcome == "executed", outcome, error or why, value, action, entry)

    def _dry_run(self, spec: registry.VerbSpec, action: Action) -> tuple[Any, dict[str, Any]]:
        """§2: every executor prints what it would do and returns a fake success."""
        msg = f"[DRY RUN] would {spec.summary}: {spec.preview(action.args)}"
        print(msg)
        before: dict[str, Any] = {}
        # Enough synthetic prior-state for the undo record to be real in dry-run,
        # so the reversibility of a plan can be inspected before anything is armed.
        if action.verb == "files.write":
            before = {"existed": False}
        if action.verb == "files.delete":
            p = Path(str(action.args.get("path", "")))
            before = {"trashed_to": str(self.trash_dir() / p.name)}
        return {"dry_run": True, "message": msg}, before

    def trash_dir(self) -> Path:
        return self.policy.config.guard.safe_zone / ".jarvis_trash"

    # -- undo --------------------------------------------------------------

    def undo_last(self, *, confirmed_by: str | None = None) -> Result:
        """Replay the last reversible Action backwards."""
        entry = self.journal.last_undoable()
        if entry is None:
            return Result(False, "cancelled", "Nothing to undo.")
        undo_dict = entry["undo"]
        proposed = ProposedAction(
            verb=undo_dict["verb"],
            args=undo_dict["args"],
            provenance={k: undo_mod._SELF for k in undo_dict["args"]},
        )
        action = self.propose(proposed)
        # `undoes` links the reversal to what it reversed in a single append, so
        # last_undoable() will not offer the same entry twice.
        return self.execute(
            action,
            transcript=f"undo of {entry['id']} ({entry['action']['verb']})",
            confirmed_by=confirmed_by,
            undoes=entry["id"],
        )
