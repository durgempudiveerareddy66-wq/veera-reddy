"""The loop that actually drives the microphone.

Step 4 built the parts — ring buffer, wake gate, turn detector, Scribe, TTS — and
left them unassembled. This is the thing that runs them.

    idle → (wake word OR push-to-talk) → record until silence → Scribe
         → the planner → the Action bus → speak the reply → idle

Two modes, and the second one matters more than it looks:

*   **wake** — openWakeWord listening continuously, on-device. Nothing leaves the
    machine until "Hey Jarvis" fires.
*   **ptt** — push-to-talk. A button in the HUD, or a global hotkey. This is the
    fallback when openWakeWord will not install, which on new Python releases is
    common. Voice still works; it just needs a press instead of a phrase.

The microphone goes deaf while JARVIS is speaking. Without that it transcribes
itself through the speakers and talks to itself forever.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .voice import (FRAME_SAMPLES, MAX_UTTERANCE_S, PREROLL_MS, RingBuffer,
                    SAMPLE_RATE, Speaker, TurnDetector, VoiceUnavailable,
                    WakeGate, resolve_device, rms, transcribe)


class VoiceLoop:
    """Owns the microphone. One instance per process."""

    def __init__(
        self,
        *,
        on_transcript: Callable[[str], str],
        on_event: Callable[[str, dict], None] | None = None,
        mic_name: str = "",
        api_key: str = "",
        voice_id: str = "",
        use_wake_word: bool = True,
    ):
        self.on_transcript = on_transcript      # returns what JARVIS should say back
        self.on_event = on_event or (lambda k, p: None)
        self.mic_name = mic_name
        self.api_key = api_key
        self.voice_id = voice_id
        self.use_wake_word = use_wake_word

        self.ring = RingBuffer(ms=PREROLL_MS)
        self.turn = TurnDetector()
        self.gate = WakeGate(on_trigger=lambda d: self.on_event("wake", d))
        self.speaker = Speaker(api_key=api_key, voice_id=voice_id,
                               on_state=lambda s: self.on_event("state", {"state": s}))

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._armed = threading.Event()      # a turn has been triggered
        self._wake_model = None
        self.mode = "wake" if use_wake_word else "ptt"
        self.error = ""
        self.running = False

    # -- control -----------------------------------------------------------

    def push_to_talk(self) -> None:
        """Start a turn now. The HUD mic button and the global hotkey call this."""
        self._armed.set()

    def barge_in(self) -> None:
        """Stop JARVIS talking. Explicit, never automatic."""
        self.speaker.barge_in()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.running = False

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "mode": self.mode, "error": self.error,
                "speaking": self.speaker.speaking.is_set()}

    # -- the loop ----------------------------------------------------------

    def _load_wake_word(self):
        """Try openWakeWord. Falling back to push-to-talk is not a failure."""
        try:
            from openwakeword.model import Model
            m = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            self.mode = "wake"
            return m
        except Exception as e:                              # noqa: BLE001
            self.mode = "ptt"
            self.error = (
                f"wake word unavailable ({type(e).__name__}) — push-to-talk only. "
                "Voice still works; press the mic button or the hotkey."
            )
            self.on_event("degraded", {"why": self.error})
            return None

    def _run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as e:
            self.error = (f"{e.name} is not installed — voice cannot start. "
                          "Run: .venv\\Scripts\\pip install numpy sounddevice")
            self.on_event("failed", {"error": self.error})
            return

        if self.use_wake_word:
            try:
                __import__("openwakeword")
                self._wake_model = self._load_wake_word()
            except ImportError:
                self.mode = "ptt"
                self.error = ("openwakeword is not installed — push-to-talk only. "
                              "Press the mic button or the space bar.")
                self.on_event("degraded", {"why": self.error})
        else:
            self.mode = "ptt"

        try:
            device = resolve_device(self.mic_name)
        except VoiceUnavailable as e:
            self.error = str(e)
            self.on_event("failed", {"error": self.error})
            return

        try:
            stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                    dtype="float32", blocksize=FRAME_SAMPLES,
                                    device=device)
            stream.start()
        except Exception as e:                              # noqa: BLE001
            # §9.4 — a device index that moved, or a mic held by another app.
            self.error = (f"could not open the microphone ({type(e).__name__}: {e}). "
                          "If you plugged in a headset, re-run setup.ps1 to "
                          "re-enumerate devices.")
            self.on_event("failed", {"error": self.error})
            return

        self.running = True
        self.on_event("ready", {"mode": self.mode, "device": device})
        recording: list = []

        try:
            while not self._stop.is_set():
                # Deaf while speaking, or it hears itself and loops forever.
                if self.speaker.speaking.is_set():
                    try:
                        stream.read(FRAME_SAMPLES)      # drain and discard
                    except Exception:                   # noqa: BLE001
                        pass
                    continue

                try:
                    frame, overflowed = stream.read(FRAME_SAMPLES)
                except Exception as e:                  # noqa: BLE001
                    # A mic unplugged mid-turn lands here. Loud, not silent.
                    self.error = f"microphone lost mid-turn: {type(e).__name__}: {e}"
                    self.on_event("failed", {"error": self.error})
                    self.running = False
                    return

                mono = frame[:, 0]
                level = float(rms(mono.tolist()))
                self.on_event("rms", {"value": min(1.0, level * 6)})

                if not self._armed.is_set():
                    self.ring.push(mono.copy())
                    if self._wake_model is not None:
                        pcm = (mono * 32767).astype(np.int16)
                        scores = self._wake_model.predict(pcm)
                        best = max(scores.values()) if scores else 0.0
                        if self.gate.offer(float(best)):
                            self._begin_turn(recording)
                    continue

                # armed — we are recording an utterance
                recording.append(mono.copy())
                state = self.turn.feed(level)
                if state == "listening":
                    continue
                if state == "timeout":
                    self.on_event("said", {"text": "That went on a while — I stopped listening."})
                self._end_turn(recording)
                recording = []
        finally:
            try:
                stream.stop(); stream.close()
            except Exception:                           # noqa: BLE001
                pass
            self.running = False

    def _begin_turn(self, recording: list) -> None:
        recording.clear()
        recording.extend(self.ring.drain())     # the preroll — what he said first
        self.turn.reset()
        self._armed.set()
        self.on_event("state", {"state": "listening"})

    def _end_turn(self, recording: list) -> None:
        self._armed.clear()
        self.turn.reset()
        self.on_event("state", {"state": "thinking"})
        try:
            text = transcribe(recording, self.api_key)
        except Exception as e:                          # noqa: BLE001
            self.on_event("failed", {"error": f"speech-to-text failed: {e}"})
            self.on_event("state", {"state": "idle"})
            return
        if not text.strip():
            self.on_event("state", {"state": "idle"})
            return

        self.on_event("transcript", {"text": text})
        try:
            reply = self.on_transcript(text)
        except Exception as e:                          # noqa: BLE001
            reply = f"That failed: {type(e).__name__}"
            self.on_event("failed", {"error": str(e)})

        if reply:
            self.speaker.say(reply)
        self.on_event("state", {"state": "idle"})
