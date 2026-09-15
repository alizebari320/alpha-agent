"""Wake word detection (M2).

M1 stub. openWakeWord + silero-VAD land here; the daemon will call
`WakeDetector.run_forever(callback)` from its idle loop.
"""

from __future__ import annotations

# Tier 1 bundled pretrained openWakeWord models (chosen in the first-run wizard).
BUNDLED_WAKEWORDS = ("hey_alpha", "hey_jarvis", "hey_computer", "hey_assistant", "hey_mycroft")


class WakeDetector:
    """Placeholder — implemented in M2."""

    def __init__(self, model: str, threshold: float = 0.5):
        self.model = model
        self.threshold = threshold

    async def run_forever(self, on_wake) -> None:
        raise NotImplementedError("M2")