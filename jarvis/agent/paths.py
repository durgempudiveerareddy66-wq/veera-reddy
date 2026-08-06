"""Path resolution and zone enforcement.

This module is the reason JARVIS is allowed near a filesystem. Everything that
decides *where* an Action may touch lives here, and nothing else is permitted to
make that judgement.

Two rules shape the whole design:

1.  **Resolve before you judge.** A path is only ever classified after
    ``.resolve()`` has collapsed ``..``, symlinks and Windows junctions. Judging
    the string the planner handed us is how a sandbox gets walked out of:
    ``workspace/notes/../../../.ssh/id_rsa`` is inside the safe zone as a string
    and very much outside it as a path.

2.  **BLACK is checked first and cannot be configured away.** The never-touch set
    is assembled from a hardcoded core plus whatever ``config/never_touch.txt``
    adds. The config file may only ever *add*. There is deliberately no code path
    that removes an entry from the core.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PureWindowsPath

WINDOWS = sys.platform == "win32"


class Zone(Enum):
    """Where a resolved path sits relative to JARVIS's permissions."""

    SAFE = "safe"          # inside the safe write zone — AMBER to write
    INDEXED = "indexed"    # a read-only index root — GREEN to read, RED to write
    OUTSIDE = "outside"    # ordinary user path — RED to write, GREEN to read
    BLACK = "black"        # never-touch — refused outright, read or write


# --- The hardcoded BLACK core -------------------------------------------------
# Windows system locations. Held as PureWindowsPath so the rule is testable on
# any platform: proving "C:\Windows\System32 is BLACK" must not require Windows.

_WINDOWS_SYSTEM_ROOTS: tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    # 8.3 short names for the same directories. Windows still resolves these, so
    # C:\PROGRA~1\App and C:\Program Files\App are one folder with two spellings —
    # and a string comparison sees two different paths. Found on Reddie's machine,
    # where a temp dir came back as C:\Users\VEERAR~1\... while every resolved
    # preview said C:\Users\veera reddy\...
    #
    # On Windows the resolve()-then-compare pass below would catch these anyway.
    # They are listed here so the *first* check catches them too, and so the rule
    # holds on a non-Windows host, where resolve() cannot expand them at all.
    # ~1..~3 because which suffix maps to which directory depends on creation
    # order, and all three of these live at the root of C:.
    r"C:\PROGRA~1",
    r"C:\PROGRA~2",
    r"C:\PROGRA~3",
    r"C:\DOCUME~1",          # legacy Documents and Settings junction
)

# Paths relative to the user's home that hold credentials or live sessions.
_HOME_RELATIVE_BLACK: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".gitconfig",
    ".docker",
    ".kube",
    "AppData/Local/Google/Chrome/User Data",
    "AppData/Local/Microsoft/Edge/User Data",
    "AppData/Roaming/Mozilla/Firefox/Profiles",
    "AppData/Local/BraveSoftware",
    ".config/google-chrome",
    ".mozilla/firefox",
)

# Filenames and extensions that are BLACK wherever they are found. A private key
# does not become safe by being moved into the workspace.
_BLACK_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.env(\..+)?$", re.I),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)$", re.I),
    re.compile(r".*\.(pem|key|ppk|pfx|p12|kdbx|keychain|jks)$", re.I),
    re.compile(r"^(shadow|passwd|sudoers)$"),
    re.compile(r"^(Login Data|Cookies|Web Data)$", re.I),  # Chromium credential DBs
    re.compile(r"^secrets?\.(ya?ml|json|toml)$", re.I),
)


def is_black_windows_path(raw: str) -> bool:
    """True if a Windows path string falls under a hardcoded system root.

    Pure and platform-independent by design — this is the check the §11 guardrail
    proof exercises, and that proof has to be runnable on the build machine.
    """
    p = PureWindowsPath(os.path.expandvars(raw))
    # PureWindowsPath already folds separators; casefold handles Windows'
    # case-insensitivity. parts[0] is the drive ("c:\\") once casefolded.
    parts = [q.casefold() for q in p.parts]
    for root in _WINDOWS_SYSTEM_ROOTS:
        rparts = [q.casefold() for q in PureWindowsPath(root).parts]
        if parts[: len(rparts)] == rparts:
            return True
    return False


_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")   # C:\... or a \\UNC\share


def _looks_windows_absolute(s: str) -> bool:
    """True for drive-letter and UNC paths, regardless of the host we run on."""
    return bool(_WINDOWS_ABS.match(os.path.expandvars(s)))


def _is_absolute(s: str) -> bool:
    """Absolute on either platform, plus ~ which expanduser will make absolute."""
    e = os.path.expandvars(s)
    return _looks_windows_absolute(e) or e.startswith(("/", "~"))


