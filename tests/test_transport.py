import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache
from citysports_efc_ble import BleakTransport, EfcConnectionError
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
        self.fail_disconnect = False
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.disconnected_callback: Callable[[Any], None] | None = None

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
        if self.fail_disconnect:
            raise OSError("disconnect failed")

    def notify(self, data: bytes) -> None:
        assert self.notify_callback is not None
        self.notify_callback(FakeCharacteristic(), bytearray(data))

    def drop(self) -> None:
        assert self.disconnected_callback is not None
        self.disconnected_callback(self)


@pytest.fixture
def device() -> BLEDevice:
    return BLEDevice("AA:BB:CC:DD:EE:FF", "CITYSPORTS-LINKER", None)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeBleakClient:
    client = FakeBleakClient()

    async def establish(*args: Any, **kwargs: Any) -> FakeBleakClient:
        client.calls.append((args, kwargs))
        client.disconnected_callback = kwargs["disconnected_callback"]
        client.is_connected = True
        return client

    monkeypatch.setattr(transport_module, "establish_connection", establish)
    return client


async def test_client_needs_connection(device: BLEDevice) -> None:
    transport = BleakTransport(device)
    with pytest.raises(EfcConnectionError, match="not connected"):
        _ = transport.client
    with pytest.raises(EfcConnectionError, match="not connected"):
        await transport.write_gatt_char("w", b"\x01")


async def test_connect_passes_options(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    def lookup() -> BLEDevice:
        return device

    transport = BleakTransport(device, timeout=12.0, ble_device_callback=lookup)
    await transport.connect(lambda: None)
    args, kwargs = fake_client.calls[0]
    assert args == (BleakClientWithServiceCache, device, "CITYSPORTS-LINKER")
    assert kwargs["timeout"] == 12.0
    assert kwargs["ble_device_callback"] is lookup
    assert callable(kwargs["disconnected_callback"])


async def test_client_factory_overrides_class(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device, client_factory=BleakClient)
    await transport.connect(lambda: None)
    args, kwargs = fake_client.calls[0]
    assert args[0] is BleakClient
    assert kwargs["ble_device_callback"] is None


async def test_connect_uses_address_without_name(
    fake_client: FakeBleakClient,
) -> None:
    device = BLEDevice("AA:BB:CC:DD:EE:FF", None, None)
    await BleakTransport(device).connect(lambda: None)
    args, _kwargs = fake_client.calls[0]
    assert args[2] == "AA:BB:CC:DD:EE:FF"


async def test_connect_reuses_live_client(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    first: list[bool] = []
    second: list[bool] = []
    await transport.connect(lambda: first.append(True))
    await transport.connect(lambda: second.append(True))
    assert len(fake_client.calls) == 1
    current: object = transport.client
    assert current is fake_client
    fake_client.drop()
    assert first == []
    assert second == [True]


async def test_connect_again_when_link_is_down(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    fake_client.is_connected = False
    await transport.connect(lambda: None)
    assert len(fake_client.calls) == 2


async def test_write_stop_notify_and_disconnect(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    await transport.write_gatt_char("w", b"\x01", response=True)
    assert fake_client.writes == [("w", b"\x01", True)]
    await transport.stop_notify("n")
    assert fake_client.stopped == ["n"]
    await transport.disconnect()
    assert fake_client.disconnects == 1
    await transport.stop_notify("n")
    assert fake_client.stopped == ["n"]
    await transport.disconnect()
    assert fake_client.disconnects == 1


async def test_stop_notify_after_link_drop_does_nothing(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    fake_client.is_connected = False
    await transport.stop_notify("n")
    assert fake_client.stopped == []


async def test_disconnect_error_still_resets_client(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    fake_client.fail_disconnect = True
    with pytest.raises(OSError, match="disconnect failed"):
        await transport.disconnect()
    with pytest.raises(EfcConnectionError):
        _ = transport.client


async def test_sync_notification_callback(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    received: list[tuple[str, bytes]] = []

    def on_data(uuid: str, data: bytes) -> None:
        received.append((uuid, data))

    await transport.start_notify("n", on_data)
    fake_client.notify(b"\x1a")
    assert received == [(FakeCharacteristic.uuid, b"\x1a")]


async def test_async_notification_callback_runs(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    received = asyncio.Event()

    async def on_data(_uuid: str, _data: bytes) -> None:
        received.set()

    await transport.start_notify("n", on_data)
    fake_client.notify(b"\x1a")
    async with asyncio.timeout(1):
        await received.wait()


async def test_async_notification_errors_are_logged(
    device: BLEDevice,
    fake_client: FakeBleakClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    failed = asyncio.Event()

    async def failing(_uuid: str, _data: bytes) -> None:
        failed.set()
        raise RuntimeError("boom")

    await transport.start_notify("n", failing)
    with caplog.at_level(logging.ERROR):
        fake_client.notify(b"\x1a")
        async with asyncio.timeout(1):
            await failed.wait()
        await asyncio.sleep(0)
    assert "EFC notification callback failed" in caplog.text


async def test_disconnect_cancels_slow_notification_task(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    started = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []

    async def slow(_uuid: str, _data: bytes) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        started.set()
        await asyncio.Event().wait()

    await transport.start_notify("n", slow)
    fake_client.notify(b"\x1a")
    async with asyncio.timeout(1):
        await started.wait()
    await transport.disconnect()
    assert tasks[0].cancelled()


async def test_notifications_after_disconnect_are_dropped(
    device: BLEDevice, fake_client: FakeBleakClient
) -> None:
    transport = BleakTransport(device)
    await transport.connect(lambda: None)
    received: list[bytes] = []
    await transport.start_notify("n", lambda _uuid, data: received.append(data))
    await transport.disconnect()
    fake_client.notify(b"\x1a")
    assert received == []
