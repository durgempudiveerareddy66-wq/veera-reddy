"""Voice — microphone in Python, never in the browser.

§5's architectural call, and the reason for it: the page is a HUD, not an audio
pipeline. Keeping capture server-side kills the webm/opus decoding problem, makes
always-on wake-word detection trivial, and means JARVIS still hears Reddie when
the window isn't focused. The ElevenLabs key never reaches the browser — it stays
in .env, and nothing sensitive shows up in devtools or a screen recording.

**Nothing streams to ElevenLabs while Reddie is idle.** Audio sits in a local ring
buffer and openWakeWord runs on-device against it. Only after "Hey Jarvis" fires
does anything leave the machine, and only the buffered utterance goes.

The hardware layer is injectable. Everything that decides *when* to listen, *when*
a turn has ended, and *when* to go deaf is a pure state machine tested against
synthetic frames — because a build machine has no microphone, and "it compiles"
is not evidence a voice loop works.
"""

from __future__ import annotations

import collections
import json
import math
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# --- tunables. §5 says both of these live at the top of voice.py so Reddie can
# --- tune them without reading the rest of the file.

SAMPLE_RATE = 16_000            # ElevenLabs Scribe is happy with 16 kHz mono
FRAME_MS = 32                   # samples per callback chunk
SILENCE_RMS = 0.012             # below this counts as silence
SILENCE_HOLD_MS = 900           # this much silence ends the turn
MAX_UTTERANCE_S = 30            # a stuck-open mic must not stream forever
PREROLL_MS = 1_200              # audio kept from *before* the wake word fired
WAKE_THRESHOLD = 0.60           # §9.3 — below this a trigger is ignored
WAKE_COOLDOWN_S = 1.5           # stops one utterance firing the wake word twice

ELEVEN_BASE = "https://api.elevenlabs.io/v1"
STT_MODEL = "scribe_v1"
TTS_MODEL = "eleven_flash_v2_5"

FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


class VoiceUnavailable(Exception):
    """Voice cannot run here, with a sentence saying what to do about it."""


# --- pure logic, testable without hardware -----------------------------------

def rms(frame: Iterable[float]) -> float:
    """Root mean square of one frame, 0.0–1.0. Drives both silence and the HUD."""
    vals = list(frame)
    if not vals:
        return 0.0
    return math.sqrt(sum(v * v for v in vals) / len(vals))