def resolve(raw: str | os.PathLike[str]) -> Path:
    """Expand, absolutise and resolve a path without requiring it to exist.

    ``strict=False`` so a not-yet-created file still resolves — but note that the
    *parent* chain is still followed through symlinks, which is what stops a
    symlinked directory being used to escape the safe zone.
    """
    s = os.path.expandvars(str(raw))
    return Path(s).expanduser().resolve(strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or sits beneath it. Both must be resolved."""
    if WINDOWS:
        # Windows comparisons are case-insensitive; Path.is_relative_to is not.
        child = Path(str(child).casefold())
        parent = Path(str(parent).casefold())
    return child == parent or parent in child.parents


@dataclass
class PathGuard:
    """Classifies resolved paths into zones.

    Constructed from config at startup, but takes every root explicitly so tests
    can build one over a temp directory instead of the real machine.
    """

    safe_zone: Path
    index_roots: tuple[Path, ...] = ()
    extra_never_touch: tuple[Path, ...] = ()
    _never_touch: tuple[Path, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        self.safe_zone = resolve(self.safe_zone)
        self.index_roots = tuple(resolve(r) for r in self.index_roots)
        self._never_touch = tuple(resolve(r) for r in self._core_never_touch()) + tuple(
            resolve(r) for r in self.extra_never_touch
        )

    @staticmethod
    def _core_never_touch() -> list[str]:
        """The hardcoded set. Nothing removes from this list."""
        roots: list[str] = []
        if WINDOWS:
            roots.extend(_WINDOWS_SYSTEM_ROOTS)
        else:
            # Linux/macOS equivalents, so the build machine enforces the same shape.
            # Note: the user's home is deliberately NOT here — on this container
            # home is /root, and blacklisting it would make the default safe zone
            # itself BLACK. The home-relative entries below cover what matters.
            roots.extend(["/etc", "/boot", "/sys", "/proc", "/usr/bin", "/sbin"])
        home = Path.home()
        roots.extend(str(home / rel) for rel in _HOME_RELATIVE_BLACK)
        return roots

    # --- classification ---------------------------------------------------

    def is_black(self, p: Path) -> tuple[bool, str]:
        """Return (black, human reason). Checked before anything else, always."""
        # 1. Never-touch roots first. A path can be BLACK for several reasons at
        #    once, and the location is the more informative one to report: knowing
        #    "/etc/passwd is under the never-touch path /etc" tells you the zone
        #    boundary held, where "passwd is a credential file" only tells you a
        #    filename matched — which would look identical if the boundary had failed.
        for root in self._never_touch:
            if _is_within(p, root):
                return True, f"{p} is under the never-touch path {root}"
        # 2. Name patterns bite anywhere else, including inside the safe zone.
        for pat in _BLACK_NAME_PATTERNS:
            if pat.match(p.name):
                # JARVIS's own .env is the single carve-out, and only that exact file.
                if p.name.lower().startswith(".env") and _is_within(p, _jarvis_root()):
                    continue
                return True, f"{p.name} is a credential or secret file"
        # 3. Windows system roots, enforced even when running elsewhere, so a
        #    Windows-style argument arriving on any host is still refused.
        if is_black_windows_path(str(p)):
            return True, f"{p} is under a protected Windows system directory"
        return False, ""

    def zone(self, raw: str | os.PathLike[str]) -> tuple[Zone, Path, str]:
        """Classify a path. Returns (zone, resolved path, reason if BLACK)."""
        # Drive-letter paths are judged as Windows paths BEFORE resolution, on
        # every host. On POSIX a string like "C:\\Windows\\System32" is not
        # absolute at all — backslashes are ordinary characters — so resolve()
        # would quietly turn it into "<cwd>/C:\\Windows\\System32" and the system
        # root would stop matching. Judge the string we were actually given.
        raw_s = str(raw)
        if _looks_windows_absolute(raw_s):
            if is_black_windows_path(raw_s):
                return (Zone.BLACK, PureWindowsPath(raw_s),  # type: ignore[return-value]
                        f"{raw_s} is under a protected Windows system directory")
            if not WINDOWS:
                # A Windows path on a non-Windows host cannot be checked against
                # the real safe zone, so it is never treated as inside it.
                return Zone.OUTSIDE, PureWindowsPath(raw_s), ""  # type: ignore[return-value]

        # §6: "Creates land in the safe write zone unless I name a target." A bare
        # "notes.md" therefore anchors to the safe zone, NOT to the process working
        # directory. cwd is meaningless to someone talking to an agent — they have
        # no idea what it is, and resolving against it silently puts files in
        # whatever folder JARVIS happened to be started from.
        p = resolve(raw_s if _is_absolute(raw_s) else str(self.safe_zone / raw_s))
        black, why = self.is_black(p)
        if black:
            return Zone.BLACK, p, why
        if _is_within(p, self.safe_zone):
            return Zone.SAFE, p, ""
        for root in self.index_roots:
            if _is_within(p, root):
                return Zone.INDEXED, p, ""
        return Zone.OUTSIDE, p, ""


def _jarvis_root() -> Path:
    """The jarvis/ package root — the one place an .env is permitted."""
    return Path(__file__).resolve().parent.parent


def load_extra_never_touch(config_path: Path) -> tuple[Path, ...]:
    """Read config/never_touch.txt. Add-only: this can never shrink the core set."""
    if not config_path.exists():
        return ()
    out: list[Path] = []
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(resolve(line))
    return tuple(out)


def default_safe_zone() -> Path:
    """``%USERPROFILE%\\JarvisWorkspace`` — computed, never a guessed username."""
    return Path.home() / "JarvisWorkspace"
