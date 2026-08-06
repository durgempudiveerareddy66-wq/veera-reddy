"""The policy engine — the component that decides what JARVIS is allowed to do.

This is the part of the build that must not be weakened. Everything here follows
from one principle: **the model does not get a vote on its own risk.** It proposes
a verb and arguments; this module decides the tier, from the Action's shape, in
code that can be read and tested.

Order of judgement matters and is deliberate:

1.  Unknown verb, or missing required args → refuse. No improvisation.
2.  BLACK checks, first and unconditionally. Nothing later can lower a BLACK.
3.  Base risk from the registry — the floor.
4.  Structural escalation: writing outside the safe zone, off-allowlist commands,
    sending to an unknown recipient.
5.  Taint escalation (§8): +1 tier, minimum AMBER, for anything derived from
    content JARVIS read rather than Reddie said.

A tier can only ever go **up** through this pipeline. There is no code path that
lowers one, and `JARVIS_YOLO` does not change tiers — it auto-confirms them,
which the journal records as such.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .actions import Action, Origin, ProposedAction, Provenance, Risk, Surface, UnknownVerb
from .paths import PathGuard, Zone
from . import registry

# Shell metacharacters. §6: "No shell metacharacters passed through." Because we
# run with shell=False these are inert as *characters*, but their presence in an
# argument means the planner is trying to compose a command line rather than fill
# a parameter — which is the thing we refuse.
_METACHARS = re.compile(r"[;&|<>`$\n\r\\]|\$\(|\|\|")

# Commands that stay RED even when allowlisted, because they publish or destroy.
_ALWAYS_RED_GIT = {"push", "reset", "clean", "rebase"}
_FORCE_FLAGS = {"--force", "-f", "--hard", "--no-verify", "--force-with-lease"}


@dataclass
class PolicyConfig:
    """Everything the engine needs, injected so tests can build a fake machine."""

    guard: PathGuard
    allowed_commands: frozenset[str] = frozenset()
    known_recipients: frozenset[str] = frozenset()
    dry_run: bool = True
    yolo: bool = False

    @classmethod
    def from_env(cls, guard: PathGuard, config_dir: Path) -> "PolicyConfig":
        return cls(
            guard=guard,
            allowed_commands=load_allowlist(config_dir / "allowed_commands.txt"),
            known_recipients=frozenset(),  # populated once comms is wired (step 6)
            dry_run=os.environ.get("JARVIS_DRYRUN", "1") != "0",
            yolo=os.environ.get("JARVIS_YOLO", "0") == "1",
        )


def load_allowlist(path: Path) -> frozenset[str]:
    """Allowlist, not denylist. A denylist loses to encoding, chaining and aliases."""
    if not path.exists():
        return frozenset()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line.lower())
    return frozenset(out)


@dataclass
class Verdict:
    """The engine's reasoning, kept so the HUD can show *why* a tier was chosen."""

    risk: Risk
    reasons: list[str] = field(default_factory=list)
    refusal: str = ""

    def raise_to(self, tier: Risk, why: str) -> None:
        if tier.value > self.risk.value:
            self.risk = tier
            self.reasons.append(why)
        elif why not in self.reasons:
            self.reasons.append(why)

    def black(self, why: str) -> None:
        self.risk = Risk.BLACK
        self.refusal = why
        self.reasons.append(why)


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config
        self._last_red_at: float = 0.0

    # -- the entry point ---------------------------------------------------

    def classify(self, proposed: ProposedAction) -> Action:
        """Turn a proposal into a classified Action. Raises UnknownVerb, never guesses."""
        spec = registry.get(proposed.verb)          # UnknownVerb propagates — §2
        args = dict(proposed.args)
        v = Verdict(risk=spec.base_risk)

        missing = [a for a in spec.required_args if a not in args or args[a] in (None, "")]
        if missing:
            v.black(f"{proposed.verb} is missing required argument(s): {', '.join(missing)}")

        if v.risk is not Risk.BLACK:
            self._check_paths(spec, args, v)
        if v.risk is not Risk.BLACK:
            self._check_shell(spec, args, v)
        if v.risk is not Risk.BLACK:
            self._check_comms(spec, args, v)

        tainted, sources = self._check_taint(spec, proposed, v)

        return Action(
            verb=spec.verb,
            surface=spec.surface,
            args=args,
            risk=v.risk,
            preview=self._preview(spec, args, v, tainted, sources),
            reversible=spec.reversible and v.risk is not Risk.BLACK,
            undo=None,          # built by the executor, which knows the prior state
            provenance=dict(proposed.provenance),
            tainted=tainted,
            taint_sources=sources,
            reasons=tuple(v.reasons),
            refusal_reason=v.refusal,
        )

    # -- checks ------------------------------------------------------------

    def _check_paths(self, spec: registry.VerbSpec, args: dict[str, Any], v: Verdict) -> None:
        for arg in spec.path_args:
            raw = args.get(arg)
            if raw in (None, ""):
                continue
            zone, resolved, why = self.config.guard.zone(raw)
            args[arg] = str(resolved)          # store the resolved path, not the string
            if zone is Zone.BLACK:
                v.black(why)
                return
            if spec.writes and zone is not Zone.SAFE:
                # §3: writing outside the safe write zone is RED.
                v.raise_to(Risk.RED, f"{resolved} is outside the safe write zone")

    def _check_shell(self, spec: registry.VerbSpec, args: dict[str, Any], v: Verdict) -> None:
        if spec.surface is not Surface.SHELL:
            return
        if spec.verb == "shell.install":
            return                              # already RED in the registry

        argv = [str(x) for x in args.get("args", [])]

        if spec.verb == "shell.git":
            sub = str(args.get("subcommand", "")).lower()
            if "git" not in self.config.allowed_commands:
                v.black("git is not on the allowlist")
                return
            if sub in _ALWAYS_RED_GIT:
                v.raise_to(Risk.RED, f"`git {sub}` publishes or discards work")
            elif f"git {sub}" not in self.config.allowed_commands:
                v.black(f"`git {sub}` is not on the allowlist")
                return
        elif spec.verb == "shell.run":
            cmd = str(args.get("command", "")).lower()
            if cmd not in self.config.allowed_commands:
                # §6: not on the allowlist is a refusal, not a confirmation prompt.
                v.black(f"`{cmd}` is not on the allowlist (config/allowed_commands.txt)")
                return

        for token in [str(args.get("command", "")), str(args.get("subcommand", ""))] + argv:
            if _METACHARS.search(token):
                v.black(f"shell metacharacters in {token!r} — commands are argv, not strings")
                return
            if token.lower() in _FORCE_FLAGS:
                v.raise_to(Risk.RED, f"`{token}` overrides a safety check")

    def _check_comms(self, spec: registry.VerbSpec, args: dict[str, Any], v: Verdict) -> None:
        if spec.surface is not Surface.COMMS:
            return
        recipients = _recipients(args.get("to"))
        if not recipients:
            return
        unknown = [r for r in recipients if not self._known(r)]
        if unknown and spec.verb in ("comms.send", "comms.invite"):
            # §3 BLACK: sending to a recipient that appears nowhere in Reddie's
            # files or contacts. With comms UNKNOWN, the contact set is empty and
            # every send is BLACK — which is the correct failure direction.
            v.black(
                f"{', '.join(unknown)} appears in no file or contact list JARVIS can see"
            )

    def _known(self, recipient: str) -> bool:
        return recipient.lower() in {r.lower() for r in self.config.known_recipients}

    def _check_taint(
        self, spec: registry.VerbSpec, proposed: ProposedAction, v: Verdict
    ) -> tuple[bool, tuple[str, ...]]:
        """§8. Anything derived from content JARVIS read is escalated and named."""
        tainted_args = proposed.tainted_args()
        if not tainted_args:
            return False, ()

        sources = tuple(dict.fromkeys(p.describe() for p in tainted_args.values()))

        # Hard stops: untrusted content may never *introduce* these three things.
        for arg, prov in tainted_args.items():
            if spec.surface is Surface.COMMS and arg == "to":
                v.black(
                    f"a recipient introduced by {prov.describe()} — untrusted content "
                    "cannot name who JARVIS talks to"
                )
                return True, sources
            if spec.surface is Surface.SHELL and arg in ("command", "subcommand", "args"):
                v.black(
                    f"a command introduced by {prov.describe()} — untrusted content "
                    "cannot name what JARVIS runs"
                )
                return True, sources
            if arg in spec.path_args and spec.writes:
                zone, resolved, _ = self.config.guard.zone(proposed.args.get(arg, ""))
                if zone is not Zone.SAFE:
                    v.black(
                        f"a write path outside the safe zone introduced by "
                        f"{prov.describe()}"
                    )
                    return True, sources

        if v.risk is not Risk.BLACK:
            # The general rule: one full tier, floor of AMBER.
            new = v.risk.escalate(1, floor=Risk.AMBER)
            v.risk = new
            v.reasons.append(
                f"escalated to {new.name}: arguments derived from untrusted content "
                f"({', '.join(sources)})"
            )
        return True, sources

    # -- presentation ------------------------------------------------------

    def _preview(
        self,
        spec: registry.VerbSpec,
        args: dict[str, Any],
        v: Verdict,
        tainted: bool,
        sources: tuple[str, ...],
    ) -> str:
        try:
            base = spec.preview(args)
        except Exception:  # a broken template must not take the policy engine down
            base = f"{spec.verb} with {args!r}"
        if v.risk is Risk.BLACK:
            return f"REFUSED — {v.refusal}"
        if tainted:
            base += f"  [derived from {', '.join(sources)} — not from you]"
        if self.config.dry_run:
            base = "[DRY RUN] " + base
        return base

    # -- confirmation gates ------------------------------------------------

    def may_execute(self, action: Action, *, confirmed_by: str | None = None) -> tuple[bool, str]:
        """Final gate. Called immediately before the executor, never earlier.

        ``confirmed_by`` is ``"click"``, ``"code"`` or ``"voice"``. The distinction
        is the whole of §3's RED rule: voice assent alone can never release a RED
        Action, because a misheard word must not be able to email a client.
        """
        if action.risk is Risk.BLACK:
            return False, action.refusal_reason or "refused"

        if action.risk is Risk.GREEN:
            return True, "green — runs immediately"

        if self.config.yolo:
            # Never applies to BLACK — that branch returned above.
            return True, f"JARVIS_YOLO=1 auto-confirmed a {action.risk.name} action"

        if action.risk is Risk.AMBER:
            if confirmed_by in ("voice", "click", "code"):
                return True, f"amber confirmed by {confirmed_by}"
            return False, "amber — waiting for your yes"

        # RED
        if confirmed_by == "voice":
            return False, (
                "RED needs a click or the 4-character code. Voice alone cannot "
                "release this."
            )
        if confirmed_by not in ("click", "code"):
            return False, "RED — waiting for a click or the code"
        gap = time.monotonic() - self._last_red_at
        if gap < RED_MIN_INTERVAL_S:
            return False, (
                f"RED actions are rate-limited to one per {RED_MIN_INTERVAL_S:.0f}s "
                f"— {RED_MIN_INTERVAL_S - gap:.1f}s to go"
            )
        self._last_red_at = time.monotonic()
        return True, f"red confirmed by {confirmed_by}"


RED_MIN_INTERVAL_S = 10.0
CONFIRM_TIMEOUT_S = 20.0        # §3/§7: defaults to cancel, never to proceed


def _recipients(to: Any) -> list[str]:
    if to in (None, ""):
        return []
    if isinstance(to, (list, tuple)):
        return [str(x).strip() for x in to if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;]", str(to)) if x.strip()]
