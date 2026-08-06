# JARVIS v3

A voice-controlled agent that takes actions on a Windows machine — moves files,
drives Chrome, runs commands, drafts messages — behind a policy engine that
decides, in code, what it is allowed to do.

**Build status: all 7 steps complete.** 12/12 guardrails proved. Verified on
Windows 11 / Python 3.13 for everything except the audio hardware path.

| Step | What | State |
|------|------|-------|
| 0 | Interview, `CLAUDE.md` | 1 of 14 answered — the rest recorded `UNKNOWN`, never guessed |
| 1 | Action bus, policy engine, journal, undo | done · 62 tests |
| 2 | `files.py`, `apps.py` | done · 16 tests · files verified on Windows |
| 3 | The HUD — reactor, action stack, telemetry, vault graph | done · verified on Windows |
| 4 | Gemini planner · voice pipeline | brain verified live · **audio unverified** |
| 5 | `browser.py` | done · 15 tests · verified against real Chrome over CDP |
| 6 | `shell.py`, `comms.py` | done · the two that can do damage, deliberately last |
| 7 | Memory, `CLAUDE.md` loading, §11 guardrail suite | done · **12/12** |

---

## Setup

```powershell
git clone -b claude/jarvis-v3-agent-build-skktgo https://github.com/durgempudiveerareddy66-wq/veera-reddy.git
cd veera-reddy\jarvis
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` creates `.venv`, installs the packages below one at a time with
progress, copies `.env.example` to `.env`, locks `.env` to your user account,
checks both API keys against their live APIs, and lists your microphones.
It touches nothing outside the `jarvis` folder.

Then put your keys in `.env` and:

```powershell
python server.py        # the HUD          → http://127.0.0.1:8765
python run.py           # the text box
python prove.py         # watch it refuse things
python guardrails.py    # the §11 suite — 12 checks, real attempts
python diagnose.py      # why is the index empty?
```

**Nothing in steps 1–3 needs any package at all.** The HUD, the policy engine,
the file surface and the vault graph are pure standard library. If `setup.ps1`
fails you can still run `server.py`.

## Every dependency, and why it is there

| Package | Why | Without it |
|---|---|---|
| `numpy` | audio maths — RMS, PCM conversion | no voice |
| `sounddevice` | the microphone and the speaker, 16 kHz mono | no voice |
| `openwakeword` | "Hey Jarvis", running **locally**, always on | no wake word; push-to-talk still fine |
| `uiautomation` | Windows UI Automation — focus windows, read window trees, click controls **by name** | `apps` surface inert, reported in the strip |
| `pywin32` | window handles, the global barge-in hotkey | no hotkey |
| `pyautogui` | last-resort coordinate clicking **only**, and it announces itself when used | nothing; it is deliberately barely used |
| `playwright` | real Chrome over CDP | `browser` surface inert |
| `requests` | HTTP where the stdlib gets awkward | little — most calls use `urllib` |

No Anthropic. No OpenAI. Neither SDK is imported and neither key is read.
`openwakeword` lags behind new Python releases; on 3.13 it may not install, and
everything else works without it.

## The switches

| Variable | Default | What it does |
|---|---|---|
| `JARVIS_DRYRUN` | **`1`** | Every executor prints what it *would* do and returns a fake success. Leave it on until you have watched JARVIS plan a few things correctly. Turn it off deliberately. |
| `JARVIS_YOLO` | `0` | Auto-confirms AMBER and RED without asking. **This is a bad idea.** A red banner spans the HUD the whole time it is set. It has never applied to BLACK and never will. |
| `JARVIS_SAFE_ZONE` | `%USERPROFILE%\JarvisWorkspace` | The one folder JARVIS may write to. Everything else is RED or BLACK. |
| `JARVIS_ROOTS` | safe zone + demo vault | Folders indexed **read-only**. Semicolon-separated. Your own folders are *not* indexed until you say so. |
| `BROWSER_USE_MAIN_PROFILE` | `0` | Attach to your real Chrome profile instead of a dedicated one. Your live bank and client sessions are in there. Announced on every connect. |

## Where each key goes

Both live in `jarvis/.env`, which is gitignored and locked to your user by
`setup.ps1`. **Neither ever reaches the browser** — nothing sensitive appears in
devtools or a screen recording.

