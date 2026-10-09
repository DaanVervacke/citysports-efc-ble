"""EFC frame encoding and decoding.

Every frame is ``header, type, length, payload, xor``. The header is
``0x1A`` from the treadmill and ``0xA1`` to it. The last byte is the XOR of
every earlier byte.
"""

import math
from collections.abc import Callable, Iterable, Mapping
from typing import Final

from .const import (
    COMMAND_CONTROL,
    COMMAND_DEVICE_INFO,
    COMMAND_INCLINE,
    COMMAND_SPEED,
    COMMAND_SPORT_RECORD,
    CONTROL_PAUSE,
    CONTROL_START,
    CONTROL_STOP,
    FRAME_COUNTERS,
    FRAME_DEVICE_INFO,
    FRAME_SPORT_RECORD,
    FRAME_STATUS,
    INBOUND_HEADER,
    KM_PER_MILE,
    MAX_FRAME_BYTES,
    NAME_PREFIX,
    OUTBOUND_HEADER,
    SERVICE_UUID,
    STATUS_CODE_MASK,
    STATUS_IMPERIAL_BIT,
)
from .exceptions import EfcProtocolError, EfcValidationError
from .models import (
    CountersFrame,
    DeviceInfoFrame,
    EfcFault,
    EfcFrame,
    SportRecordFrame,
    StatusFrame,
    UnknownFrame,
    WorkoutState,
)

__all__ = ["is_efc_advertisement", "parse_frame"]

_WORKOUT_CODES = frozenset(WorkoutState)
_FAULT_CODES = frozenset(EfcFault)


def xor_checksum(data: bytes) -> int:
    """Return the XOR of every byte in ``data``."""
    value = 0
    for byte in data:
        value ^= byte
    return value


def encode_command(command: int, payload: bytes = b"") -> bytes:
    """Build one outbound frame with its checksum.

    Args:
        command: The frame type byte.
        payload: The payload bytes.

    Returns:
        The full frame, ready to write.
    """
    body = bytes((OUTBOUND_HEADER, command, len(payload))) + payload
    return body + bytes((xor_checksum(body),))


def speed_to_kmh(raw: int, imperial: bool) -> float:
    """Convert a speed byte (0.1 km/h or 0.1 mph steps) to km/h."""
    if imperial:
        return round(raw * KM_PER_MILE / 10, 3)
    return raw / 10


def kmh_to_speed(kmh: float, imperial: bool) -> int:
    """Convert km/h to the nearest speed byte (0.1 km/h or 0.1 mph steps).

    Raises:
        EfcValidationError: The value is not a number, is not finite or
            does not fit in one byte.
    """
    if isinstance(kmh, bool) or not isinstance(kmh, int | float):
        raise EfcValidationError("Speed must be a number")
    if not math.isfinite(kmh):
        raise EfcValidationError("Speed must be a finite number")
    raw = round(kmh * 10 / KM_PER_MILE) if imperial else round(kmh * 10)
    if not 0 <= raw <= 0xFF:
        raise EfcValidationError("Speed cannot be encoded in one byte")
    return raw


def device_info_query() -> bytes:
    """Build the device info query, ``A1 05 00 A4``."""
    return encode_command(COMMAND_DEVICE_INFO)


def start_command() -> bytes:
    """Build the start command. The treadmill also uses it to resume."""
    return encode_command(COMMAND_CONTROL, bytes((CONTROL_START,)))


def pause_command() -> bytes:
    """Build the pause command, ``A1 03 01 03 A0``.

    The bytes come from the Trught notes. Untested on real hardware.
    """
    return encode_command(COMMAND_CONTROL, bytes((CONTROL_PAUSE,)))


def stop_command() -> bytes:
    """Build the stop command, ``A1 03 01 05 A6``.

    The treadmill shows the workout summary and slows the belt down.
    """
    return encode_command(COMMAND_CONTROL, bytes((CONTROL_STOP,)))


def speed_command(raw: int) -> bytes:
    """Build a speed command from a speed byte."""
    return encode_command(COMMAND_SPEED, bytes((0x01, raw)))


def incline_command(percent: int) -> bytes:
    """Build an incline command from a whole percent value."""
    return encode_command(COMMAND_INCLINE, bytes((0x01, percent)))


