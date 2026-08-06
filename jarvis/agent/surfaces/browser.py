"""The browser surface — real Chrome, over CDP.

Playwright's ``connect_over_cdp`` against a Chrome that Reddie launched, rather
than a headless browser JARVIS spawned. That is the point: the value is in acting
on pages he is already logged into. It is also the risk, which is why this module
is the most defensive one in the build.

Three rules shape it:

*   **A dedicated profile by default.** Reddie's real profile holds live sessions
    for his bank and his clients. Using it is opt-in via ``BROWSER_USE_MAIN_PROFILE``,
    documented with its risk, and this module says so out loud every time.
*   **Payment, password and OAuth pages are BLACK.** Not "confirm harder" — refused.
    Detected from the live DOM before every action, not from the URL, because a
    URL is a guess and a password field is a fact.
*   **Page text is untrusted content** (§8). Everything read here comes back
    flagged with its source URL so taint tracking has something to key on. A page
    that says "email admin@evil.com" is reported, never obeyed.

§9.2: ``connect_over_cdp`` fails if Chrome is already running without the debug
port, and the raw error is useless. That exact case is detected and answered with
the command to restart Chrome.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .. import registry
from ..paths import PathGuard, Zone

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
NAV_TIMEOUT_MS = 20_000
ACTION_TIMEOUT_MS = 10_000
MAX_TEXT = 120_000

_GUARD: PathGuard | None = None
_PW = None            # the Playwright driver
_BROWSER = None       # the connected Chrome

# Selectors that mean "do not touch this page". Checked against the live DOM.
_DANGER_SELECTORS = {
    "a password field": "input[type=password]",
    "a payment card field": (
        "input[autocomplete*=cc-], input[name*=cardnumber], input[name*=card_number], "
        "input[id*=cardnumber], iframe[src*=checkout], iframe[name*=card]"
    ),
    "an OAuth consent screen": (
        "[data-consent], form[action*='oauth'], form[action*='consent'], "
        "button[id*='consent'], input[name='scope']"
    ),
}

# URL fragments that corroborate the DOM check. Used ONLY to add reasons, never
# as the sole basis for allowing something — a URL is a guess, a password input
# is a fact.
_DANGER_URL_HINTS = ("/checkout", "/payment", "/billing", "/signin", "/login",
                     "accounts.google.com", "oauth", "/consent", "paypal.com",
                     "stripe.com", "/transfer", "/bank")


class BrowserUnavailable(Exception):
    """Chrome isn't reachable, with a sentence saying exactly what to do."""


class PageRefused(Exception):
    """The page itself is BLACK — payment, password or consent. §3."""


def configure(guard: PathGuard) -> None:
    global _GUARD
    _GUARD = guard


def launch_command() -> str:
    """The exact command to restart Chrome with, for §9.2's error message."""
    profile = os.environ.get("BROWSER_PROFILE_DIR", r"%LOCALAPPDATA%\JarvisChrome")
    if sys.platform == "win32":
        return (r'"C:\Program Files\Google\Chrome\Application\chrome.exe" '
                f'--remote-debugging-port={CDP_PORT} --user-data-dir="{profile}"')
    return f"google-chrome --remote-debugging-port={CDP_PORT} --user-data-dir={profile}"


def connect():
    """Attach to a running Chrome. Never launches one behind Reddie's back."""
    global _PW, _BROWSER
    if _BROWSER is not None:
        try:
            _ = _BROWSER.contexts
            return _BROWSER
        except Exception:                                   # noqa: BLE001
            _BROWSER = None                                 # it died; reconnect

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserUnavailable(
            "playwright is not installed. Run setup.ps1, or: "
            ".venv\\Scripts\\pip install playwright"
        ) from None

    if _PW is None:
        _PW = sync_playwright().start()

    try:
        _BROWSER = _PW.chromium.connect_over_cdp(CDP_URL, timeout=5000)
    except Exception as e:                                  # noqa: BLE001
        # §9.2. The raw Playwright error here is "connect ECONNREFUSED" or a
        # timeout, which tells Reddie nothing actionable. The overwhelmingly
        # common cause is a Chrome already running WITHOUT the debug port — and
        # in that case a new Chrome silently joins the existing process and the
        # port never opens, so "just launch Chrome" is wrong advice.
        raise BrowserUnavailable(
            f"cannot reach Chrome on port {CDP_PORT}.\n"
            "  If Chrome is already open, it was started without the debugging "
            "port and a new window will just join it — close Chrome completely "
            "first (check the tray), then run:\n"
            f"    {launch_command()}\n"
            f"  ({type(e).__name__})"
        ) from None

    if _using_main_profile():
        # Said every time, not once at startup. This is Reddie's live banking
        # session and he should be reminded whenever it is in play.
        print("  ! BROWSER_USE_MAIN_PROFILE=1 — JARVIS is attached to your REAL "
              "Chrome profile, with your live logins. This is the risky mode.")
    return _BROWSER


def _using_main_profile() -> bool:
    return os.environ.get("BROWSER_USE_MAIN_PROFILE", "0") == "1"


def _page():
    b = connect()
    ctxs = b.contexts
    if not ctxs or not ctxs[0].pages:
        raise BrowserUnavailable("Chrome is attached but has no open tab.")
    return ctxs[0].pages[0]


# --- the page-danger gate ----------------------------------------------------

