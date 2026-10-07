from __future__ import annotations

from citysports_efc_ble import (
    CountersFrame,
    CounterTracker,
    EfcState,
    UnknownFrame,
    WorkoutState,
    parse_frame,
)

from .conftest import DEVICE_INFO, frame, status


def test_state_merges_frames() -> None:
    state = EfcState()
    assert state.supports_incline is None
    state = state.with_frame(parse_frame(DEVICE_INFO))
    state = state.with_frame(parse_frame(status(speed=35, code=2)))
    state = state.with_frame(
        CountersFrame(elapsed=60, distance=50, energy=31, steps=70, heart_rate=0)
    )
    state = state.with_frame(parse_frame(frame(0x04, bytes(7) + b"\x05" + bytes(8))))
    assert state.system_id == "11:22:33:44:55:66"
    assert state.manufacturer_code == 0x22
    assert state.speed_kmh == 3.5
    assert state.workout_state is WorkoutState.RUNNING
    assert state.supports_incline is False
    assert state.elapsed_seconds == 60
    assert state.distance_m == 50
    assert state.energy_kcal == 3.1
    assert state.steps == 70
    assert state.workout_counter == 5
    assert state.with_frame(UnknownFrame(9, b"")) is state


def test_imperial_distance_is_converted() -> None:
    state = EfcState().with_frame(parse_frame(status(code=0x82)))
    state = state.with_frame(
        CountersFrame(elapsed=1, distance=100, energy=0, steps=0, heart_rate=0)
    )
    assert state.imperial is True
    assert state.distance_m == 161


def _counters(elapsed: int, energy: int, steps: int) -> CountersFrame:
    return CountersFrame(
        elapsed=elapsed, distance=0, energy=energy, steps=steps, heart_rate=0
    )


def test_tracker_continues_wraps() -> None:
    tracker = CounterTracker()
    assert tracker.correct(_counters(5990, 9990, 9995)) == _counters(5990, 9990, 9995)
    assert tracker.correct(_counters(3, 5, 2)) == _counters(6003, 10005, 10002)
    assert tracker.correct(_counters(10, 9, 9)) == _counters(6010, 10009, 10009)


def test_tracker_treats_other_drops_as_reset() -> None:
    tracker = CounterTracker()
    tracker.correct(_counters(5990, 9990, 9995))
    tracker.correct(_counters(3, 5, 2))
    assert tracker.correct(_counters(1, 1, 1)) == _counters(1, 1, 1)


def test_tracker_reset_forgets_history() -> None:
    tracker = CounterTracker()
    tracker.correct(_counters(5990, 9990, 9995))
    tracker.reset()
    assert tracker.correct(_counters(3, 5, 2)) == _counters(3, 5, 2)
