"""Frames, fakes and wait helpers for the EFC test suite."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from citysports_efc_ble import (
    ConnectionLostCallback,
    DisconnectedCallback,
    EfcClient,
    NotificationCallback,
    UpdateCallback,
)
from citysports_efc_ble.const import NOTIFY_CHARACTERISTIC_UUID
from citysports_efc_ble.protocol import device_info_query

FIXTURES = Path(__file__).parent / "fixtures"

DEVICE_INFO = bytes.fromhex("1a050c00220017000166554433221150")
STATUS_STANDBY = bytes.fromhex("1a0109780a0000000006000066")
STATUS_RUNNING_20 = bytes.fromhex("1a0109780a0000140002000072")
COUNTERS_ZERO = bytes.fromhex("1a020c00000000000000000000000014")

WAIT_SECONDS = 2.0


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
    """In-memory transport that answers the device info query.

    Like Bleak, ``disconnect()`` calls the disconnected callback while the
    link is up.
    """

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.replies = [DEVICE_INFO, STATUS_STANDBY] if replies is None else replies
        self.writes: list[bytes] = []
        self.write_characteristics: list[str] = []
        self.notify_characteristics: list[str] = []
        self.write_times: list[float] = []
        self.responses: list[bool] = []
        self.callback: NotificationCallback | None = None
        self.disconnected_callback: DisconnectedCallback | None = None
        self.linked = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stop_notify_calls = 0
        self.fail_writes = False
        self.fail_connect: BaseException | None = None
        self.fail_start_notify: BaseException | None = None
        self.fail_stop_notify: BaseException | None = None
        self.fail_disconnect: BaseException | None = None
        self.hang_writes = False
        self.hang_stop_notify = False
        self.on_write: Callable[[bytes], None] | None = None
        self.written = asyncio.Event()

    async def connect(self, disconnected_callback: DisconnectedCallback) -> None:
        self.connect_calls += 1
        if self.fail_connect is not None:
            raise self.fail_connect
        self.disconnected_callback = disconnected_callback
        self.linked = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.fail_disconnect is not None:
            raise self.fail_disconnect
        if self.linked:
            self.drop()

    async def start_notify(
        self, characteristic: str, callback: NotificationCallback
    ) -> None:
        if self.fail_start_notify is not None:
            raise self.fail_start_notify
        self.notify_characteristics.append(characteristic)
        self.callback = callback

    async def stop_notify(self, characteristic: str) -> None:
        self.stop_notify_calls += 1
        if self.hang_stop_notify:
            await asyncio.Event().wait()
        if self.fail_stop_notify is not None:
            raise self.fail_stop_notify

    async def write_gatt_char(
        self, characteristic: str, data: bytes, response: bool = False
    ) -> None:
        if self.hang_writes:
            await asyncio.Event().wait()
        if self.fail_writes:
            raise OSError("write failed")
        self.writes.append(data)
        self.write_characteristics.append(characteristic)
        self.write_times.append(asyncio.get_running_loop().time())
        self.responses.append(response)
        self.written.set()
        if self.on_write is not None:
            self.on_write(data)
        if data == device_info_query():
            for reply in self.replies:
                self.notify(reply)

    def notify(self, data: bytes) -> None:
        assert self.callback is not None
        self.callback(NOTIFY_CHARACTERISTIC_UUID, data)

    def drop(self) -> None:
        """Simulate a link loss reported by the BLE stack."""
        self.linked = False
        assert self.disconnected_callback is not None
        self.disconnected_callback()


def make_client(
    transport: FakeTransport,
    *,
    response_timeout_seconds: float = 1.0,
    keepalive_seconds: float = 60.0,
    write_spacing_seconds: float = 0.0,
    ramp_interval_seconds: float = 0.15,
    update_callback: UpdateCallback | None = None,
    connection_lost_callback: ConnectionLostCallback | None = None,
    allow_control: bool = True,
) -> EfcClient:
    return EfcClient(
        transport,
        response_timeout_seconds=response_timeout_seconds,
        keepalive_seconds=keepalive_seconds,
        write_spacing_seconds=write_spacing_seconds,
        ramp_interval_seconds=ramp_interval_seconds,
        update_callback=update_callback,
        connection_lost_callback=connection_lost_callback,
        allow_control=allow_control,
    )


async def wait_for_writes(transport: FakeTransport, count: int) -> None:
    """Wait until the transport recorded ``count`` writes."""
    async with asyncio.timeout(WAIT_SECONDS):
        while len(transport.writes) < count:
            transport.written.clear()
            await transport.written.wait()


async def drain(client: EfcClient) -> None:
    """Wait until the client processed every queued notification."""
    async with asyncio.timeout(WAIT_SECONDS):
        await client._frames.join()


async def wait_for(event: asyncio.Event) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        await event.wait()
