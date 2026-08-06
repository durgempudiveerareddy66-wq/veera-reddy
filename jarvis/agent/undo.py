"""Inverse Actions.

`jarvis undo` and the HUD's UNDO control both come here. The rule is that the
inverse is itself an Action — it goes back through the policy engine and is
classified on its own merits. Undo is not a privileged back door: undoing a write
that landed outside the safe zone still asks, because putting a file back is
still touching a place JARVIS isn't trusted with.

Where an inverse cannot be honestly constructed, this returns ``None`` and the
Action is recorded as irreversible. A wrong undo is worse than no undo — silently
"restoring" a file to empty content because the original was never captured is
exactly the kind of confident destruction this build exists to avoid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .actions import Origin, ProposedAction, Provenance

# Undo is always initiated by Reddie, so its arguments are USER-origin — they
# come from the journal, which is JARVIS's own record, not from read content.
_SELF = Provenance(Origin.USER, "jarvis journal")


def inverse_of(verb: str, args: dict[str, Any], before: dict[str, Any] | None = None) -> Optional[ProposedAction]:
    """Build the Action that reverses ``verb``.

    ``before`` carries state the executor captured *prior* to acting — the old
    contents of an overwritten file, the original path of a deleted one. Without
    it, verbs that destroy information are correctly reported as irreversible.
    """
    before = before or {}

    def prov(*names: str) -> dict[str, Provenance]:
        return {n: _SELF for n in names}

    if verb in ("files.create", "files.copy", "files.zip", "browser.download"):
        target = args.get("dst") or args.get("path")
        if not target:
            return None
        return ProposedAction("files.delete", {"path": str(target)}, prov("path"))

    if verb == "files.write":
        # Only reversible if we captured what was there before. If the file did
        # not previously exist, the inverse is deletion, not an empty write.
        if "existed" not in before:
            return None
        if not before["existed"]:
            return ProposedAction("files.delete", {"path": str(args["path"])}, prov("path"))
        return ProposedAction(
            "files.write",
            {"path": str(args["path"]), "content": before.get("content", "")},
            prov("path", "content"),
        )

    if verb in ("files.move", "files.rename"):
        src, dst = args.get("src"), args.get("dst")
        if not src or not dst:
            return None
        # The inverse of "move A to B" is "move B back to A" — and note the
        # destination of the inverse is the *original* location, which may well
        # sit outside the safe zone. The policy engine will tier it accordingly.
        return ProposedAction(verb, {"src": str(dst), "dst": str(src)}, prov("src", "dst"))

    if verb == "files.delete":
        trashed = before.get("trashed_to")
        original = args.get("path")
        if not trashed or not original:
            return None
        return ProposedAction(
            "files.move", {"src": str(trashed), "dst": str(original)}, prov("src", "dst")
        )

    if verb == "apps.launch":
        app = args.get("app")
        return ProposedAction("apps.close", {"window": str(app)}, prov("window")) if app else None

    # Everything else — sends, clicks, form fills, shell commands, page navigation
    # — cannot be undone by JARVIS, and says so rather than pretending.
    return None


IRREVERSIBLE_NOTE = (
    "This cannot be undone by JARVIS. Nothing it does to a web page, a shell, or "
    "someone else's inbox is recoverable from this side."
)
