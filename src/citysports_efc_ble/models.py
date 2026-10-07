"""Typed EFC models."""

from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum

from .const import KM_PER_MILE


class WorkoutState(IntEnum):
    """Workout state codes from the status byte.

    ``PAUSING`` and ``PAUSED`` come from the EQiSports app and were not
    seen on real hardware yet.
    """

    IDLE = 0
    COUNTDOWN = 1
    RUNNING = 2
    PAUSING = 3
    PAUSED = 4
    SUMMARY = 5
    STANDBY = 6


class EfcFault(IntEnum):
    """Fault codes from the status byte.

    The codes and their meaning come from the EQiSports app. None was
    seen on real hardware yet.
    """

    BOARD_COMMUNICATION = 17
    SURGE = 18
    INCLINE_LINE_DISCONNECTED = 20
    OVERCURRENT = 21
    SAFETY_KEY_REMOVED = 23
    CONSOLE_COMMUNICATION = 25


class ConnectionStatus(StrEnum):
    """Protocol session status.

    A session moves from ``DISCONNECTED`` to ``CONNECTED`` when the
    transport connects, and to ``READY`` once a device info frame and a
    status frame arrived. Controls need ``READY``.
    """

    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class StatusFrame:
    """Decoded ``1A 01`` status frame.

    Speeds are in km/h, also for imperial units. The ``*_raw`` fields hold
    the wire bytes, in 0.1 km/h or 0.1 mph steps. ``workout_state`` and
    ``fault`` are None when ``status_code`` is not a known code.
    """

    imperial: bool
    max_speed_raw: int
    min_speed_raw: int
    speed_raw: int
    max_speed_kmh: float
    min_speed_kmh: float
    speed_kmh: float
    max_incline_percent: int
    min_incline_percent: int
    incline_percent: int
    status_code: int
    workout_state: WorkoutState | None
    fault: EfcFault | None


@dataclass(frozen=True, slots=True)
class CountersFrame:
    """Decoded ``1A 02`` counters frame, in wire units.

    ``energy`` is in 0.1 kcal. ``distance`` is in metres on metric units.
    The client corrects counter wraps before it applies the frame, so the
    values can exceed 16 bits after correction.
    """

    elapsed: int
    distance: int
    energy: int
    steps: int
    heart_rate: int


@dataclass(frozen=True, slots=True)
class DeviceInfoFrame:
    """Decoded ``1A 05`` device info frame.

    ``system_id`` holds the six wire bytes. On the CITYSPORTS WP9 they are
    the Bluetooth address in reverse order.
    """

    manufacturer_code: int
    model_code: int
    revision: int
    system_id: bytes

    @property
    def system_id_address(self) -> str:
        """The system id as a colon separated address, in display order."""
        return ":".join(f"{byte:02X}" for byte in reversed(self.system_id))


@dataclass(frozen=True, slots=True)
class SportRecordFrame:
    """Decoded ``1A 04`` sport record frame.

    Only the workout counter has a known meaning. ``payload`` keeps every
    byte for later analysis.
    """

    workout_counter: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class UnknownFrame:
    """A valid frame with a type this library does not decode."""

    frame_type: int
    payload: bytes


type EfcFrame = (
    StatusFrame | CountersFrame | DeviceInfoFrame | SportRecordFrame | UnknownFrame
)
"""Any decoded inbound frame."""


@dataclass(frozen=True, slots=True)
class EfcState:
    """Latest decoded treadmill state, merged across frames.

    Speeds are in km/h and distances in metres, also on imperial units.
    The counters are per workout. The treadmill resets them when it
    returns to standby. Fields stay None until the matching frame
    arrives.
    """

    manufacturer_code: int | None = None
    model_code: int | None = None
    revision: int | None = None
    system_id: str | None = None
    imperial: bool | None = None
    min_speed_kmh: float | None = None
    max_speed_kmh: float | None = None
    min_incline_percent: int | None = None
    max_incline_percent: int | None = None
    speed_kmh: float | None = None
    incline_percent: int | None = None
    status_code: int | None = None
    workout_state: WorkoutState | None = None
    fault: EfcFault | None = None
    elapsed_seconds: int | None = None
    distance_m: int | None = None
    energy_kcal: float | None = None
    steps: int | None = None
    heart_rate_bpm: int | None = None
    workout_counter: int | None = None

    @property
    def supports_incline(self) -> bool | None:
        """Whether the treadmill reports an incline range above zero."""
        if self.max_incline_percent is None:
            return None
        return self.max_incline_percent > 0

    def with_frame(self, frame: EfcFrame) -> EfcState:
        """Apply one decoded frame.

        Args:
            frame: A decoded frame. Counter frames must already be
                corrected for wraps.

        Returns:
            The state with the frame applied. Unknown frames leave it
            unchanged.
        """
        match frame:
            case StatusFrame():
                return replace(
                    self,
                    imperial=frame.imperial,
                    min_speed_kmh=frame.min_speed_kmh,
                    max_speed_kmh=frame.max_speed_kmh,
                    min_incline_percent=frame.min_incline_percent,
                    max_incline_percent=frame.max_incline_percent,
                    speed_kmh=frame.speed_kmh,
                    incline_percent=frame.incline_percent,
                    status_code=frame.status_code,
                    workout_state=frame.workout_state,
                    fault=frame.fault,
                )
            case CountersFrame():
                distance = (
                    round(frame.distance * KM_PER_MILE)
                    if self.imperial
                    else frame.distance
                )
                return replace(
                    self,
                    elapsed_seconds=frame.elapsed,
                    distance_m=distance,
                    energy_kcal=frame.energy / 10,
                    steps=frame.steps,
                    heart_rate_bpm=frame.heart_rate,
                )
            case DeviceInfoFrame():
                return replace(
                    self,
                    manufacturer_code=frame.manufacturer_code,
                    model_code=frame.model_code,
                    revision=frame.revision,
                    system_id=frame.system_id_address,
                )
            case SportRecordFrame():
                return replace(self, workout_counter=frame.workout_counter)
            case _:
                return self


@dataclass(frozen=True, slots=True)
class EfcUpdate:
    """State update passed to the client's ``update_callback``.

    ``state`` and ``status`` are the client values after ``frame`` was
    applied. ``raw`` holds the frame bytes as received.
    """

    state: EfcState
    status: ConnectionStatus
    frame: EfcFrame
    raw: bytes