class RingBuffer:
    """The last N ms of audio, held locally and overwritten.

    This is the privacy boundary as much as a technical one: while JARVIS is
    idle, everything the microphone hears lands here and is discarded. It is the
    reason the README can say nothing leaves the machine until the wake word.
    """

    def __init__(self, ms: int = PREROLL_MS):
        self.max_frames = max(1, ms // FRAME_MS)
        self.frames: collections.deque = collections.deque(maxlen=self.max_frames)

    def push(self, frame) -> None:
        self.frames.append(frame)

    def drain(self) -> list:
        out = list(self.frames)
        self.frames.clear()
        return out

    def __len__(self) -> int:
        return len(self.frames)


@dataclass
class TurnDetector:
    """Decides when Reddie has stopped talking.

    RMS-based, with a hold: a single quiet frame is a pause between words, not
    the end of a sentence. Only SILENCE_HOLD_MS of continuous quiet ends the turn.
    """

    silence_rms: float = SILENCE_RMS
    hold_ms: int = SILENCE_HOLD_MS
    max_s: int = MAX_UTTERANCE_S

    _quiet_ms: int = field(default=0, init=False)
    _total_ms: int = field(default=0, init=False)
    _heard_speech: bool = field(default=False, init=False)

    def reset(self) -> None:
        self._quiet_ms = 0
        self._total_ms = 0
        self._heard_speech = False

    def feed(self, level: float) -> str:
        """Return 'listening', 'ended' or 'timeout' for one frame."""
        self._total_ms += FRAME_MS
        if level >= self.silence_rms:
            self._heard_speech = True
            self._quiet_ms = 0
        else:
            self._quiet_ms += FRAME_MS

        if self._total_ms >= self.max_s * 1000:
            return "timeout"
        # Silence before any speech is just Reddie not having started yet.
        if self._heard_speech and self._quiet_ms >= self.hold_ms:
            return "ended"
        return "listening"


class WakeGate:
    """Confidence threshold and cooldown around the wake word (§9.3).

    Every trigger is reported to the HUD, including the ones rejected, so a
    false-fire from a podcast is visible rather than mysterious. A wake word can
    only ever *start a turn* — it can never confirm anything, which is what makes
    false-firing into a RED action structurally impossible rather than unlikely.
    """

    def __init__(self, threshold: float = WAKE_THRESHOLD,
                 cooldown_s: float = WAKE_COOLDOWN_S,
                 on_trigger: Callable[[dict], None] | None = None):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.on_trigger = on_trigger or (lambda d: None)
        self._last = 0.0

    def offer(self, score: float, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if score < self.threshold:
            self.on_trigger({"score": score, "accepted": False, "why": "below threshold"})
            return False
        if now - self._last < self.cooldown_s:
            self.on_trigger({"score": score, "accepted": False, "why": "cooldown"})
            return False
        self._last = now
        self.on_trigger({"score": score, "accepted": True, "why": ""})
        return True


# --- hardware ----------------------------------------------------------------

def list_devices() -> list[dict[str, Any]]:
    """Enumerate inputs. §9.4 — indices move when headphones are plugged in."""
    try:
        import sounddevice as sd
    except ImportError:
        raise VoiceUnavailable(
            "sounddevice is not installed. Run setup.ps1, or: "
            ".venv\\Scripts\\pip install sounddevice numpy"
        ) from None
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append({"index": i, "name": d["name"],
                        "rate": int(d.get("default_samplerate", 0))})
    return out


def resolve_device(preferred_name: str = "") -> int | None:
    """Find the chosen device *by name*, not by index.

    Indices are positional and shift the moment a USB headset appears. Persisting
    the name and re-resolving at startup is what stops JARVIS going silently deaf
    — and going deaf silently is the most confusing failure in this whole build.
    """
    devices = list_devices()
    if not devices:
        raise VoiceUnavailable("no input devices found — is a microphone connected?")
    if preferred_name:
        for d in devices:
            if d["name"].strip().lower() == preferred_name.strip().lower():
                return d["index"]
    return None                 # caller falls back to the system default


class Speaker:
    """ElevenLabs TTS, streamed and played locally.

    While this is speaking the microphone is deaf — otherwise JARVIS transcribes
    itself through the speakers and loops forever.
    """

    def __init__(self, api_key: str = "", voice_id: str = "",
                 on_state: Callable[[str], None] | None = None):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "")
        self.on_state = on_state or (lambda s: None)
        self.speaking = threading.Event()
        self._stop = threading.Event()

    def barge_in(self) -> None:
        """Stop talking immediately. Bound to a global hotkey and the HUD control."""
        self._stop.set()

    def resolve_voice(self) -> str:
        """Pick a voice if none was configured.

        Without this, an empty ELEVENLABS_VOICE_ID means JARVIS is simply silent
        — it plans, it acts, and it never speaks, with nothing saying why. One
        blank line in a config file should not be the difference between a voice
        agent and a mute one, so it asks the account which voices exist and takes
        the first.
        """
        if self.voice_id:
            return self.voice_id
        try:
            req = urllib.request.Request(f"{ELEVEN_BASE}/voices",
                                         headers={"xi-api-key": self.api_key})
            with urllib.request.urlopen(req, timeout=15) as r:
                voices = json.loads(r.read()).get("voices", [])
            if voices:
                self.voice_id = voices[0]["voice_id"]
                self.on_state(f"voice-auto:{voices[0].get('name', self.voice_id)}")
                return self.voice_id
        except Exception as e:                              # noqa: BLE001
            self.on_state(f"voice-lookup-failed:{type(e).__name__}")
        return ""

    def say(self, text: str) -> None:
        if not self.api_key:
            self.on_state("tts-unavailable: no ELEVENLABS_API_KEY")
            return
        if not self.resolve_voice():
            self.on_state("tts-unavailable: no voices on this ElevenLabs account")
            return
        self._stop.clear()
        self.speaking.set()
        self.on_state("speaking")
        try:
            self._stream(text)
        finally:
            self.speaking.clear()
            self._stop.clear()
            self.on_state("idle")

    def _stream(self, text: str) -> None:
        import numpy as np
        import sounddevice as sd

        url = (f"{ELEVEN_BASE}/text-to-speech/{self.voice_id}/stream"
               f"?output_format=pcm_16000")
        body = json.dumps({"text": text, "model_id": TTS_MODEL}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r, \
             sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as out:
            while not self._stop.is_set():
                chunk = r.read(4096)
                if not chunk:
                    break
                out.write(np.frombuffer(chunk, dtype=np.int16))


def transcribe(frames: list, api_key: str = "") -> str:
    """ElevenLabs Scribe. Called only after the wake word has fired."""
    import numpy as np

    api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise VoiceUnavailable("ELEVENLABS_API_KEY is not set — check jarvis/.env")

    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    wav = _wav_header(len(pcm)) + pcm

    boundary = "----jarvis"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\n"
        f"{STT_MODEL}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"turn.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode(),
        wav, f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"{ELEVEN_BASE}/speech-to-text", data=b"".join(parts),
        headers={"xi-api-key": api_key,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("text", "").strip()


def _wav_header(n: int) -> bytes:
    import struct
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16) +
            b"data" + struct.pack("<I", n))


def available() -> tuple[bool, str]:
    """Can voice run at all? Degrade loudly, never silently.

    Only numpy and sounddevice are REQUIRED — they are the microphone itself.
    openwakeword is optional: without it you press a button instead of saying
    "Hey Jarvis", which is a smaller loss than having no voice.

    This function used to demand all three, which switched voice off entirely
    whenever the wake word failed to install — defeating the push-to-talk
    fallback that exists precisely for that case. openwakeword's runtime lags
    behind new Python releases, so that is the COMMON case, not an edge one.
    """
    missing = []
    for mod, why in (("sounddevice", "the microphone"), ("numpy", "audio maths")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(f"{mod} ({why})")
    if missing:
        return False, ("voice needs " + " and ".join(missing) +
                       " — run: pip install numpy sounddevice")
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return False, "ELEVENLABS_API_KEY is not set in jarvis/.env"

    try:
        __import__("openwakeword")
        return True, "ready — wake word available"
    except ImportError:
        return True, ("ready — PUSH TO TALK (openwakeword not installed, so no "
                      "'Hey Jarvis'; press the mic button or the space bar)")
