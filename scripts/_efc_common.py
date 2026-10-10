"""Shared helpers for the EFC developer scripts."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TextIO
from urllib.parse import urlsplit

import habluetooth
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak_esphome import APIConnectionManager, ESPHomeDeviceConfig
from citysports_efc_ble import (
    BleTransport,
    DeviceInfoFrame,
    DisconnectedCallback,
    EfcProtocolError,
    EfcState,
    NotificationCallback,
    is_efc_advertisement,
    parse_frame,
)
from citysports_efc_ble.protocol import xor_checksum
from habluetooth import BluetoothScanningMode, BluetoothServiceInfoBleak

_LOGGER = logging.getLogger(__name__)

FAKE_SYSTEM_ID = bytes.fromhex("665544332211")
SYSTEM_ID_OFFSET = 9
DISCOVERY_WARMUP_SECONDS = 5.0
DISCOVERY_POLL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class Sighting:
    """One advertisement seen during a scan."""

    device: BLEDevice
    name: str | None
    rssi: int | None
    service_uuids: tuple[str, ...]

    @property
    def is_efc(self) -> bool:
        """Whether the advertisement looks like an EFC treadmill."""
        return is_efc_advertisement(self.name, self.service_uuids)


def redact_frame(data: bytes) -> bytes:
    """Replace the system id of a device info frame with a fixed fake value.

    Other frames and frames that do not decode are returned unchanged.
    """
    try:
        frame = parse_frame(data)
    except EfcProtocolError:
        return data
    if not isinstance(frame, DeviceInfoFrame):
        return data
    system_id_end = SYSTEM_ID_OFFSET + len(FAKE_SYSTEM_ID)
    body = data[:SYSTEM_ID_OFFSET] + FAKE_SYSTEM_ID + data[system_id_end:-1]
    return body + bytes((xor_checksum(body),))


def state_record(state: EfcState) -> dict[str, object]:
    """Return the state as a JSON friendly dict with the system id redacted."""
    record: dict[str, object] = {}
    for field in fields(state):
        value = getattr(state, field.name)
        if isinstance(value, Enum):
            value = value.name
        record[field.name] = value
    if record["system_id"] is not None:
        record["system_id"] = "11:22:33:44:55:66"
    return record


class CaptureWriter:
    """Write newline-delimited JSON frame records."""

    def __init__(self, path: Path | None) -> None:
        """Open ``path`` for appending, or capture nothing when None."""
        self._file: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")
        self._start = time.monotonic()

    def __enter__(self) -> Self:
        """Return the writer itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the capture file."""
        self.close()

    def write(self, direction: str, payload: bytes) -> None:
        """Log and store one redacted frame."""
        record = {
            "t": round(time.monotonic() - self._start, 3),
            "dir": direction,
            "hex": redact_frame(payload).hex(),
        }
        _LOGGER.info("%s %s", direction, record["hex"])
        if self._file is not None:
            self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._file.flush()

    def close(self) -> None:
        """Close the capture file. Does nothing when no path was given."""
        if self._file is not None:
            self._file.close()
            self._file = None


class CaptureTransport(BleTransport):
    """Capture transport traffic while delegating to a BLE transport."""

    def __init__(self, transport: BleTransport, capture: CaptureWriter) -> None:
        """Wrap ``transport`` and record every frame in ``capture``."""
        self._transport = transport
        self._capture = capture

    async def connect(self, disconnected_callback: DisconnectedCallback) -> None:
        """Connect the wrapped transport."""
        await self._transport.connect(disconnected_callback)

    async def disconnect(self) -> None:
        """Disconnect the wrapped transport."""
        await self._transport.disconnect()

    async def start_notify(
        self, characteristic: str, callback: NotificationCallback
    ) -> None:
        """Subscribe and record every notification."""

        def on_notification(received: str, payload: bytes) -> Awaitable[None] | None:
            self._capture.write("rx", payload)
            return callback(received, payload)

        await self._transport.start_notify(characteristic, on_notification)

    async def stop_notify(self, characteristic: str) -> None:
        """Unsubscribe on the wrapped transport."""
        await self._transport.stop_notify(characteristic)

    async def write_gatt_char(
        self, characteristic: str, data: bytes, response: bool = False
    ) -> None:
        """Record and forward one write."""
        self._capture.write("tx", data)
        await self._transport.write_gatt_char(characteristic, data, response=response)


class DiscoveryBluetoothManager(habluetooth.BluetoothManager):
    """Bluetooth manager that keeps scanner-owned advertisement data."""

    def _discover_service_info(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Skip the callback dispatch because the scripts read the scanners directly."""


