"""The HUD server — stdlib only, localhost only.

    python jarvis/server.py     →  http://127.0.0.1:8765

The browser is a display, not an audio pipeline (§5). It receives state over SSE
at /api/events and never touches getUserMedia. When voice arrives in step 4 the
microphone lives in Python; nothing about this page changes.

Bound to 127.0.0.1 deliberately. This process can move files and, later, run
commands — it must not be reachable from the network.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import vault
from agent.actions import Origin, ProposedAction, Provenance, Risk
from agent.runtime import build as build_runtime

HERE = Path(__file__).resolve().parent
UI = HERE / "ui"
HOST, PORT = "127.0.0.1", 8765
BUILD = "fix3"        # printed at startup, so a stale process is obvious
USER = Provenance(Origin.USER)

MIME = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
        ".json": "application/json", ".svg": "image/svg+xml"}


class Hub:
    """Fan-out of server events to every connected HUD."""

    def __init__(self) -> None:
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.state = "idle"

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def emit(self, kind: str, payload: dict) -> None:
        msg = json.dumps({"kind": kind, "ts": time.time(), **payload})
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass        # a stalled tab must not block the agent

    def set_state(self, s: str) -> None:
        self.state = s
        self.emit("state", {"state": s})


HUB = Hub()
RT = None                       # the Runtime, built in main()
PENDING: dict[str, dict] = {}   # actions waiting on confirmation
PENDING_PLAN: list = []         # the rest of a multi-step plan, shown up front


def _expire_pending() -> None:
    """§3/§7: the confirmation timeout defaults to cancel, never to proceed."""
    from agent.policy import CONFIRM_TIMEOUT_S
    while True:
        time.sleep(1)
        now = time.monotonic()
        for aid, rec in list(PENDING.items()):
            left = CONFIRM_TIMEOUT_S - (now - rec["at"])
            if left <= 0:
                PENDING.pop(aid, None)
                RT.bus.execute(rec["action"], transcript=rec["transcript"],
                               confirmed_by=None)
                HUB.emit("cancelled", {"id": aid, "reason": "timed out — cancelled"})
                HUB.set_state("idle")
            else:
                HUB.emit("countdown", {"id": aid, "left": round(left, 1)})


def health() -> dict:
    from agent import registry
    import os
    g = RT.guard
    stats = RT.journal.stats()
    bs = RT.brain.status()
    brain = f"gemini:{bs['model']}" if bs["gemini_key"] else (
        f"ollama:{bs['ollama_model']}" if bs["ollama_model"] else "none")
    apps_ok, apps_why = _apps_status()
    v_ok, v_why = _voice_status()
    _v_label = "READY" if v_ok else v_why[:46]
    return {
        "brain": brain,
        "stt": _v_label, "tts": _v_label, "wake": _v_label,
        "dry_run": RT.policy.config.dry_run,
        "yolo": RT.policy.config.yolo,
        "safe_zone": str(g.safe_zone),
        "index_roots": [str(r) for r in g.index_roots],
        "surfaces": list(RT.attached),
        "apps_ok": apps_ok,
        "apps_why": apps_why,
        "chrome": "not built (step 5)",
        "verbs": len(registry.all_verbs()),
        "attached_verbs": sum(1 for s in registry.all_verbs().values() if s.attached),
        "journal": stats,
        "brain_calls": bs["calls"],
        "brain_tokens": bs["in_tokens"] + bs["out_tokens"],
        "brain_error": bs["last_error"],
        "notes": list(RT.notes),
        "state": HUB.state,
    }


def _apps_status() -> tuple[bool, str]:
    from agent.surfaces import apps
    return apps.available()


def _voice_status() -> tuple[bool, str]:
    from agent import voice
    return voice.available()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # keep the console for JARVIS, not for GETs
        pass

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # -- GET ---------------------------------------------------------------

    def do_GET(self) -> None:
        try:
            self._get()
        except Exception as e:                              # noqa: BLE001
            # Degrade loudly (§9.7). A traceback swallowed here shows up in the
            # HUD as a counter that never moved, which is indistinguishable from
            # an empty vault.
            import traceback
            msg = f"{type(e).__name__}: {e}"
            HUB.emit("error", {"error": f"{self.path} — {msg}"})
            try:
                self._json({"error": msg, "where": self.path,
                            "traceback": traceback.format_exc().splitlines()[-6:]}, 500)
            except Exception:
                pass

    def _get(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._file(UI / "index.html")
        if path.startswith("/ui/"):
            return self._file(UI / path[4:])
        if path == "/api/health":
            return self._json(health())
        if path == "/api/graph":
            return self._json(vault.build(RT.guard))
        if path == "/api/journal":
            return self._json({"entries": RT.journal.tail(40)})
        if path == "/api/events":
            return self._sse()
        self._send(404, b"no", "text/plain")

    def _file(self, p: Path) -> None:
        p = p.resolve()
        if not p.is_file() or UI.resolve() not in p.parents:
            return self._send(404, b"no", "text/plain")
        self._send(200, p.read_bytes(), MIME.get(p.suffix, "application/octet-stream"))

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = HUB.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=10)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")     # keep the pipe warm
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # A browser reload aborts the old SSE stream. On Linux that surfaces
            # as BrokenPipeError or ConnectionResetError; on Windows it is
            # ConnectionAbortedError (WinError 10053). Missing the Windows name
            # turned an ordinary page refresh into a server error banner.
            pass
        finally:
            HUB.unsubscribe(q)

    # -- POST --------------------------------------------------------------

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/command":
                return self._command(self._body())
            if path == "/api/confirm":
                return self._confirm(self._body())
            if path == "/api/undo":
                HUB.set_state("thinking")
                r = RT.bus.undo_last(confirmed_by="click")
                HUB.set_state("idle")
                return self._json({"ok": r.ok, "message": r.message,
                                   "outcome": r.outcome})
        except Exception as e:                              # noqa: BLE001
            HUB.emit("error", {"error": f"{type(e).__name__}: {e}"})
            HUB.set_state("idle")
            return self._json({"ok": False, "message": str(e)}, 500)
        self._send(404, b"no", "text/plain")

    def _command(self, body: dict) -> None:
        from run import COMMANDS       # the same fixed parser the text box uses
        line = str(body.get("text", "")).strip()
        HUB.emit("transcript", {"text": line})
        if not line:
            return self._json({"ok": False, "message": "nothing typed"})

        import shlex
        try:
            parts = shlex.split(line)
        except ValueError as e:
            return self._json({"ok": False, "message": f"couldn't parse that: {e}"})

        cmd, rest = parts[0].lower(), parts[1:]

        if cmd in COMMANDS:
            # An exact command is taken literally — no model round-trip, no
            # interpretation, no cost. Typing `list .` should just list.
            verb, argnames = COMMANDS[cmd]
            args: dict[str, str] = {}
            for i, name in enumerate(argnames):
                if i == len(argnames) - 1 and name in ("content", "query"):
                    args[name] = " ".join(rest[i:]) if len(rest) > i else ""
                elif i < len(rest):
                    args[name] = rest[i]
            HUB.set_state("thinking")
            proposed = ProposedAction(verb, args, {k: USER for k in args})
        else:
            # Anything else goes to the planner, which degrades loudly: Gemini,
            # then Ollama, then fixed shapes that announce they are not the model.
            HUB.set_state("thinking")
            plan = RT.brain.plan(line)
            HUB.emit("brain", {"source": plan.source, "model": plan.model,
                               "degraded": plan.degraded})
            if plan.say:
                HUB.emit("said", {"text": plan.say})
            if not plan.actions:
                HUB.set_state("idle")
                return self._json({"ok": bool(plan.say), "message": plan.say,
                                   "source": plan.source})
            if len(plan.actions) > 1:
                # §2: a multi-step request shows the whole plan before step one runs.
                actions = RT.bus.plan(plan.actions)
                PENDING_PLAN[:] = actions[1:]
                proposed = plan.actions[0]
            else:
                proposed = plan.actions[0]

        action = RT.bus.propose(proposed)
        HUB.emit("action", {"action": action.to_dict()})

        if action.risk is Risk.BLACK:
            RT.bus.execute(action, transcript=line)
            HUB.emit("blocked", {"id": action.id, "reason": action.refusal_reason})
            HUB.set_state("blocked")
            return self._json({"ok": False, "risk": "BLACK",
                               "message": action.refusal_reason})

        if action.risk is Risk.GREEN:
            r = RT.bus.execute(action, transcript=line)
            HUB.emit("done", {"id": action.id, "ok": r.ok, "result": r.value,
                              "message": r.message})
            HUB.set_state("idle")
            return self._json({"ok": r.ok, "risk": "GREEN", "result": r.value})

        # AMBER or RED — park it and let the overlay ask.
        code = action.id[:4].upper()
        PENDING[action.id] = {"action": action, "transcript": line,
                              "at": time.monotonic(), "code": code}
        HUB.emit("confirm", {"action": action.to_dict(), "code": code,
                             "needs": "code" if action.risk is Risk.RED else "yes"})
        HUB.set_state("awaiting")
        return self._json({"ok": True, "risk": action.risk.name, "pending": action.id,
                           "code": code if action.risk is Risk.RED else None})

    def _confirm(self, body: dict) -> None:
        aid = str(body.get("id", ""))
        rec = PENDING.pop(aid, None)
        if rec is None:
            return self._json({"ok": False, "message": "that action is no longer waiting"})

        action = rec["action"]
        if not body.get("approve"):
            RT.bus.execute(action, transcript=rec["transcript"], confirmed_by=None)
            HUB.emit("cancelled", {"id": aid, "reason": "you cancelled it"})
            HUB.set_state("idle")
            return self._json({"ok": False, "message": "cancelled"})

        # §3: RED needs the click-or-code path. The HUD button counts as a click,
        # but the code must match if one was issued — a mis-typed code cancels.
        how = "click"
        if action.risk is Risk.RED:
            typed = str(body.get("code", "")).strip().upper()
            if typed != rec["code"]:
                RT.bus.execute(action, transcript=rec["transcript"], confirmed_by=None)
                HUB.emit("cancelled", {"id": aid, "reason": "wrong code — cancelled"})
                HUB.set_state("idle")
                return self._json({"ok": False, "message": "wrong code"})
            how = "code"

        HUB.set_state("thinking")
        r = RT.bus.execute(action, transcript=rec["transcript"], confirmed_by=how)
        HUB.emit("done", {"id": aid, "ok": r.ok, "result": r.value, "message": r.message})
        HUB.set_state("idle")
        return self._json({"ok": r.ok, "message": r.message, "result": r.value})


def main() -> int:
    global RT
    RT = build_runtime(on_event=lambda kind, payload: HUB.emit(kind, payload))
    threading.Thread(target=_expire_pending, daemon=True).start()

    # Bind FIRST. Printing the banner before binding meant a second instance
    # printed a perfectly normal-looking startup and then died on "address
    # already in use" — while the FIRST, older process kept serving the browser.
    # Every restart looked successful and changed nothing.
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"\n  CANNOT START — port {PORT} is already in use ({e}).")
        print(f"  Another JARVIS is already running and IS the one your browser")
        print(f"  is talking to. It may be running older code.\n")
        print(f"  Windows:  taskkill /F /IM python.exe")
        print(f"  then start this again.\n")
        return 1

    print(f"\n  J A R V I S  ·  HUD  ·  build {BUILD}  ·  http://{HOST}:{PORT}\n")
    print(f"  safe write zone : {RT.guard.safe_zone}")
    print(f"  dry run         : {'ON' if RT.policy.config.dry_run else 'OFF'}")
    print(f"  vault           : {vault.build(RT.guard)['stats']}")
    for n in RT.notes:
        print(f"  ! {n}")
    print("\n  ctrl-c to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
