"""Bleak based transport for EFC treadmills."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import DEFAULT_CONNECT_TIMEOUT_SECONDS
from .exceptions import EfcConnectionError
from .transport_types import BleTransport, DisconnectedCallback, NotificationCallback

__all__ = ["BleakTransport"]

_LOGGER = logging.getLogger(__name__)


class BleakTransport(BleTransport):
    """Adapt a Bleak client to the EFC transport protocol."""

    def __init__(
        self,
        device: BLEDevice,
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        client_factory: type[BleakClient] | None = None,
        ble_device_callback: Callable[[], BLEDevice] | None = None,
    ) -> None:
        """Initialize the transport.

        Args:
            device: The BLE device to connect to.
            timeout: Connection timeout in seconds.
            client_factory: The Bleak client class. Defaults to
                ``BleakClientWithServiceCache``.
            ble_device_callback: Returns the latest ``BLEDevice`` for
                connection retries. Home Assistant passes a lookup in its
                Bluetooth manager here.
        """
        self.device = device
        self.timeout = timeout
        self._client_class: type[BleakClient] = (
            client_factory
            if client_factory is not None
            else BleakClientWithServiceCache
        )
        self._ble_device_callback = ble_device_callback
        self._client: BleakClient | None = None
        self._disconnected_callback: DisconnectedCallback | None = None
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._accept_notifications = True

    @property
    def client(self) -> BleakClient:
        """The connected Bleak client.

        Raises:
            EfcConnectionError: The transport is not connected.
        """
        if self._client is None:
            raise EfcConnectionError("EFC BLE transport is not connected")
        return self._client

    async def connect(self, disconnected_callback: DisconnectedCallback) -> None:
        """Connect to the device. A live connection is reused.

        Args:
            disconnected_callback: Called when Bleak reports that the link
                dropped. A reused connection reports to the latest
                callback.
        """
        self._accept_notifications = True
        self._disconnected_callback = disconnected_callback
        if self._client is None or not self._client.is_connected:
            self._client = await establish_connection(
                self._client_class,
                self.device,
                self.device.name or self.device.address,
                disconnected_callback=self._on_disconnect,
                ble_device_callback=self._ble_device_callback,
                timeout=self.timeout,
            )

    async def disconnect(self) -> None:
        """Disconnect and cancel any in-flight notification callbacks."""
        self._accept_notifications = False
        await self._cancel_notification_tasks()
        client = self._client
        try:
            if client is not None:
                await client.disconnect()
        finally:
            self._client = None

    async def start_notify(
        self, characteristic: str, callback: NotificationCallback
    ) -> None:
        """Call ``callback`` with every notification on ``characteristic``.

        A callback that returns an awaitable runs as a task. ``disconnect()``
        cancels those tasks, and errors they raise are logged.

        Raises:
            EfcConnectionError: The transport is not connected.
        """

        def on_notification(
            gatt_characteristic: BleakGATTCharacteristic, payload: bytearray
        ) -> None:
            if not self._accept_notifications:
                return
            result = callback(gatt_characteristic.uuid, bytes(payload))
            if isinstance(result, Awaitable):
                task = asyncio.ensure_future(result)
                self._notification_tasks.add(task)
                task.add_done_callback(self._notification_task_done)

        await self.client.start_notify(characteristic, on_notification)

    async def stop_notify(self, characteristic: str) -> None:
        """Unsubscribe from notifications.

        Does nothing when the link already dropped.
        """
        if self._client is not None and self._client.is_connected:
            await self._client.stop_notify(characteristic)

    async def write_gatt_char(
        self, characteristic: str, data: bytes, response: bool = False
    ) -> None:
        """Write ``data`` to ``characteristic``, with or without response.

        Raises:
            EfcConnectionError: The transport is not connected.
        """
        await self.client.write_gatt_char(characteristic, data, response=response)

    def _on_disconnect(self, _client: BleakClient) -> None:
        callback = self._disconnected_callback
        if callback is not None:
            callback()

    def _notification_task_done(self, task: asyncio.Task[None]) -> None:
        self._notification_tasks.discard(task)
        if task.cancelled():
            return
        with suppress(asyncio.CancelledError):
            error = task.exception()
        if error is not None:
            _LOGGER.error("EFC notification callback failed", exc_info=error)

    async def _cancel_notification_tasks(self) -> None:
        tasks = tuple(self._notification_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
