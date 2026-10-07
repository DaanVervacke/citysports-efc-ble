from __future__ import annotations

import pytest
from citysports_efc_ble import (
    CountersFrame,
    DeviceInfoFrame,
    EfcFault,
    EfcProtocolError,
    EfcValidationError,
    SportRecordFrame,
    StatusFrame,
    UnknownFrame,
    WorkoutState,
    is_efc_advertisement,
    parse_frame,
)
from citysports_efc_ble.protocol import (
    device_info_query,
    encode_command,
    incline_command,
    kmh_to_speed,
    pause_command,
    speed_command,
    speed_to_kmh,
    sport_record_query,
    start_command,
    stop_command,
    xor_checksum,
)

from .conftest import DEVICE_INFO, STATUS_STANDBY, frame, load_capture, status


def test_commands_match_captured_bytes() -> None:
    assert device_info_query() == bytes.fromhex("a10500a4")
    assert start_command() == bytes.fromhex("a1030101a2")
    assert stop_command() == bytes.fromhex("a1030105a6")
    assert pause_command() == bytes.fromhex("a1030103a0")
    assert speed_command(0x0B) == bytes.fromhex("a10102010ba8")
    assert speed_command(0x1F) == bytes.fromhex("a10102011fbc")
    assert incline_command(2) == bytes.fromhex("a102020102a2")
    assert sport_record_query() == encode_command(0x04, bytes.fromhex("0100000001"))
    assert sport_record_query()[:8] == bytes.fromhex("a104050100000001")


def test_xor_checksum() -> None:
    assert xor_checksum(b"") == 0
    assert xor_checksum(bytes.fromhex("a10500")) == 0xA4


def test_parse_status_standby() -> None:
    decoded = parse_frame(STATUS_STANDBY)
    assert decoded == StatusFrame(
        imperial=False,
        max_speed_raw=120,
        min_speed_raw=10,
        speed_raw=0,
        max_speed_kmh=12.0,
        min_speed_kmh=1.0,
        speed_kmh=0.0,
        max_incline_percent=0,
        min_incline_percent=0,
        incline_percent=0,
        status_code=6,
        workout_state=WorkoutState.STANDBY,
        fault=None,
    )


def test_parse_status_imperial_fault_and_unknown() -> None:
    imperial = parse_frame(status(speed=20, code=0x80 | 2, max_speed=75, min_speed=5))
    assert isinstance(imperial, StatusFrame)
    assert imperial.imperial is True
    assert imperial.speed_kmh == pytest.approx(3.219, abs=0.001)
    assert imperial.max_speed_kmh == pytest.approx(12.07, abs=0.01)
    assert imperial.workout_state is WorkoutState.RUNNING

    fault = parse_frame(status(code=23))
    assert isinstance(fault, StatusFrame)
    assert fault.fault is EfcFault.SAFETY_KEY_REMOVED
    assert fault.workout_state is None

    unknown = parse_frame(status(code=8))
    assert isinstance(unknown, StatusFrame)
    assert unknown.status_code == 8
    assert unknown.workout_state is None
    assert unknown.fault is None


def test_parse_counters() -> None:
    decoded = parse_frame(bytes.fromhex("1a020c007900bb003d010600000000ec"))
    assert decoded == CountersFrame(
        elapsed=121, distance=187, energy=61, steps=262, heart_rate=0
    )


def test_parse_device_info() -> None:
    decoded = parse_frame(DEVICE_INFO)
    assert isinstance(decoded, DeviceInfoFrame)
    assert decoded.manufacturer_code == 0x22
    assert decoded.model_code == 0x17
    assert decoded.revision == 1
    assert decoded.system_id_address == "11:22:33:44:55:66"


def test_parse_sport_record_and_unknown() -> None:
    payload = bytes(range(16))
    decoded = parse_frame(frame(0x04, payload))
    assert decoded == SportRecordFrame(workout_counter=0x0607, payload=payload)
    assert parse_frame(frame(0x09, b"\x01")) == UnknownFrame(0x09, b"\x01")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\x1a\x01", "shorter"),
        (bytes(21), "longer"),
        (bytes.fromhex("a10500a4"), "header"),
        (bytes.fromhex("1a010500"), "length byte"),
        (STATUS_STANDBY[:-1] + b"\x00", "checksum"),
        (frame(0x01, bytes(3)), "payload bytes"),
    ],
)
def test_parse_rejects_bad_frames(data: bytes, message: str) -> None:
    with pytest.raises(EfcProtocolError, match=message):
        parse_frame(data)


def test_speed_conversions() -> None:
    assert speed_to_kmh(35, False) == 3.5
    assert kmh_to_speed(3.5, False) == 35
    assert kmh_to_speed(3.219, True) == 20
    with pytest.raises(EfcValidationError, match="finite"):
        kmh_to_speed(float("nan"), False)
    with pytest.raises(EfcValidationError, match="one byte"):
        kmh_to_speed(-1, False)
    with pytest.raises(EfcValidationError, match="one byte"):
        kmh_to_speed(30, False)


def test_is_efc_advertisement() -> None:
    assert is_efc_advertisement("CITYSPORTS-LINKER")
    assert is_efc_advertisement("citysports-linker")
    assert is_efc_advertisement(None, ["FFEEDDCC-BBAA-9988-7766-554433221100"])
    assert not is_efc_advertisement(None)
    assert not is_efc_advertisement(
        "ESLinker", ["0000fff0-0000-1000-8000-00805f9b34fb"]
    )


@pytest.mark.parametrize("name", ["wp9_empty_belt.jsonl", "wp9_walking.jsonl"])
def test_every_captured_frame_decodes(name: str) -> None:
    rows = load_capture(name)
    known_commands = {
        device_info_query(),
        start_command(),
        stop_command(),
        *(speed_command(raw) for raw in range(256)),
    }
    for row in rows:
        data = bytes.fromhex(row["hex"])
        if row["dir"] == "rx":
            assert not isinstance(parse_frame(data), UnknownFrame)
        else:
            assert data in known_commands