def sport_record_query() -> bytes:
    """Build the sport record query, ``A1 04 05 01 00 00 00 01 A0``.

    The bytes come from the Trught notes. The treadmill answers with a
    ``1A 04`` sport record frame.
    """
    return encode_command(COMMAND_SPORT_RECORD, bytes((0x01, 0x00, 0x00, 0x00, 0x01)))


def parse_frame(data: bytes) -> EfcFrame:
    """Decode one inbound frame.

    Args:
        data: The notification payload.

    Returns:
        The decoded frame. Valid frames of an unknown type decode to
        ``UnknownFrame``.

    Raises:
        EfcProtocolError: The frame is too short, too long, has the wrong
            header or length, or fails the checksum.
    """
    if len(data) < 4:
        raise EfcProtocolError("EFC frame is shorter than 4 bytes")
    if len(data) > MAX_FRAME_BYTES:
        raise EfcProtocolError(f"EFC frame is longer than {MAX_FRAME_BYTES} bytes")
    if data[0] != INBOUND_HEADER:
        raise EfcProtocolError(
            f"EFC frame header is 0x{data[0]:02X}, not 0x{INBOUND_HEADER:02X}"
        )
    if len(data) != data[2] + 4:
        raise EfcProtocolError("EFC frame length byte does not match its size")
    if xor_checksum(data[:-1]) != data[-1]:
        raise EfcProtocolError("EFC frame checksum is invalid")
    frame_type = data[1]
    payload = bytes(data[3:-1])
    decoder = _DECODERS.get(frame_type)
    if decoder is None:
        return UnknownFrame(frame_type, payload)
    expected, decode = decoder
    if len(payload) != expected:
        raise EfcProtocolError(
            f"EFC frame type 0x{frame_type:02X} has {len(payload)} payload "
            f"bytes, expected {expected}"
        )
    return decode(payload)


def _parse_counters(payload: bytes) -> CountersFrame:
    return CountersFrame(
        elapsed=int.from_bytes(payload[0:2]),
        distance=int.from_bytes(payload[2:4]),
        energy=int.from_bytes(payload[4:6]),
        steps=int.from_bytes(payload[6:8]),
        heart_rate=payload[8],
    )


def _parse_device_info(payload: bytes) -> DeviceInfoFrame:
    return DeviceInfoFrame(
        manufacturer_code=int.from_bytes(payload[0:2]),
        model_code=int.from_bytes(payload[2:4]),
        revision=int.from_bytes(payload[4:6]),
        system_id=payload[6:12],
    )


def _parse_sport_record(payload: bytes) -> SportRecordFrame:
    return SportRecordFrame(
        workout_counter=int.from_bytes(payload[6:8]), payload=payload
    )


def _parse_status(payload: bytes) -> StatusFrame:
    status = payload[6]
    imperial = bool(status & STATUS_IMPERIAL_BIT)
    code = status & STATUS_CODE_MASK
    workout_state = WorkoutState(code) if code in _WORKOUT_CODES else None
    fault = EfcFault(code) if code in _FAULT_CODES else None
    return StatusFrame(
        imperial=imperial,
        max_speed_raw=payload[0],
        min_speed_raw=payload[1],
        speed_raw=payload[4],
        max_speed_kmh=speed_to_kmh(payload[0], imperial),
        min_speed_kmh=speed_to_kmh(payload[1], imperial),
        speed_kmh=speed_to_kmh(payload[4], imperial),
        max_incline_percent=payload[2],
        min_incline_percent=payload[3],
        incline_percent=payload[5],
        status_code=code,
        workout_state=workout_state,
        fault=fault,
    )


_DECODERS: Final[Mapping[int, tuple[int, Callable[[bytes], EfcFrame]]]] = {
    FRAME_STATUS: (9, _parse_status),
    FRAME_COUNTERS: (12, _parse_counters),
    FRAME_SPORT_RECORD: (16, _parse_sport_record),
    FRAME_DEVICE_INFO: (12, _parse_device_info),
}


def is_efc_advertisement(name: str | None, service_uuids: Iterable[str] = ()) -> bool:
    """Tell whether a BLE advertisement looks like an EFC treadmill.

    Args:
        name: The advertised local name, if any.
        service_uuids: The advertised service UUIDs.

    Returns:
        True when the EFC service UUID is advertised or the name starts
        with ``CITYSPORTS``, ignoring case.
    """
    if any(uuid.lower() == SERVICE_UUID for uuid in service_uuids):
        return True
    return name is not None and name.upper().startswith(NAME_PREFIX)
