"""The apps surface — launching, focusing and clicking Windows applications.

§9.1 says `uiautomation` fails oddly on elevated windows and needs the right
permissions. So the rule here is: **never click into a window you cannot read.**
If the UI tree can't be walked, this surface reports why and stops. It does not
fall back to firing a click at a coordinate and hoping, because blind coordinate
clicking is how agents destroy things — the click lands wherever the window
happens to be, which may be a different application entirely.

`pyautogui` is therefore not imported. When coordinate clicking is eventually
added it must announce itself in the preview, per §6.

This module imports cleanly on any platform. The Windows dependencies are
resolved lazily so the rest of JARVIS can be built and tested off-Windows, and
so a missing dependency produces a sentence Reddie can act on rather than an
ImportError at startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from .. import registry

WINDOWS = sys.platform == "win32"

# Applications JARVIS may launch by name. Launching is GREEN, so this is the only
# thing standing between a mis-transcription and an arbitrary executable running.
KNOWN_APPS = {
    "notepad": "notepad.exe",
    "explorer": "explorer.exe",
    "calculator": "calc.exe",
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "code": "code.cmd",
    "vscode": "code.cmd",
    "terminal": "wt.exe",
    "powershell": "powershell.exe",
}


class SurfaceUnavailable(Exception):
    """The surface cannot run here, with a sentence explaining what to do."""


def _uia():
    """Import uiautomation lazily, with an actionable message if it isn't there."""
    if not WINDOWS:
        raise SurfaceUnavailable(
            "window control needs Windows UI Automation — this is "
            f"{sys.platform}, so focus, close and click are unavailable here"
        )
    try:
        import uiautomation  # type: ignore
    except ImportError:
        raise SurfaceUnavailable(
            "uiautomation is not installed. Run setup.ps1, or: "
            ".venv\\Scripts\\pip install uiautomation"
        ) from None
    return uiautomation


def launch(app: str, **_: Any) -> tuple[Any, dict]:
    """Start an application by known name."""
    key = str(app).strip().lower()
    exe = KNOWN_APPS.get(key)
    if exe is None:
        raise SurfaceUnavailable(
            f"{app!r} is not a known application. Known: "
            f"{', '.join(sorted(KNOWN_APPS))}. Add it to KNOWN_APPS in apps.py — "
            "JARVIS will not launch an arbitrary executable from a transcript."
        )
    if WINDOWS:
        os.startfile(exe)               # type: ignore[attr-defined]
    else:
        # Off-Windows this is the honest answer: the verb exists, the machine doesn't.
        raise SurfaceUnavailable(f"cannot launch {exe} on {sys.platform}")
    return {"launched": exe}, {}


def list_windows(**_: Any) -> tuple[Any, dict]:
    uia = _uia()
    out = []
    for w in uia.GetRootControl().GetChildren():
        try:
            if w.ControlTypeName == "WindowControl" and w.Name:
                out.append({"name": w.Name, "class": w.ClassName})
        except Exception:                       # noqa: BLE001
            continue        # a window that vanishes mid-walk is normal, not an error
    return {"windows": out, "count": len(out)}, {}


def _find_window(name: str):
    uia = _uia()
    target = name.strip().lower()
    for w in uia.GetRootControl().GetChildren():
        try:
            if w.Name and target in w.Name.lower():
                return w
        except Exception:                       # noqa: BLE001
            continue
    raise SurfaceUnavailable(
        f"no open window matching {name!r}. If the app is running elevated "
        "(as administrator), JARVIS cannot read it unless it is elevated too — "
        "and it should not be."
    )


def focus(window: str, **_: Any) -> tuple[Any, dict]:
    w = _find_window(window)
    w.SetActive()
    return {"focused": w.Name}, {}


def close(window: str, **_: Any) -> tuple[Any, dict]:
    w = _find_window(window)
    name = w.Name
    w.GetWindowPattern().Close()
    return {"closed": name}, {}


def click_control(window: str, control: str, **_: Any) -> tuple[Any, dict]:
    """Click a control *by name*, never by coordinate.

    If the control cannot be found in the window's tree, this fails. It does not
    guess a position — §9.1. A named control that isn't there means the UI is not
    in the state the plan assumed, and acting anyway is how the wrong button
    gets pressed.
    """
    w = _find_window(window)
    target = control.strip().lower()
    found = None
    for c in w.GetChildren():
        try:
            if c.Name and target in c.Name.lower():
                found = c
                break
        except Exception:                       # noqa: BLE001
            continue
    if found is None:
        raise SurfaceUnavailable(
            f"no control named {control!r} in {w.Name!r}. JARVIS will not click a "
            "coordinate instead — the window may have moved or changed state."
        )
    found.Click(simulateMove=False)
    return {"clicked": found.Name, "in": w.Name}, {}


def available() -> tuple[bool, str]:
    """Can this surface actually do anything here? For the HUD telemetry strip."""
    try:
        _uia()
        return True, "UI Automation ready"
    except SurfaceUnavailable as e:
        return False, str(e)


def attach_all() -> None:
    for verb, fn in {
        "apps.launch": launch, "apps.focus": focus, "apps.close": close,
        "apps.list_windows": list_windows, "apps.click_control": click_control,
    }.items():
        registry.attach(verb, fn)
