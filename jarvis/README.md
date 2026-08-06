# JARVIS v3

A voice-controlled agent that takes actions on a Windows machine.

**Build status: step 4 of 7 complete.** The spine — Action bus, policy engine,
journal, undo — is built and proved. No surfaces are attached, so JARVIS currently
cannot touch anything. That is deliberate: §10 of the build spec puts the safety
machinery first and gives it hands afterwards.

| Step | What | State |
|------|------|-------|
| 0 | Interview, `CLAUDE.md` | done — 1 of 14 answered, rest recorded `UNKNOWN` |
| 1 | Action bus, policy engine, journal | **done, 59 tests passing** |
| 2 | `files.py`, `apps.py` | **done, 16 more tests passing** |
| 3 | The HUD — reactor, action stack, telemetry, graph | **done** |
| 4 | Voice — wake word, Scribe, TTS, barge-in | **code done, hardware UNVERIFIED** |
| 5 | `browser.py` | not started |
| 6 | `shell.py`, `comms.py` | not started |
| 7 | Memory, guardrail suite, this README in full | not started |

---

## Try it now

Nothing to install — step 1 is stdlib Python only.

```bash
python jarvis/server.py              # the HUD  →  http://127.0.0.1:8765
python jarvis/prove.py               # watch it refuse things
python jarvis/run.py                 # the text box — type at the file surface
python jarvis/tests/test_spine.py    # 59 tests, the spine
python jarvis/tests/test_surfaces.py # 16 tests, real files on disk
python jarvis/tests/test_brain.py    # 16 tests, incl. a compromised planner
python jarvis/tests/test_voice.py    # 19 tests, synthetic audio
```

`prove.py` builds a throwaway machine under `/tmp`, proposes eighteen Actions
ranging from harmless to hostile, and prints what the policy engine did with each.
Nothing it does touches a real folder.

## The architecture, in one paragraph

The model never touches the OS. It proposes **Actions** naming verbs from a fixed
registry (`agent/registry.py`). A **policy engine** (`agent/policy.py`) classifies
each one GREEN / AMBER / RED / BLACK from the Action's *shape* — never from the
model's opinion, which is why `ProposedAction` has no risk field for it to fill in.
An **executor** runs what survives, a **journal** (`logs/actions.jsonl`) records
everything including what was refused, and every reversible Action carries its own
inverse so `undo` is real rather than best-effort.

```
voice → transcript → PLANNER → [Action, Action, …]
                                    ↓
                            POLICY ENGINE ── risk tier
                                    ↓
              GREEN: run   AMBER: ask   RED: ask hard   BLACK: refuse
                                    ↓
                                EXECUTOR → result + undo record
                                    ↓
                          spoken line + HUD card + journal line
```

## The four tiers

| Tier | Means | Released by |
|------|-------|-------------|
| **GREEN** | reading, searching, listing, opening, navigating | nothing — runs, then announces |
| **AMBER** | writing inside the safe zone, filling a form, an allowlisted command | one word — voice is enough |
| **RED** | sending, deleting, writing outside the safe zone, `git push`, installing | a **click or a typed code**. Voice is never enough. Rate-limited to one per 10s, never batched |
| **BLACK** | system paths, credential stores, off-allowlist commands, unknown recipients | **nothing.** No phrasing, no override, not even `JARVIS_YOLO` |

The `JARVIS_YOLO=1` override auto-confirms AMBER and RED. It is a bad idea, it
prints a banner across the HUD the whole time it is on, and it has never applied
to BLACK.

## What is proved, and what is not

Proved on this build machine (Linux), by `prove.py` and 59 unit tests:

- writing outside the safe write zone → RED; reading outside it stays GREEN
- `C:\Windows`, `Program Files`, `ProgramData` → BLACK, on any host
- `.ssh`, `.aws`, `id_rsa`, `*.pem`, `*.kdbx`, `.env`, Chromium `Login Data` → BLACK
- `..` traversal and symlinks out of the safe zone → caught **after** resolution
- off-allowlist commands (`curl`, `rm`, `powershell`, `certutil`) → BLACK
- `git push` → RED · shell metacharacters → BLACK · `--no-verify` → RED
- sending to an address in no file or contact list → BLACK
- a file or web page introducing a recipient, a command, or a write path → BLACK
- other content-derived arguments → escalated one tier, floor AMBER, source named
- an unknown verb → refusal, never improvisation
- RED not released by voice · RED rate-limited · confirmation timeout cancels
- undo record is the true inverse, tiered on its own merits, offered only once

**Not yet proved, and it needs your machine:** everything Windows-specific —
`uiautomation`, Chrome CDP, the microphone, real `C:\` paths, elevated windows.
This container is Linux; those tests are honest only when run on Windows.

## Keys

Nothing is hardcoded. Copy `.env.example` to `.env` and fill it in — `.env` is
gitignored and `setup.ps1` will restrict it to your user.

| Key | Used for | Status |
|-----|----------|--------|
| `GEMINI_API_KEY` | planning, tool selection | verified working — HTTP 200, 42 models |
| `ELEVENLABS_API_KEY` | speech in (Scribe) and out (TTS) | **unverified** — the build container's network policy blocks `api.elevenlabs.io`. `setup.ps1` checks it on Windows |

Not Grok — the spec named xAI, a Gemini key was supplied instead, so the brain is
Gemini's native API. **No Anthropic, no OpenAI**: neither SDK is imported anywhere
and neither key is read.

> If these keys were ever pasted into a chat window, a ticket, or a shared doc,
> rotate them. Assume anything that left your machine is public.

## Layout

```
jarvis/
  CLAUDE.md            who Reddie is — STATED vs UNKNOWN vs DEFAULT, never blurred
  prove.py             the readable guardrail demo
  agent/
    actions.py         Action, ProposedAction, Risk, Provenance
    registry.py        every verb JARVIS has, and its floor risk
    policy.py          the tiering engine — the part not to weaken
    paths.py           zone enforcement; resolve before you judge
    journal.py         append-only actions.jsonl
    undo.py            inverse Actions
    bus.py             propose → gate → execute → record
    surfaces/          empty until step 2
  config/
    allowed_commands.txt   allowlist, not denylist
    never_touch.txt        add-only; cannot shrink the hardcoded BLACK set
  tests/test_spine.py  59 tests
  data/demo_vault/     fixtures — NOT Reddie's real business
```

## Open questions

`CLAUDE.md` records 13 of 14 interview answers as `UNKNOWN`. Until they are
answered JARVIS will say "I don't know that about you yet" rather than guess, and
the three paths it runs on are **defaults it chose**, flagged as such every time
they drive an outcome.
