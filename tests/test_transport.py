from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from citysports_efc_ble import BleakTransport
from citysports_efc_ble import transport as transport_module


class FakeCharacteristic:
    uuid = "ffeeddcc-bbaa-9988-7766-554433221102"


class FakeBleakClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.notify_callback: Callable[[Any, bytearray], None] | None = None
        self.writes: list[tuple[str, bytes, bool]] = []
        self.stopped: list[str] = []
        self.disconnects = 0

    async def start_notify(
        self, characteristic: str, callback: Callable[[Any, bytearray], None]
    ) -> None:
        self.notify_callback = callback

    async def stop_notify(self, characteristic: str) -> None:
        self.stopped.append(characteristic)

    async def write_gatt_char(
        self, characteristic: str, data: bytes, response: bool = False
    ) -> None:
        self.writes.append((characteristic, data, response))

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False


@pytest.fixture
def device() -> BLEDevice:
    return BLEDevice("AA:BB:CC:DD:EE:FF", "CITYSPORTS-LINKER", None)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeBleakClient:
    client = FakeBleakClient()
    calls: list[dict[str, Any]] = []

    async def establish(*args: Any, **kwargs: Any) -> FakeBleakClient:
        calls.append(kwargs)
        client.disconnected_callback = kwargs["disconnected_callback"]  # type: ignore[attr-defined]
        return client

    monkeypatch.setattr(transport_module, "establish_connection", establish)
    client.calls = calls  # type: ignore[attr-defined]
    return client


async def test_connect_write_and_disconnect(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device, client_factory=lambda *_a, **_k: fake_client)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="not connected"):
        _ = transport.client
    dropped: list[bool] = []
    await transport.connect(lambda: dropped.append(True))
    await transport.connect(lambda: dropped.append(True))
    assert len(fake_client.calls) == 1  # type: ignore[attr-defined]
    fake_client.disconnected_callback(fake_client)  # type: ignore[attr-defined]
    assert dropped == [True]
    await transport.write_gatt_char("w", b"\x01", response=True)
    assert fake_client.writes == [("w", b"\x01", True)]
    await transport.stop_notify("n")
    assert fake_client.stopped == ["n"]
    await transport.disconnect()
    assert fake_client.disconnects == 1
    await transport.stop_notify("n")
    assert fake_client.stopped == ["n"]
    await transport.disconnect()


async def test_notifications_sync_and_async(
    device: BLEDevice,
    fake_client: FakeBleakClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    received: list[tuple[str, bytes]] = []

    def sync_callback(uuid: str, data: bytes) -> None:
        received.append((uuid, data))

    await transport.start_notify("n", sync_callback)
    assert fake_client.notify_callback is not None
    fake_client.notify_callback(FakeCharacteristic(), bytearray(b"\x1a"))
    assert received == [(FakeCharacteristic.uuid, b"\x1a")]

    async def failing(_uuid: str, _data: bytes) -> None:
        raise RuntimeError("boom")

    await transport.start_notify("n", failing)
    with caplog.at_level(logging.ERROR):
        fake_client.notify_callback(FakeCharacteristic(), bytearray(b"\x1a"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert "EFC notification callback failed" in caplog.text

    async def ok(_uuid: str, _data: bytes) -> None:
        return None

    await transport.start_notify("n", ok)
    fake_client.notify_callback(FakeCharacteristic(), bytearray(b"\x1a"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    blocker = asyncio.Event()

    async def slow(_uuid: str, _data: bytes) -> None:
        await blocker.wait()

    await transport.start_notify("n", slow)
    fake_client.notify_callback(FakeCharacteristic(), bytearray(b"\x1a"))
    await asyncio.sleep(0)
    await transport.disconnect()
    fake_client.notify_callback(FakeCharacteristic(), bytearray(b"\x1a"))
    assert not transport._notification_tasks
