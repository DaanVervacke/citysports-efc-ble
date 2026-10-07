"""Counter wrap correction.

The EQiSports app treats elapsed time as wrapping at 6000 s, steps at
10000 and energy at 10000 (0.1 kcal). None of these wraps was captured on
real hardware. The tracker adds the modulus only when a counter falls
from the top tenth of its range into the bottom tenth during one workout.
Any other drop counts as a reset.
"""

from dataclasses import dataclass, replace

from .const import ELAPSED_MODULUS, ENERGY_MODULUS, STEPS_MODULUS
from .models import CountersFrame


@dataclass(slots=True)
class _Counter:
    modulus: int
    previous: int | None = None
    offset: int = 0

    def correct(self, raw: int) -> int:
        previous = self.previous
        if previous is not None and raw < previous:
            window = self.modulus // 10
            wrapped = previous >= self.modulus - window and raw < window
            self.offset = self.offset + self.modulus if wrapped else 0
        self.previous = raw
        return self.offset + raw


class CounterTracker:
    """Correct counter wraps within one workout."""

    def __init__(self) -> None:
        """Start with no history."""
        self._elapsed = _Counter(ELAPSED_MODULUS)
        self._energy = _Counter(ENERGY_MODULUS)
        self._steps = _Counter(STEPS_MODULUS)

    def reset(self) -> None:
        """Forget the history, for example when the treadmill returns to standby."""
        for counter in (self._elapsed, self._energy, self._steps):
            counter.previous = None
            counter.offset = 0

    def correct(self, frame: CountersFrame) -> CountersFrame:
        """Return ``frame`` with wrapped counters continued.

        Args:
            frame: A counters frame in wire units.

        Returns:
            The frame with elapsed time, energy and steps corrected.
        """
        return replace(
            frame,
            elapsed=self._elapsed.correct(frame.elapsed),
            energy=self._energy.correct(frame.energy),
            steps=self._steps.correct(frame.steps),
        )
