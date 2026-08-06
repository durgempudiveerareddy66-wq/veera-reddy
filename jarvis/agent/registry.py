"""The verb registry — the complete set of things JARVIS can be asked to do.

The planner picks from this table and nothing else. That is what makes "an unknown
verb is a refusal" enforceable rather than aspirational: there is no path from a
model output to an executor that does not pass through a registered
:class:`VerbSpec`.

``base_risk`` here is the *floor*, not the verdict. The policy engine may raise it
— a ``files.create`` aimed outside the safe zone becomes RED, a ``shell.run``
whose command is off the allowlist becomes BLACK — but nothing may lower it.

Executors are attached in build steps 2–6. Until a surface is wired up its verbs
are registered but unexecutable, and asking for one says so plainly rather than
failing somewhere deeper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .actions import Risk, Surface, UnknownVerb

PreviewFn = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class VerbSpec:
    verb: str
    surface: Surface
    base_risk: Risk
    reversible: bool
    required_args: tuple[str, ...]
    preview: PreviewFn
    summary: str
    path_args: tuple[str, ...] = ()      # args the PathGuard must classify
    writes: bool = False                 # does this mutate the filesystem?
    executor: Optional[Callable[..., Any]] = None

    @property
    def attached(self) -> bool:
        return self.executor is not None


_REGISTRY: dict[str, VerbSpec] = {}


def register(spec: VerbSpec) -> VerbSpec:
    if spec.verb in _REGISTRY:
        raise ValueError(f"duplicate verb {spec.verb!r}")
    _REGISTRY[spec.verb] = spec
    return spec


def get(verb: str) -> VerbSpec:
    """Look up a verb, or refuse. Never invents a spec for an unknown name."""
    try:
        return _REGISTRY[verb]
    except KeyError:
        raise UnknownVerb(verb) from None


def all_verbs() -> dict[str, VerbSpec]:
    return dict(_REGISTRY)


def attach(verb: str, executor: Callable[..., Any]) -> None:
    """Wire a surface's implementation to its verb. Called from surfaces/*.py."""
    spec = get(verb)
    _REGISTRY[verb] = VerbSpec(**{**spec.__dict__, "executor": executor})


def _a(args: dict[str, Any], key: str, default: str = "?") -> str:
    v = args.get(key, default)
    return str(v) if v not in (None, "") else default


# --- files -------------------------------------------------------------------

register(VerbSpec("files.find", Surface.FILES, Risk.GREEN, False, ("query",),
                  lambda a: f"Search the index for {_a(a,'query')!r} and list what matches.",
                  "search the read-only index"))
register(VerbSpec("files.list", Surface.FILES, Risk.GREEN, False, ("path",),
                  lambda a: f"List the contents of {_a(a,'path')}.",
                  "list a folder", path_args=("path",)))
register(VerbSpec("files.read", Surface.FILES, Risk.GREEN, False, ("path",),
                  lambda a: f"Read {_a(a,'path')} and report what it says.",
                  "read a file", path_args=("path",)))
register(VerbSpec("files.open", Surface.FILES, Risk.GREEN, False, ("path",),
                  lambda a: f"Open {_a(a,'path')} in its default application.",
                  "open a file in its default app", path_args=("path",)))
register(VerbSpec("files.create", Surface.FILES, Risk.AMBER, True, ("path",),
                  lambda a: f"Create a new file at {_a(a,'path')}"
                            f" ({len(str(a.get('content','')))} characters).",
                  "create a file", path_args=("path",), writes=True))
register(VerbSpec("files.write", Surface.FILES, Risk.AMBER, True, ("path", "content"),
                  lambda a: f"Overwrite {_a(a,'path')} with "
                            f"{len(str(a.get('content','')))} characters of new content.",
                  "overwrite a file", path_args=("path",), writes=True))
register(VerbSpec("files.move", Surface.FILES, Risk.AMBER, True, ("src", "dst"),
                  lambda a: f"Move {_a(a,'src')} to {_a(a,'dst')}.",
                  "move a file", path_args=("src", "dst"), writes=True))
register(VerbSpec("files.rename", Surface.FILES, Risk.AMBER, True, ("src", "dst"),
                  lambda a: f"Rename {_a(a,'src')} to {_a(a,'dst')}.",
                  "rename a file", path_args=("src", "dst"), writes=True))
register(VerbSpec("files.copy", Surface.FILES, Risk.AMBER, True, ("src", "dst"),
                  lambda a: f"Copy {_a(a,'src')} to {_a(a,'dst')}.",
                  "copy a file", path_args=("src", "dst"), writes=True))
register(VerbSpec("files.zip", Surface.FILES, Risk.AMBER, True, ("src", "dst"),
                  lambda a: f"Compress {_a(a,'src')} into the archive {_a(a,'dst')}.",
                  "zip a folder", path_args=("src", "dst"), writes=True))
# Deleting is RED per §3. It is nonetheless reversible, because the executor moves
# to a trash folder inside the safe zone rather than unlinking — an agent that can
# permanently destroy a file on one misread word is not one worth running.
register(VerbSpec("files.delete", Surface.FILES, Risk.RED, True, ("path",),
                  lambda a: f"Delete {_a(a,'path')} (moved to the JARVIS trash, recoverable).",
                  "delete a file", path_args=("path",), writes=True))

# --- apps --------------------------------------------------------------------

register(VerbSpec("apps.launch", Surface.APPS, Risk.GREEN, True, ("app",),
                  lambda a: f"Launch {_a(a,'app')}.", "launch an application"))
register(VerbSpec("apps.focus", Surface.APPS, Risk.GREEN, False, ("window",),
                  lambda a: f"Bring the window {_a(a,'window')!r} to the front.",
                  "focus a window"))