| Key | Used for | Get one at |
|---|---|---|
| `GEMINI_API_KEY` | planning, tool selection | [aistudio.google.com](https://aistudio.google.com) |
| `ELEVENLABS_API_KEY` | speech in (Scribe) and out (TTS) | [elevenlabs.io](https://elevenlabs.io) → Profile |

> If a key has ever been pasted into a chat window, a ticket or a shared
> document, rotate it. Assume anything that left your machine is public.

## What a day costs

Measured, not estimated — three real planning turns against `gemini-3.6-flash`:

```
3 calls · 11,827 input tokens · 49 output tokens · ~3,958 tokens per turn
```

Input dominates because every turn carries the tool declarations for all 36
verbs, plus `CLAUDE.md`, plus memory. Output is tiny — a function call is a few
dozen tokens.

- **40 turns a day ≈ 160,000 tokens**, almost all input.
- ElevenLabs bills characters, not tokens. A spoken reply is 100–300 characters,
  so **40 replies ≈ 4,000–12,000 characters**. Scribe bills per minute of audio;
  40 short utterances is roughly 5–10 minutes.

Check current rates at [ai.google.dev/pricing](https://ai.google.dev/pricing) and
[elevenlabs.io/pricing](https://elevenlabs.io/pricing) — they change, and quoting
a number here that goes stale would be worse than none. The HUD's top strip shows
live call and token counts so you can watch the real figure rather than trust this
paragraph.

## Exactly what leaves your machine, and when

**While idle: nothing.** The microphone feeds a local ring buffer that
`openwakeword` reads on-device. Audio older than ~1.2 seconds is overwritten and
gone. There is no network call until you speak the wake word.

| Moment | What leaves | Where to |
|---|---|---|
| You say "Hey Jarvis" | nothing yet — the wake word is detected locally | — |
| You finish your sentence | the buffered utterance, as WAV | ElevenLabs (Scribe) |
| JARVIS plans | your transcript, `CLAUDE.md`, memory, tool declarations, the last ~10 turns | Google (Gemini) |
| JARVIS speaks | the reply text | ElevenLabs (TTS) |
| JARVIS reads a file or page | **nothing** — until that content is included in a plan, at which point it goes to Google wrapped in `<untrusted_data>` |
| Everything else | nothing | — |

Never sent anywhere: your `.env`, credential files, browser password databases,
SSH keys, or anything under a never-touch path. Those are BLACK — JARVIS cannot
read them, so it cannot transmit them.

The HUD is `127.0.0.1` only. It is not reachable from your network.

## Revoking JARVIS's access, in a hurry

**Right now, this second:**

```powershell
taskkill /F /IM python.exe
```

That kills the server and every surface with it. JARVIS cannot act when it is not
running. Everything below is for making sure it *stays* unable to act.

**1. Revoke the keys.** This is the one that matters, because a key that has
leaked is dangerous whether or not JARVIS is running.

- Gemini: [aistudio.google.com](https://aistudio.google.com) → API keys → delete
- ElevenLabs: [elevenlabs.io](https://elevenlabs.io) → Profile → API key → regenerate

**2. Take away its hands.** Rename the safe write zone:

```powershell
Rename-Item "$env:USERPROFILE\JarvisWorkspace" "JarvisWorkspace.OFF"
```

Every write now targets a path that does not exist, and every write outside it
was already RED.

**3. Cut the browser off.** Close Chrome completely, then reopen it normally —
without `--remote-debugging-port`. JARVIS attaches over CDP and nothing else; no
debug port means no browser access.

**4. Check what it did.** `logs/actions.jsonl` is append-only and records every
action including the refused ones, with the transcript that caused each:

```powershell
Get-Content logs\actions.jsonl -Tail 40
```

**5. Delete what it remembers.** `memory/facts.json` inside the safe zone. Delete
the file; nothing else holds state.

You do not need to uninstall anything. JARVIS has no service, no scheduled task,
no autostart and no background process. It runs when you run it.

## The four tiers

| Tier | Covers | Released by |
|---|---|---|
| **GREEN** | reading, searching, listing, opening, navigating | nothing — runs, then announces |
| **AMBER** | writing inside the safe zone, filling a form, an allowlisted command, saving a draft | one word — voice is enough |
| **RED** | sending, deleting, writing outside the safe zone, `git push`, installing | a **click or a typed code**. Voice is never enough. One per 10 s, never batched |
| **BLACK** | system paths, credential stores, off-allowlist commands, unknown recipients, payment and password pages | **nothing.** No phrasing, no override, not even `JARVIS_YOLO` |

The tier is decided by `policy.py` from the Action's *shape*. The model gets no
say — `ProposedAction` has no risk field for it to fill in. That is structural,
not a convention.

## What is proved, and what is not

`python guardrails.py` runs all twelve §11 checks and exits non-zero if any
misbehave. Current result: **12/12**.

Writing outside the safe zone · delete not released by voice · `git push` and
off-allowlist commands refused · an injected file reported as content and never
obeyed · a web page's instruction escalated with its source named · a send to an
unknown address BLACK · `C:\Windows\System32` and `.ssh` BLACK and unreleasable ·
confirmation timeout cancelling · Gemini failing over to Ollama · both brains gone
leaving an honest refusal · a missing mic failing loudly · a file move undone and
the file returning.

**Not proved, and it needs your hardware:** that `sounddevice` opens the right
device, that `openwakeword` fires on your voice, that Scribe returns a transcript,
that the global hotkey registers, and — specifically — that a microphone
**unplugged mid-turn** fails loudly. Check 11 proves only the no-device path, and
says so in its own output rather than quietly counting itself a win.

## Layout

```
jarvis/
  CLAUDE.md              who Reddie is — STATED vs UNKNOWN vs DEFAULT, never blurred
  server.py              the HUD server, 127.0.0.1 only
  run.py                 the text box
  prove.py               readable tiering demo
  guardrails.py          the §11 suite
  diagnose.py            why is the index empty?
  setup.ps1              venv, packages, key checks, mic list
  agent/
    actions.py  registry.py  policy.py  paths.py
    journal.py  undo.py  bus.py  runtime.py
    brain.py    Gemini → Ollama → fixed shapes → honest refusal
    memory.py   facts, with provenance; never written silently
    voice.py    mic in Python, never the browser
    vault.py    the graph
    surfaces/   files · apps · browser · shell · comms
  config/
    allowed_commands.txt   allowlist, not denylist
    never_touch.txt        add-only; cannot shrink the hardcoded BLACK set
  tests/                 126 tests, stdlib unittest only
  data/demo_vault/       fixtures — NOT a real business
```

## Open questions

`CLAUDE.md` still records 13 of 14 interview answers as `UNKNOWN`. Until they are
answered JARVIS says "I don't know that about you yet" rather than guessing, and
the three paths it runs on are **defaults it chose**, flagged as such every time
they drive an outcome. The demo vault's clients and invoices are fiction and
JARVIS will say so if asked.