def proxy_host(value: str) -> str:
    """Return the host portion accepted by bleak-esphome."""
    parsed = urlsplit(value if "://" in value else f"//{value}")
    return parsed.hostname or value.removeprefix("//")


Scanner = Callable[[float], Awaitable[list[Sighting]]]
"""Coroutine function that scans for at least the given seconds."""


async def scan_local(seconds: float) -> list[Sighting]:
    """Scan with the local Bluetooth adapter."""
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    return [
        Sighting(
            device,
            advertisement.local_name or device.name,
            advertisement.rssi,
            tuple(advertisement.service_uuids),
        )
        for device, advertisement in found.values()
    ]


def _proxy_scanner(manager: habluetooth.BluetoothManager) -> Scanner:
    async def scan(seconds: float) -> list[Sighting]:
        await asyncio.sleep(min(seconds, DISCOVERY_WARMUP_SECONDS))
        seen: dict[str, Sighting] = {}
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            for scanner in manager.async_current_scanners():
                discovered = scanner.discovered_devices_and_advertisement_data
                for device, advertisement in discovered.values():
                    seen[device.address] = Sighting(
                        device,
                        advertisement.local_name or device.name,
                        advertisement.rssi,
                        tuple(advertisement.service_uuids),
                    )
            if asyncio.get_running_loop().time() >= deadline:
                return list(seen.values())
            await asyncio.sleep(DISCOVERY_POLL_SECONDS)

    return scan


@asynccontextmanager
async def open_scanner(
    proxy: str | None, noise_psk: str | None
) -> AsyncIterator[Scanner]:
    """Yield a scanner for the local adapter, or for an ESPHome proxy."""
    if proxy is None:
        yield scan_local
        return
    manager = APIConnectionManager(
        ESPHomeDeviceConfig(address=proxy_host(proxy), noise_psk=noise_psk)
    )
    bluetooth_manager = DiscoveryBluetoothManager()
    try:
        await bluetooth_manager.async_setup()
        await manager.start()
        for scanner in bluetooth_manager.async_current_scanners():
            if getattr(scanner, "connectable", False):
                scanner.set_requested_mode(BluetoothScanningMode.ACTIVE)
        yield _proxy_scanner(bluetooth_manager)
    finally:
        try:
            await manager.stop()
        except Exception:
            _LOGGER.debug("Stopping the ESPHome connection failed", exc_info=True)
        try:
            bluetooth_manager.async_stop()
        except Exception:
            _LOGGER.debug("Stopping the Bluetooth manager failed", exc_info=True)


async def find_treadmill(
    scan: Scanner, address: str | None, seconds: float
) -> BLEDevice:
    """Return the treadmill with ``address``, or the first EFC treadmill seen.

    Raises:
        TimeoutError: No matching device was seen.
    """
    for sighting in await scan(seconds):
        if address is not None:
            if sighting.device.address.upper() == address.upper():
                return sighting.device
        elif sighting.is_efc:
            return sighting.device
    raise TimeoutError("EFC treadmill was not found")


def load_config(path: Path) -> dict[str, Any]:
    """Load the optional JSON config file, or an empty dict when missing.

    Raises:
        ValueError: The file is not valid JSON or does not hold a JSON object.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"{path} is not valid JSON: {err}") from err
    if isinstance(data, dict):
        return data
    raise ValueError(f"{path} must contain a JSON object")
