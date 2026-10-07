"""Typed EFC BLE client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from types import TracebackType
from typing import Protocol, Self

from .const import (
    DEFAULT_KEEPALIVE_SECONDS,
    DEFAULT_RAMP_INTERVAL,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_WRITE_SPACING,
    MIN_RAMP_INTERVAL,
    NOTIFY_CHARACTERISTIC_UUID,
    WRITE_CHARACTERISTIC_UUID,
)
from .counters import CounterTracker
from .exceptions import (
    EfcConnectionError,
    EfcNotReadyError,
    EfcProtocolError,
    EfcTimeoutError,
    EfcValidationError,
)
from .models import (
    ConnectionStatus,
    CountersFrame,
    EfcState,
    EfcUpdate,
    StatusFrame,
    WorkoutState,
)
from .protocol import (
    device_info_query,
    incline_command,
    kmh_to_speed,
    parse_frame,
    pause_command,
    speed_command,
    sport_record_query,
    start_command,
    stop_command,
)

NotificationCallback = Callable[[str, bytes], Awaitable[None] | None]
"""Transport callback taking the characteristic UUID and the raw payload."""
DisconnectedCallback = Callable[[], None]
"""Transport callback for a link that dropped without ``disconnect()``."""
UpdateCallback = Callable[[EfcUpdate], Awaitable[None]]
"""Coroutine function awaited with each ``EfcUpdate``."""
ConnectionLostCallback = Callable[[Exception], Awaitable[None] | None]
"""Sync or async function called with the exception that ended a session."""
_LOGGER = logging.getLogger(__name__)
_RESET_STATES = frozenset((WorkoutState.IDLE, WorkoutState.STANDBY))


class BleTransport(Protocol):
    """Minimal BLE transport supplied by the caller."""

    async def connect(self, disconnected_callback: DisconnectedCallback) -> None:
        """Establish the GATT connection.

        The transport calls ``disconnected_callback`` when the link drops.
        """

    async def disconnect(self) -> None:
        """Tear down the GATT connection."""

    async def start_notify(
        self, characteristic: str, callback: NotificationCallback
    ) -> None:
        """Subscribe to notifications on ``characteristic``."""

    async def stop_notify(self, characteristic: str) -> None:
        """Unsubscribe from notifications on ``characteristic``."""

    async def write_gatt_char(
        self, characteristic: str, data: bytes, response: bool = False
    ) -> None:
        """Write ``data`` to ``characteristic``."""


class EfcClient:
    """Communicate with one EFC treadmill."""

    def __init__(
        self,
        transport: BleTransport,
        *,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
        keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
        write_spacing: float = DEFAULT_WRITE_SPACING,
        ramp_interval: float = DEFAULT_RAMP_INTERVAL,
        update_callback: UpdateCallback | None = None,
        connection_lost_callback: ConnectionLostCallback | None = None,
        allow_control: bool = False,
    ) -> None:
        """Initialize the client.

        Args:
            transport: BLE transport that owns the GATT connection.
            response_timeout: Seconds ``connect()`` waits for the device
                info and status frames.
            keepalive_seconds: Interval between keepalive device info
                queries.
            write_spacing: Minimum seconds between two writes. The
                EQiSports app waits 150 ms.
            ramp_interval: Seconds between the 0.1 speed steps of a speed
                ramp. The minimum is 0.15.
            update_callback: Coroutine function awaited with an
                ``EfcUpdate`` for every valid frame.
            connection_lost_callback: Called, sync or async, with the
                exception that ended a session. Not called for
                ``disconnect()``.
            allow_control: Enable the control methods.

        Raises:
            EfcValidationError: ``ramp_interval`` is below 0.15 seconds.
        """
        if ramp_interval < MIN_RAMP_INTERVAL:
            raise EfcValidationError(
                f"ramp_interval must be at least {MIN_RAMP_INTERVAL} seconds"
            )
        self.transport = transport
        self.response_timeout = response_timeout
        self.keepalive_seconds = keepalive_seconds
        self.write_spacing = write_spacing
        self.ramp_interval = ramp_interval
        self.update_callback = update_callback
        self.connection_lost_callback = connection_lost_callback
        self.allow_control = allow_control
        self.state = EfcState()
        self.status = ConnectionStatus.DISCONNECTED
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._counters = CounterTracker()
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._ramp_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._ready: asyncio.Future[None] | None = None
        self._status_frame: StatusFrame | None = None
        self._session_failure: Exception | None = None
        self._last_write: float | None = None
        self._cleanup_done = False
        self._closing = False

    @property
    def connected(self) -> bool:
        """Whether the transport is connected, ready or not."""
        return self.status is not ConnectionStatus.DISCONNECTED

    @property
    def ready(self) -> bool:
        """Whether the session is ready for controls."""
        return self.status is ConnectionStatus.READY

    async def connect(self) -> None:
        """Connect and wait for the first device info and status frames.

        Connects the transport, subscribes to notifications, sends the
        device info query and waits until both a device info frame and a
        status frame arrived. A no-op when already connected. The client
        can connect again after ``disconnect()``.

        Errors raised by the transport while connecting propagate
        unchanged.

        Raises:
            EfcTimeoutError: The frames did not arrive within
                ``response_timeout``.
            EfcConnectionError: The link dropped while connecting.
        """
        async with self._lifecycle_lock:
            if self.connected:
                return
            self._reset_session()
            self._ready = asyncio.get_running_loop().create_future()
            try:
                await self.transport.connect(self._on_disconnected)
                self.status = ConnectionStatus.CONNECTED
                self._frames = asyncio.Queue()
                self._worker = asyncio.create_task(self._consume(self._frames))
                await self.transport.start_notify(
                    NOTIFY_CHARACTERISTIC_UUID, self._notification
                )
                await self._write(device_info_query())
                await self._wait_ready(self._ready)
                self.status = ConnectionStatus.READY
                self._keepalive_task = asyncio.create_task(self._keepalive())
            except BaseException:
                await self._teardown()
                raise

    async def disconnect(self) -> None:
        """Disconnect and tear down the session.

        Cancels a running speed ramp and the keepalive, stops
        notifications and disconnects the transport. Errors during
        teardown are suppressed. A no-op when already disconnected.
        """
        async with self._lifecycle_lock:
            self._closing = True
            try:
                await self._teardown()
            finally:
                self._closing = False

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def start(self) -> None:
        """Start a workout. The belt moves after a countdown of about 3 s.

        Raises:
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: The write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(start_command(), "start")

    async def resume(self) -> None:
        """Resume a paused workout. Untested on real hardware.

        The treadmill uses the start command to resume.

        Raises:
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: The write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(start_command(), "resume")

    async def pause(self) -> None:
        """Pause the workout. Untested on real hardware.

        Raises:
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: The write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(pause_command(), "pause")

    async def stop(self) -> None:
        """Stop the workout and cancel any running speed ramp.

        The belt slows down by about 0.5 km/h per second while the
        treadmill shows the workout summary.

        Raises:
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: The write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(stop_command(), "stop")

    async def set_speed(self, kmh: float) -> None:
        """Set the belt speed.

        While the belt runs, the client ramps from the last reported speed
        in 0.1 steps, one every ``ramp_interval`` seconds, and returns once
        the target is written. A later ``set_speed()``, ``start()``,
        ``pause()``, ``resume()``, ``stop()`` or ``disconnect()`` cancels
        the ramp, and this call then returns without an error. The ramp
        also ends when the belt leaves the running state. When the belt
        is not running, the target is written once.

        Args:
            kmh: Target speed in km/h, within the range the treadmill
                reports. On imperial units the value is rounded to the
                nearest 0.1 mph.

        Raises:
            EfcValidationError: The value is outside the reported range.
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: A write failed.
        """
        self._require_control()
        status = self._require_status()
        target = kmh_to_speed(kmh, status.imperial)
        if not status.min_speed_raw <= target <= status.max_speed_raw:
            raise EfcValidationError(
                f"Speed must be between {status.min_speed_kmh:g} and "
                f"{status.max_speed_kmh:g} km/h"
            )
        await self._cancel_ramp()
        if status.workout_state is not WorkoutState.RUNNING:
            await self._send(speed_command(target), "speed")
            return
        task = asyncio.create_task(self._ramp(status.speed_raw, target))
        self._ramp_task = task
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                task.cancel()
                raise
        finally:
            if self._ramp_task is task:
                self._ramp_task = None

    async def set_incline(self, percent: int) -> None:
        """Set the incline. Untested on real hardware.

        Args:
            percent: Whole percent value within the range the treadmill
                reports.

        Raises:
            EfcValidationError: The treadmill has no incline, or the value
                is outside the reported range.
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: The write failed.
        """
        self._require_control()
        status = self._require_status()
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise EfcValidationError("Incline must be a whole percent value")
        if status.max_incline_percent == 0:
            raise EfcValidationError("This treadmill does not support incline")
        if not status.min_incline_percent <= percent <= status.max_incline_percent:
            raise EfcValidationError(
                f"Incline must be between {status.min_incline_percent} and "
                f"{status.max_incline_percent} percent"
            )
        await self._send(incline_command(percent), "incline")

    async def request_sport_record(self) -> None:
        """Ask for the sport record frame. Untested on real hardware.

        The answer arrives as a ``SportRecordFrame`` update and sets
        ``state.workout_counter``.

        Raises:
            EfcNotReadyError: Controls are disabled or the session is not
                ready.
            EfcConnectionError: The write failed.
        """
        self._require_control()
        await self._send(sport_record_query(), "sport record query")

    def _require_control(self) -> None:
        if not self.allow_control:
            raise EfcNotReadyError("EFC controls are disabled for this session")
        if not self.ready:
            raise EfcNotReadyError("EFC controls are not ready")

    def _require_status(self) -> StatusFrame:
        status = self._status_frame
        if status is None:
            raise EfcNotReadyError("No status frame received yet")
        return status

    async def _ramp(self, start: int, target: int) -> None:
        step = 1 if target > start else -1
        values = list(range(start + step, target + step, step)) or [target]
        for index, value in enumerate(values):
            if index:
                await asyncio.sleep(self.ramp_interval)
                if self.state.workout_state is not WorkoutState.RUNNING:
                    return
            await self._send(speed_command(value), "speed")

    async def _cancel_ramp(self) -> None:
        task = self._ramp_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _wait_ready(self, ready: asyncio.Future[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(ready), self.response_timeout)
        except TimeoutError as err:
            raise EfcTimeoutError(
                "Timed out waiting for the device info and status frames"
            ) from err

    async def _write(self, data: bytes) -> None:
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            if self._last_write is not None:
                delay = self._last_write + self.write_spacing - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            try:
                await self.transport.write_gatt_char(
                    WRITE_CHARACTERISTIC_UUID, data, response=True
                )
            finally:
                self._last_write = loop.time()

    async def _send(self, data: bytes, label: str) -> None:
        self._raise_if_session_failed()
        try:
            await self._write(data)
        except Exception as err:
            await self._handle_session_failure(err)
            raise EfcConnectionError(
                f"Writing {label} to the treadmill failed"
            ) from err

    async def _keepalive(self) -> None:
        """Query the device info so a dead link shows up as a failed write."""
        try:
            while True:
                await asyncio.sleep(self.keepalive_seconds)
                await self._write(device_info_query())
        except Exception as err:  # noqa: BLE001
            await self._handle_session_failure(err)

    def _notification(self, _characteristic: str, payload: bytes) -> None:
        if self._worker is not None:
            self._frames.put_nowait(bytes(payload))

    async def _consume(self, queue: asyncio.Queue[bytes | None]) -> None:
        while (payload := await queue.get()) is not None:
            try:
                await self._process(payload)
            except Exception:
                _LOGGER.exception("EFC notification processing failed")

    async def _process(self, payload: bytes) -> None:
        try:
            frame = parse_frame(payload)
        except EfcProtocolError as err:
            _LOGGER.warning("Dropped EFC frame %s: %s", payload.hex(), err)
            return
        if isinstance(frame, StatusFrame):
            if frame.workout_state in _RESET_STATES:
                self._counters.reset()
            self._status_frame = frame
        elif isinstance(frame, CountersFrame):
            frame = self._counters.correct(frame)
        self.state = self.state.with_frame(frame)
        ready = self._ready
        if (
            ready is not None
            and not ready.done()
            and self.state.system_id is not None
            and self.state.status_code is not None
        ):
            ready.set_result(None)
        callback = self.update_callback
        if callback is not None:
            try:
                await callback(EfcUpdate(self.state, self.status, frame, payload))
            except Exception:
                _LOGGER.exception("EFC update callback failed")

    def _on_disconnected(self) -> None:
        if not self.connected or self._closing:
            return
        task = asyncio.get_running_loop().create_task(
            self._handle_session_failure(
                EfcConnectionError("The treadmill disconnected")
            )
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _handle_session_failure(self, error: Exception) -> None:
        if self._session_failure is not None or not self.connected:
            return
        self._session_failure = error
        self.status = ConnectionStatus.DISCONNECTED
        ready = self._ready
        if ready is not None and not ready.done():
            failure = EfcConnectionError("The session failed while connecting")
            failure.__cause__ = error
            ready.set_exception(failure)
        if self._closing:
            return
        current = asyncio.current_task()
        keepalive = self._keepalive_task
        if keepalive is not None and keepalive is not current:
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)
        self._keepalive_task = None
        await self._cancel_ramp()
        await self._cleanup_transport()
        await self._stop_worker()
        _LOGGER.warning("EFC session failed: %s", error)
        callback = self.connection_lost_callback
        if callback is None:
            return
        try:
            result = callback(error)
            if result is not None:
                await result
        except Exception:
            _LOGGER.exception("EFC connection lost callback failed")

    def _raise_if_session_failed(self) -> None:
        failure = self._session_failure
        if failure is not None:
            raise EfcConnectionError(f"The EFC session failed: {failure}") from failure

    async def _teardown(self) -> None:
        keepalive = self._keepalive_task
        self._keepalive_task = None
        if keepalive is not None:
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)
        await self._cancel_ramp()
        await self._cleanup_transport()
        await self._stop_worker()
        ready = self._ready
        self._ready = None
        if ready is not None and ready.done() and not ready.cancelled():
            ready.exception()
        self.status = ConnectionStatus.DISCONNECTED

    async def _cleanup_transport(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        with suppress(Exception):
            await self.transport.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
        with suppress(Exception):
            await self.transport.disconnect()

    async def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        self._frames.put_nowait(None)
        if worker is not asyncio.current_task():
            await asyncio.gather(worker, return_exceptions=True)

    def _reset_session(self) -> None:
        self.state = EfcState()
        self._counters.reset()
        self._status_frame = None
        self._session_failure = None
        self._last_write = None
        self._cleanup_done = False