register(VerbSpec("apps.list_windows", Surface.APPS, Risk.GREEN, False, (),
                  lambda a: "List the open windows.", "list open windows"))
register(VerbSpec("apps.close", Surface.APPS, Risk.AMBER, False, ("window",),
                  lambda a: f"Close the window {_a(a,'window')!r}. Unsaved work in it may be lost.",
                  "close a window"))
register(VerbSpec("apps.click_control", Surface.APPS, Risk.AMBER, False, ("window", "control"),
                  lambda a: f"Click the control named {_a(a,'control')!r} "
                            f"in {_a(a,'window')!r}.",
                  "click a named UI control"))

# --- browser -----------------------------------------------------------------

register(VerbSpec("browser.navigate", Surface.BROWSER, Risk.GREEN, False, ("url",),
                  lambda a: f"Navigate the browser to {_a(a,'url')}.", "go to a URL"))
register(VerbSpec("browser.read_page", Surface.BROWSER, Risk.GREEN, False, (),
                  lambda a: "Read the text of the current page.", "read the page"))
register(VerbSpec("browser.find_element", Surface.BROWSER, Risk.GREEN, False, ("selector",),
                  lambda a: f"Look for {_a(a,'selector')!r} on the current page.",
                  "find an element"))
register(VerbSpec("browser.screenshot", Surface.BROWSER, Risk.GREEN, False, (),
                  lambda a: "Screenshot the current page.", "screenshot the page"))
register(VerbSpec("browser.wait", Surface.BROWSER, Risk.GREEN, False, ("selector",),
                  lambda a: f"Wait for {_a(a,'selector')!r} to appear.", "wait for an element"))
register(VerbSpec("browser.extract", Surface.BROWSER, Risk.GREEN, False, ("selector",),
                  lambda a: f"Extract {_a(a,'selector')!r} from the current page.",
                  "extract page data"))
register(VerbSpec("browser.click", Surface.BROWSER, Risk.AMBER, False, ("selector",),
                  lambda a: f"Click {_a(a,'selector')!r} on {_a(a,'url','the current page')}.",
                  "click something on a page"))
register(VerbSpec("browser.fill", Surface.BROWSER, Risk.AMBER, False, ("selector", "value"),
                  lambda a: f"Type {_a(a,'value')!r} into {_a(a,'selector')!r} "
                            f"on {_a(a,'url','the current page')}.",
                  "fill a form field"))
register(VerbSpec("browser.download", Surface.BROWSER, Risk.AMBER, True, ("url", "dst"),
                  lambda a: f"Download {_a(a,'url')} to {_a(a,'dst')}.",
                  "download a file", path_args=("dst",), writes=True))

# --- shell -------------------------------------------------------------------

register(VerbSpec("shell.run", Surface.SHELL, Risk.AMBER, False, ("command",),
                  lambda a: f"Run `{_a(a,'command')} "
                            f"{' '.join(str(x) for x in a.get('args', []))}`".rstrip()
                            + f" in {_a(a,'cwd','the safe write zone')}.",
                  "run an allowlisted command", path_args=("cwd",)))
register(VerbSpec("shell.git", Surface.SHELL, Risk.AMBER, False, ("subcommand",),
                  lambda a: f"Run `git {_a(a,'subcommand')} "
                            f"{' '.join(str(x) for x in a.get('args', []))}`".rstrip()
                            + f" in {_a(a,'cwd','the safe write zone')}.",
                  "run a git subcommand", path_args=("cwd",)))
register(VerbSpec("shell.scaffold", Surface.SHELL, Risk.AMBER, True, ("template", "dst"),
                  lambda a: f"Scaffold a {_a(a,'template')} project at {_a(a,'dst')}.",
                  "scaffold a project", path_args=("dst",), writes=True))
register(VerbSpec("shell.install", Surface.SHELL, Risk.RED, False, ("package",),
                  lambda a: f"Install the software package {_a(a,'package')!r} on this machine.",
                  "install software"))

# --- comms -------------------------------------------------------------------

register(VerbSpec("comms.read", Surface.COMMS, Risk.GREEN, False, (),
                  lambda a: f"Read messages matching {_a(a,'query','everything recent')}.",
                  "read messages"))
register(VerbSpec("comms.draft", Surface.COMMS, Risk.AMBER, True, ("to", "subject", "body"),
                  lambda a: f"Save a draft to {_a(a,'to')} subject {_a(a,'subject')!r} "
                            f"({len(str(a.get('body','')))} characters). Nothing is sent.",
                  "save a draft"))
# The one that can embarrass Reddie in front of a client. RED, always, no batching.
register(VerbSpec("comms.send", Surface.COMMS, Risk.RED, False, ("to", "subject", "body"),
                  lambda a: f"SEND a message to {_a(a,'to')} with subject "
                            f"{_a(a,'subject')!r}. This cannot be taken back.",
                  "send a message"))
register(VerbSpec("comms.invite", Surface.COMMS, Risk.RED, False, ("to", "when", "subject"),
                  lambda a: f"SEND a calendar invite to {_a(a,'to')} for {_a(a,'when')} "
                            f"titled {_a(a,'subject')!r}. This cannot be taken back.",
                  "send a calendar invite"))

# --- meta --------------------------------------------------------------------

register(VerbSpec("meta.undo", Surface.META, Risk.GREEN, False, (),
                  lambda a: "Undo the last reversible Action.",
                  "undo (the inverse Action is classified on its own merits)"))
register(VerbSpec("meta.brief", Surface.META, Risk.GREEN, False, (),
                  lambda a: "Summarise what's on today.", "brief"))
register(VerbSpec("meta.say", Surface.META, Risk.GREEN, False, ("text",),
                  lambda a: "Answer out loud without touching anything.", "just talk"))
