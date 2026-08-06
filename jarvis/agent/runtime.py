"""Assembling a running JARVIS from .env and config/.

One place builds the guard, the policy, the journal, the bus, and attaches
whichever surfaces exist. The HUD server (step 3) and the text box (step 2) both
come here rather than each wiring their own — so there is exactly one answer to
"what is the safe zone", and it is not duplicated into something that can drift.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .brain import Brain
from .bus import ActionBus
from .journal import Journal
from .paths import PathGuard, default_safe_zone, load_extra_never_touch
from .policy import PolicyConfig, PolicyEngine, load_allowlist

ROOT = Path(__file__).resolve().parent.parent      # jarvis/


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Read .env without a dependency. Values already in the environment win."""
    path = path or (ROOT / ".env")
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        out[k] = v
        os.environ.setdefault(k, v)
    return out


def _split_paths(raw: str) -> tuple[Path, ...]:
    """Windows separates with ';'. Splitting on ':' would break 'C:\\...'."""
    sep = ";" if os.name == "nt" or ";" in raw else os.pathsep
    return tuple(Path(p.strip()) for p in raw.split(sep) if p.strip())


@dataclass
class Runtime:
    guard: PathGuard
    policy: PolicyEngine
    journal: Journal
    bus: ActionBus
    brain: Brain
    attached: tuple[str, ...]
    notes: tuple[str, ...]      # things Reddie should know — degrade loudly

    @property
    def dry_run(self) -> bool:
        return self.policy.config.dry_run


def build(*, on_event=None) -> Runtime:
    load_dotenv()
    notes: list[str] = []

    raw_safe = os.environ.get("JARVIS_SAFE_ZONE", "").strip()
    if raw_safe:
        safe = Path(raw_safe)
    else:
        safe = default_safe_zone()
        notes.append(
            f"safe write zone is my default, {safe} — you have not set one. "
            "Set JARVIS_SAFE_ZONE in .env to change it."
        )
    safe.mkdir(parents=True, exist_ok=True)

    raw_roots = os.environ.get("JARVIS_ROOTS", "").strip()
    if raw_roots:
        roots = _split_paths(raw_roots)
    else:
        roots = (safe, ROOT / "data" / "demo_vault")
        notes.append(
            "index roots are my default — the safe zone plus the DEMO vault. "
            "None of your own folders are indexed. Anything the demo vault says "
            "about clients or invoices is fiction. Set JARVIS_ROOTS to point at "
            "real folders."
        )

    guard = PathGuard(
        safe_zone=safe,
        index_roots=roots,
        extra_never_touch=load_extra_never_touch(ROOT / "config" / "never_touch.txt"),
    )

    # A guard that quietly blacklists everything looks exactly like an empty
    # machine, so its complaints go straight to the telemetry strip.
    for w in guard.warnings:
        notes.append(w)

    policy = PolicyEngine(PolicyConfig.from_env(guard, ROOT / "config"))
    if policy.config.yolo:
        notes.append(
            "JARVIS_YOLO=1 — AMBER and RED are being auto-confirmed without asking. "
            "This is a bad idea. It still does not apply to BLACK."
        )
    if not policy.config.allowed_commands:
        notes.append("config/allowed_commands.txt is empty — every shell command is BLACK.")

    journal = Journal(ROOT / "logs" / "actions.jsonl")
    bus = ActionBus(policy, journal, on_event=on_event)

    attached: list[str] = []
    from .surfaces import files as files_surface
    files_surface.configure(guard)
    files_surface.attach_all()
    attached.append("files")

    from .surfaces import apps as apps_surface
    apps_surface.attach_all()
    ok, why = apps_surface.available()
    attached.append("apps")
    if not ok:
        notes.append(f"apps surface is attached but inert: {why}")

    from .surfaces import browser as browser_surface
    browser_surface.configure(guard)
    browser_surface.attach_all()
    attached.append("browser")
    b_ok, b_why = browser_surface.available()
    if not b_ok:
        notes.append(f"browser surface is attached but inert: {b_why}")
    elif browser_surface._using_main_profile():
        notes.append(
            "BROWSER_USE_MAIN_PROFILE=1 — JARVIS is attached to your REAL Chrome "
            "profile, with your live bank and client sessions in it."
        )

    from .surfaces import shell as shell_surface
    shell_surface.configure(guard, policy.config.allowed_commands)
    shell_surface.attach_all()
    attached.append("shell")
    s_ok, s_why = shell_surface.available()
    if not s_ok:
        notes.append(f"shell surface: {s_why}")

    from .surfaces import comms as comms_surface
    comms_surface.configure(guard)
    comms_surface.attach_all()
    attached.append("comms")
    # §3's unknown-recipient rule needs a contact set to mean anything. It is
    # harvested from the indexed files — nothing else is trusted to name a person.
    contacts = comms_surface.known_recipients()
    policy.config.known_recipients = contacts
    c_ok, c_why = comms_surface.available()
    notes.append(f"comms surface: {c_why}")

    brain = Brain()
    if not brain.api_key:
        notes.append(
            "no GEMINI_API_KEY — the planner is unavailable. Fixed command shapes "
            "still work and everything else gets an honest refusal."
        )
    elif not brain.ollama_model:
        notes.append(
            "no OLLAMA_MODEL set — if Gemini is unreachable there is no local "
            "fallback, only the fixed command shapes."
        )

    return Runtime(guard, policy, journal, bus, brain, tuple(attached), tuple(notes))