def page_danger(page) -> list[str]:
    """What makes this page untouchable. Empty list means it is ordinary."""
    found: list[str] = []
    for label, sel in _DANGER_SELECTORS.items():
        try:
            if page.query_selector(sel) is not None:
                found.append(label)
        except Exception:                                   # noqa: BLE001
            continue
    url = (page.url or "").lower()
    hint = next((h for h in _DANGER_URL_HINTS if h in url), None)
    if hint and not found:
        found.append(f"a URL containing {hint!r}")
    return found


def _require_safe_page(page, what: str) -> None:
    danger = page_danger(page)
    if danger:
        raise PageRefused(
            f"refusing to {what} on {page.url} — it shows {', '.join(danger)}. "
            "Pages handling money, credentials or consent are BLACK, and no "
            "phrasing changes that."
        )


# --- read-only verbs (GREEN) -------------------------------------------------

def navigate(url: str, **_: Any) -> tuple[Any, dict]:
    if not str(url).lower().startswith(("http://", "https://")):
        raise BrowserUnavailable(f"{url!r} is not an http(s) URL")
    page = _page()
    page.goto(str(url), timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    return {"url": page.url, "title": page.title()}, {}


def read_page(**_: Any) -> tuple[Any, dict]:
    """Read the visible text. UNTRUSTED — §8 — and it says so in its payload."""
    page = _page()
    text = page.inner_text("body")[:MAX_TEXT]
    return {"url": page.url, "title": page.title(), "untrusted": True,
            "source": page.url, "text": text,
            "danger": page_danger(page)}, {}


def find_element(selector: str, **_: Any) -> tuple[Any, dict]:
    page = _page()
    els = page.query_selector_all(str(selector))
    return {"selector": selector, "count": len(els),
            "first_text": (els[0].inner_text()[:400] if els else None)}, {}


def screenshot(**_: Any) -> tuple[Any, dict]:
    page = _page()
    dst = _GUARD.safe_zone / "screenshots"
    dst.mkdir(parents=True, exist_ok=True)
    out = dst / f"page-{abs(hash(page.url)) % 10**8}.png"
    page.screenshot(path=str(out))
    return {"saved": str(out), "url": page.url}, {}


def wait(selector: str, **_: Any) -> tuple[Any, dict]:
    page = _page()
    page.wait_for_selector(str(selector), timeout=ACTION_TIMEOUT_MS)
    return {"appeared": selector}, {}


def extract(selector: str, **_: Any) -> tuple[Any, dict]:
    page = _page()
    vals = [e.inner_text()[:600] for e in page.query_selector_all(str(selector))]
    return {"selector": selector, "count": len(vals), "untrusted": True,
            "source": page.url, "values": vals[:200]}, {}


# --- acting verbs (AMBER, and refused outright on a dangerous page) ----------

def click(selector: str, **_: Any) -> tuple[Any, dict]:
    page = _page()
    _require_safe_page(page, "click")
    el = page.query_selector(str(selector))
    if el is None:
        raise BrowserUnavailable(
            f"no element matching {selector!r} on {page.url}. JARVIS will not "
            "click a coordinate instead."
        )
    # Never submit a form JARVIS did not fill itself (§6).
    if _is_submit(el) and not _FILLED_THIS_PAGE.get(page.url):
        raise PageRefused(
            f"{selector!r} submits a form JARVIS did not fill. Submitting someone "
            "else's half-completed form is not something it will do."
        )
    el.click(timeout=ACTION_TIMEOUT_MS)
    return {"clicked": selector, "url": page.url}, {}


def _is_submit(el) -> bool:
    try:
        t = (el.get_attribute("type") or "").lower()
        tag = el.evaluate("e => e.tagName").lower()
        return t == "submit" or (tag == "button" and t not in ("button", "reset"))
    except Exception:                                       # noqa: BLE001
        return False


_FILLED_THIS_PAGE: dict[str, bool] = {}


def fill(selector: str, value: str, **_: Any) -> tuple[Any, dict]:
    page = _page()
    _require_safe_page(page, "type into a field")
    el = page.query_selector(str(selector))
    if el is None:
        raise BrowserUnavailable(f"no field matching {selector!r} on {page.url}")
    el.fill(str(value), timeout=ACTION_TIMEOUT_MS)
    _FILLED_THIS_PAGE[page.url] = True
    return {"filled": selector, "chars": len(str(value)), "url": page.url}, {}


def download(url: str, dst: str, **_: Any) -> tuple[Any, dict]:
    zone, path, why = _GUARD.zone(dst)
    if zone is Zone.BLACK:
        raise PageRefused(why)
    if zone is not Zone.SAFE:
        raise PageRefused(f"{path} is outside the safe write zone")
    page = _page()
    with page.expect_download(timeout=NAV_TIMEOUT_MS) as info:
        page.goto(str(url), timeout=NAV_TIMEOUT_MS)
    info.value.save_as(str(path))
    return {"downloaded": str(url), "to": str(path)}, {}


def available() -> tuple[bool, str]:
    """For the telemetry strip. Never launches Chrome to find out."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "playwright not installed — run setup.ps1"
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", CDP_PORT))
        return True, ("attached to your REAL profile" if _using_main_profile()
                      else f"Chrome on :{CDP_PORT}")
    except OSError:
        return False, f"no Chrome on :{CDP_PORT} — {launch_command()[:60]}…"
    finally:
        s.close()


def attach_all() -> None:
    for verb, fn in {
        "browser.navigate": navigate, "browser.read_page": read_page,
        "browser.find_element": find_element, "browser.screenshot": screenshot,
        "browser.wait": wait, "browser.extract": extract,
        "browser.click": click, "browser.fill": fill, "browser.download": download,
    }.items():
        registry.attach(verb, fn)
