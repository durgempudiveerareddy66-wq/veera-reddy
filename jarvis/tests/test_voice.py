"""Build step 4 — the voice logic, against synthetic audio.

A build machine has no microphone, so none of this touches hardware. What it does
test is everything that decides *when* to listen and *when* a turn has ended —
which is where voice loops actually go wrong.

What these tests CANNOT prove, and only Reddie's machine can: that sounddevice
opens the right device, that openWakeWord fires on his voice, that ElevenLabs
returns audio, that the global hotkey registers.

    python jarvis/tests/test_voice.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.voice import (FRAME_MS, PREROLL_MS, RingBuffer, SILENCE_HOLD_MS,
                         SILENCE_RMS, TurnDetector, WakeGate, rms)

def _strip_js_comments(src: str) -> str:
    """Remove /* */ and // comments. Crude, but this is our own source."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


LOUD = [0.4, -0.4] * 32
QUIET = [0.0005, -0.0005] * 32


class TestRms(unittest.TestCase):
    def test_silence_reads_near_zero(self):
        self.assertLess(rms([0.0] * 64), SILENCE_RMS)

    def test_speech_reads_above_the_threshold(self):
        self.assertGreater(rms(LOUD), SILENCE_RMS)

    def test_an_empty_frame_does_not_divide_by_zero(self):
        self.assertEqual(rms([]), 0.0)


class TestRingBuffer(unittest.TestCase):
    def test_it_holds_only_the_preroll_window(self):
        rb = RingBuffer(ms=PREROLL_MS)
        for i in range(10_000):
            rb.push([i])
        self.assertEqual(len(rb), PREROLL_MS // FRAME_MS)

    def test_old_audio_is_discarded_not_accumulated(self):
        # The privacy claim in the README rests on this: while idle, everything
        # the microphone hears is overwritten, so there is nothing to leak.
        rb = RingBuffer(ms=128)
        for i in range(500):
            rb.push([i])
        kept = rb.drain()
        self.assertEqual(len(kept), 4)
        self.assertEqual(kept[-1], [499])
        self.assertNotIn([0], kept)

    def test_draining_empties_it(self):
        rb = RingBuffer(ms=320)
        rb.push([1]); rb.push([2])
        rb.drain()
        self.assertEqual(len(rb), 0)


class TestTurnDetector(unittest.TestCase):
    def setUp(self):
        self.d = TurnDetector()
        self.d.reset()

    def test_silence_before_speech_does_not_end_the_turn(self):
        # Reddie pressing the button and pausing before speaking must not be
        # read as a finished, empty utterance.
        for _ in range(200):
            self.assertEqual(self.d.feed(0.0), "listening")

    def test_a_pause_between_words_does_not_end_the_turn(self):
        self.d.feed(0.3)
        gap = (SILENCE_HOLD_MS // FRAME_MS) - 2
        for _ in range(gap):
            self.assertEqual(self.d.feed(0.0), "listening")
        self.assertEqual(self.d.feed(0.3), "listening")     # they kept going

    def test_enough_continuous_silence_ends_the_turn(self):
        self.d.feed(0.3)
        out = "listening"
        for _ in range(SILENCE_HOLD_MS // FRAME_MS + 1):
            out = self.d.feed(0.0)
        self.assertEqual(out, "ended")

    def test_a_stuck_open_microphone_times_out(self):
        self.d.feed(0.3)
        out = "listening"
        for _ in range(40_000 // FRAME_MS):
            out = self.d.feed(0.5)          # never goes quiet
            if out == "timeout":
                break
        self.assertEqual(out, "timeout")

    def test_reset_clears_the_previous_turn(self):
        self.d.feed(0.3)
        for _ in range(SILENCE_HOLD_MS // FRAME_MS + 1):
            self.d.feed(0.0)
        self.d.reset()
        self.assertEqual(self.d.feed(0.0), "listening")


class TestWakeGate(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.g = WakeGate(on_trigger=self.seen.append)

    def test_a_confident_trigger_fires(self):
        self.assertTrue(self.g.offer(0.9, now=100.0))

    def test_a_weak_trigger_is_rejected(self):
        self.assertFalse(self.g.offer(0.2, now=100.0))
        self.assertEqual(self.seen[-1]["why"], "below threshold")

    def test_every_trigger_is_reported_including_the_rejected_ones(self):
        # §9.3 — a false-fire from a podcast must be visible in the HUD, not
        # silently swallowed, or Reddie cannot tell a deaf mic from a picky one.
        self.g.offer(0.1, now=100.0)
        self.g.offer(0.95, now=101.0)
        self.assertEqual(len(self.seen), 2)
        self.assertFalse(self.seen[0]["accepted"])
        self.assertTrue(self.seen[1]["accepted"])

    def test_cooldown_stops_one_utterance_firing_twice(self):
        self.assertTrue(self.g.offer(0.9, now=100.0))
        self.assertFalse(self.g.offer(0.9, now=100.5))
        self.assertEqual(self.seen[-1]["why"], "cooldown")

    def test_the_cooldown_expires(self):
        self.assertTrue(self.g.offer(0.9, now=100.0))
        self.assertTrue(self.g.offer(0.9, now=102.0))


class TestStructuralGuarantees(unittest.TestCase):
    def test_the_wake_gate_cannot_confirm_anything(self):
        """§9.3: false-firing into a RED action must be structurally impossible.

        The proof is by absence. WakeGate's entire public surface is offer() ->
        bool. It has no path to the policy engine, no way to produce a
        confirmed_by value, and nothing that returns 'click' or 'code'. A wake
        word can start a turn and nothing else — so no amount of false-firing
        can release a RED action, however many times a television says the word.
        """
        surface = [n for n in dir(WakeGate) if not n.startswith("_")]
        self.assertEqual(surface, ["offer"])      # one method, returning a bool

    def test_the_tunables_are_at_the_top_of_the_module_as_named_constants(self):
        # §5 asks for both silence numbers to be tunable without reading the file.
        import agent.voice as v
        for name in ("SAMPLE_RATE", "SILENCE_RMS", "SILENCE_HOLD_MS", "WAKE_THRESHOLD"):
            self.assertTrue(hasattr(v, name), name)
        self.assertEqual(v.SILENCE_HOLD_MS, 900)     # ~900ms, per the spec
        self.assertEqual(v.SAMPLE_RATE, 16_000)

    def test_no_web_speech_api_and_no_browser_audio_anywhere(self):
        """§5: the page never touches audio, and the Web Speech API is banned.

        Comments are stripped first — app.js says in prose that it never touches
        getUserMedia, and a grep over raw text flags that sentence as a violation
        of the rule it is documenting.
        """
        ui = Path(__file__).resolve().parent.parent / "ui"
        for f in ui.glob("*.js"):
            code = _strip_js_comments(f.read_text())
            for banned in ("getUserMedia", "webkitSpeechRecognition",
                           "SpeechRecognition", "MediaRecorder", "AudioContext"):
                self.assertNotIn(banned, code, f"{banned} in {f.name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
