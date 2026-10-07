"""Shared fakes and frames for the EFC test suite."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from citysports_efc_ble.client import DisconnectedCallback, NotificationCallback

FIXTURES = Path(__file__).parent / "fixtures"

DEVICE_INFO = bytes.fromhex("1a050c00220017000166554433221150")
STATUS_STANDBY = bytes.fromhex("1a0109780a0000000006000066")
STATUS_RUNNING_20 = bytes.fromhex("1a0109780a0000140002000072")
COUNTERS_ZERO = bytes.fromhex("1a020c00000000000000000000000014")


def frame(frame_type: int, payload: bytes) -> bytes:
    body = bytes((0x1A, frame_type, len(payload))) + payload
    checksum = 0
    for byte in body:
        checksum ^= byte
    return body + bytes((checksum,))


def status(
    *,
    speed: int = 0,
    code: int = 6,
    max_speed: int = 120,
    min_speed: int = 10,
    max_incline: int = 0,
    min_incline: int = 0,
    incline: int = 0,
) -> bytes:
    return frame(
        0x01,
        bytes(
            (max_speed, min_speed, max_incline, min_incline, speed, incline, code, 0, 0)
        ),
    )


def counters(
    *, elapsed: int = 0, distance: int = 0, energy: int = 0, steps: int = 0
) -> bytes:
    return frame(
        0x02,
        elapsed.to_bytes(2)
        + distance.to_bytes(2)
        + energy.to_bytes(2)
        + steps.to_bytes(2)
        + bytes(4),
    )


def load_capture(name: str) -> list[dict[str, Any]]:
    lines = (FIXTURES / name).read_text().splitlines()
    return [json.loads(line) for line in lines]


class FakeTransport:
    """In-memory transport that answers the device info query."""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.replies = [DEVICE_INFO, STATUS_STANDBY] if replies is None else replies
        self.writes: list[bytes] = []
        self.write_times: list[float] = []
        self.responses: list[bool] = []
        self.callback: NotificationCallback | None = None
        self.disconnected_callback: DisconnectedCallback | None = None
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stop_notify_calls = 0
        self.fail_writes = False
        self.fail_connect: BaseException | None = None
        self.on_write: Callable[[bytes], None] | None = None

    async def connect(self, disconnected_callback: DisconnectedCallback) -> None:
        self.connect_calls += 1
        if self.fail_connect is not None:
            raise self.fail_connect
        self.disconnected_callback = disconnected_callback

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def start_notify(
        self, characteristic: str, callback: NotificationCallback
    ) -> None:
        self.callback = callback

    async def stop_notify(self, characteristic: str) -> None:
        self.stop_notify_calls += 1

    async def write_gatt_char(
        self, characteristic: str, data: bytes, response: bool = False
    ) -> None:
        if self.fail_writes:
            raise OSError("write failed")
        self.writes.append(data)
        self.write_times.append(asyncio.get_running_loop().time())
        self.responses.append(response)
        if self.on_write is not None:
            self.on_write(data)
        if data == bytes.fromhex("a10500a4"):
            for reply in self.replies:
                self.notify(reply)

    def notify(self, data: bytes) -> None:
        assert self.callback is not None
        self.callback("ffeeddcc-bbaa-9988-7766-554433221102", data)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0.01)
