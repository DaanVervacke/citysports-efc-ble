"""Async client for one EFC treadmill.

``EfcClient`` connects through a ``BleTransport``, decodes the
notifications into an ``EfcState`` and sends the control commands. When a
session fails, a background task tears it down and then calls
``connection_lost_callback``.
"""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self, TypeAlias

from bleak.exc import BleakError

from .const import (
    DEFAULT_KEEPALIVE_SECONDS,
    DEFAULT_RAMP_INTERVAL_SECONDS,
    DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    DEFAULT_WRITE_SPACING_SECONDS,
    MIN_RAMP_INTERVAL_SECONDS,
    NOTIFY_CHARACTERISTIC_UUID,
    WRITE_CHARACTERISTIC_UUID,
)
from .counters import CounterTracker
from .exceptions import (
    EfcConnectionError,
    EfcControlDisabledError,
    EfcError,
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
from .transport_types import BleTransport as BleTransport  # noqa: PLC0414
from .transport_types import (
    DisconnectedCallback as DisconnectedCallback,  # noqa: PLC0414
)
from .transport_types import (
    NotificationCallback as NotificationCallback,  # noqa: PLC0414
)

__all__ = ["ConnectionLostCallback", "EfcClient", "UpdateCallback"]

UpdateCallback: TypeAlias = Callable[[EfcUpdate], Awaitable[None] | None]  # noqa: UP040
"""Sync or async function called with each ``EfcUpdate``.

It runs in the notification worker and must return quickly.
"""
ConnectionLostCallback: TypeAlias = Callable[[Exception], Awaitable[None] | None]  # noqa: UP040
"""Sync or async function called with the exception that ended a session."""

_LOGGER = logging.getLogger(__name__)
_RESET_STATES = frozenset((WorkoutState.IDLE, WorkoutState.STANDBY))
_TRANSPORT_ERRORS = (OSError, BleakError, TimeoutError, EfcError)


def _check_timings(
    response_timeout_seconds: float,
    keepalive_seconds: float,
    write_spacing_seconds: float,
    ramp_interval_seconds: float,
) -> None:
    timings = {
        "response_timeout_seconds": response_timeout_seconds,
        "keepalive_seconds": keepalive_seconds,
        "write_spacing_seconds": write_spacing_seconds,
        "ramp_interval_seconds": ramp_interval_seconds,
    }
    for name, value in timings.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EfcValidationError(f"{name} must be a number")
        if not math.isfinite(value):
            raise EfcValidationError(f"{name} must be finite")
    if response_timeout_seconds <= 0:
        raise EfcValidationError("response_timeout_seconds must be above 0")
    if keepalive_seconds <= 0:
        raise EfcValidationError("keepalive_seconds must be above 0")
    if write_spacing_seconds < 0:
        raise EfcValidationError("write_spacing_seconds must not be negative")
    if ramp_interval_seconds < MIN_RAMP_INTERVAL_SECONDS:
        raise EfcValidationError(
            f"ramp_interval_seconds must be at least {MIN_RAMP_INTERVAL_SECONDS}"
        )


class EfcClient:
    """Communicate with one EFC treadmill."""

    def __init__(
        self,
        transport: BleTransport,
        *,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
        write_spacing_seconds: float = DEFAULT_WRITE_SPACING_SECONDS,
        ramp_interval_seconds: float = DEFAULT_RAMP_INTERVAL_SECONDS,
        update_callback: UpdateCallback | None = None,
        connection_lost_callback: ConnectionLostCallback | None = None,
        allow_control: bool = False,
    ) -> None:
        """Initialize the client.

        Args:
            transport: BLE transport that owns the GATT connection.
            response_timeout_seconds: Seconds ``connect()`` waits for the
                device info and status frames. Every transport call is
                also limited to this time.
            keepalive_seconds: Interval between keepalive device info
                queries.
            write_spacing_seconds: Minimum seconds between two writes. The
                EQiSports app waits 150 ms.
            ramp_interval_seconds: Seconds between the 0.1 speed steps of
                a speed ramp. The minimum is 0.15.
            update_callback: Called, sync or async, with an ``EfcUpdate``
                for every valid frame. It must return quickly.
            connection_lost_callback: Called, sync or async, with the
                exception that ended a ready session. Not called for
                ``disconnect()`` or for a failed ``connect()``.
            allow_control: Enable the control methods.

        Raises:
            EfcValidationError: A timing is not a finite number,
                ``response_timeout_seconds`` or ``keepalive_seconds`` is
                not above 0, ``write_spacing_seconds`` is negative, or
                ``ramp_interval_seconds`` is below 0.15.
        """
        _check_timings(
            response_timeout_seconds,
            keepalive_seconds,
            write_spacing_seconds,
            ramp_interval_seconds,
        )
        self.transport = transport
        self.update_callback = update_callback
        self.connection_lost_callback = connection_lost_callback
        self.allow_control = allow_control
        self._response_timeout = response_timeout_seconds
        self._keepalive_seconds = keepalive_seconds
        self._write_spacing = write_spacing_seconds
        self._ramp_interval = ramp_interval_seconds
        self._state = EfcState()
        self._status = ConnectionStatus.DISCONNECTED
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._counters = CounterTracker()
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._ramp_task: asyncio.Task[None] | None = None
        self._connect_tasks: set[asyncio.Task[None]] = set()
        self._recovery_task: asyncio.Task[None] | None = None
        self._recovering = False
        self._ready: asyncio.Future[None] | None = None
        self._status_frame: StatusFrame | None = None
        self._session_failure: Exception | None = None
        self._last_write: float | None = None
        self._transport_open = False
        self._connecting = False
        self._close_requests = 0

    @property
    def state(self) -> EfcState:
        """The latest decoded treadmill state."""
        return self._state

    @property
    def status(self) -> ConnectionStatus:
        """The session status."""
        return self._status

    @property
    def connected(self) -> bool:
        """Whether the transport is connected, ready or not."""
        return self._status is not ConnectionStatus.DISCONNECTED

    @property
    def ready(self) -> bool:
        """Whether the session is ready for controls."""
        return self._status is ConnectionStatus.READY

    async def connect(self) -> None:
        """Connect and wait for the first device info and status frames.

        Connects the transport, subscribes to notifications, sends the
        device info query and waits until both a device info frame and a
        status frame arrived. Does nothing when already connected. Waits
        for the cleanup of a failed session first. The client can connect
        again after ``disconnect()`` or after a lost session.

        A failed attempt cleans up the transport and does not call
        ``connection_lost_callback``.

        Raises:
            EfcTimeoutError: The frames did not arrive within
                ``response_timeout_seconds``.
            EfcConnectionError: The transport failed, the link dropped
                while connecting, or ``disconnect()`` cancelled the
                attempt. Transport errors are chained as ``__cause__``.
        """
        task = asyncio.create_task(self._connect())
        self._connect_tasks.add(task)
        task.add_done_callback(self._connect_tasks.discard)
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise EfcConnectionError(
                "disconnect() cancelled the connection attempt"
            ) from None

    async def disconnect(self) -> None:
        """Disconnect and tear down the session.

        Cancels an in-flight ``connect()``, a running speed ramp and the
        keepalive, waits for the cleanup of a failed session, stops
        notifications and disconnects the transport. Errors during
        teardown are logged at debug level. Does nothing when already
        disconnected. ``connection_lost_callback`` is not called once
        this method returned.
        """
        self._close_requests += 1
        try:
            await self._cancel_connects()
            await self._wait_for_recovery()
            async with self._lifecycle_lock:
                await self._teardown()
        finally:
            self._close_requests -= 1

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
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or the write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(start_command(), "start")

    async def resume(self) -> None:
        """Resume a paused workout. Untested on real hardware.

        The treadmill uses the start command to resume.

        Raises:
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or the write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(start_command(), "resume")

    async def pause(self) -> None:
        """Pause the workout. Untested on real hardware.

        Raises:
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or the write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(pause_command(), "pause")

    async def stop(self) -> None:
        """Stop the workout and cancel any running speed ramp.

        The belt slows down by about 0.5 km/h per second while the
        treadmill shows the workout summary.

        Raises:
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or the write failed.
        """
        self._require_control()
        await self._cancel_ramp()
        await self._send(stop_command(), "stop")

    async def set_speed(self, kmh: float) -> None:
        """Set the belt speed.

        While the belt runs, the client ramps from the last reported speed
        in 0.1 steps, one every ``ramp_interval_seconds``, and returns
        once the target is written. A later ``set_speed()``, ``start()``,
        ``pause()``, ``resume()``, ``stop()`` or ``disconnect()`` cancels
        the ramp, and this call then returns without an error. The ramp
        also ends when the belt leaves the running state. When the belt
        is not running, the target is written once.

        Args:
            kmh: Target speed in km/h, within the range the treadmill
                reports. On imperial units the value is rounded to the
                nearest 0.1 mph.

        Raises:
            EfcValidationError: The value is not a number or is outside
                the reported range.
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or a write failed,
                also while ramping.
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
        status = self._require_status()
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
            self._raise_if_session_failed()
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
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or the write failed.
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
            EfcControlDisabledError: Controls are disabled.
            EfcNotReadyError: The session is not ready.
            EfcConnectionError: The session failed or the write failed.
        """
        self._require_control()
        await self._send(sport_record_query(), "sport record query")

    @property
    def _closing(self) -> bool:
        return self._close_requests > 0

    def _require_control(self) -> None:
        if not self.allow_control:
            raise EfcControlDisabledError("EFC controls are disabled for this session")
        self._raise_if_session_failed()
        if not self.ready:
            raise EfcNotReadyError("EFC controls are not ready")

    def _require_status(self) -> StatusFrame:
        status = self._status_frame
        if status is None:
            raise EfcNotReadyError("No status frame received yet")
        return status

    def _raise_if_session_failed(self) -> None:
        failure = self._session_failure
        if failure is not None:
            raise EfcConnectionError(f"The EFC session failed: {failure}") from failure

    async def _connect(self) -> None:
        while True:
            await self._wait_for_teardown()
            async with self._lifecycle_lock:
                if self._recovering:
                    continue
                if not self.connected:
                    await self._open_session()
                return

    async def _open_session(self) -> None:
        self._reset_session()
        ready = asyncio.get_running_loop().create_future()
        self._ready = ready
        self._connecting = True
        try:
            self._transport_open = True
            await self.transport.connect(self._on_disconnected)
            self._status = ConnectionStatus.CONNECTED
            self._frames = asyncio.Queue()
            self._worker = asyncio.create_task(self._consume(self._frames))
            async with asyncio.timeout(self._response_timeout):
                await self.transport.start_notify(
                    NOTIFY_CHARACTERISTIC_UUID, self._notification
                )
            await self._write(device_info_query())
            await self._wait_ready(ready)
            self._raise_if_session_failed()
            self._status = ConnectionStatus.READY
            self._keepalive_task = asyncio.create_task(self._keepalive())
        except BaseException as err:
            self._close_requests += 1
            try:
                await self._teardown()
            finally:
                self._close_requests -= 1
            if isinstance(err, Exception) and not isinstance(err, EfcError):
                raise EfcConnectionError("Connecting to the treadmill failed") from err
            raise
        finally:
            self._connecting = False

    async def _cancel_connects(self) -> None:
        tasks = [task for task in self._connect_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    async def _wait_for_teardown(self) -> None:
        task = self._recovery_task
        if self._recovering and task is not None:
            await asyncio.wait((task,))

    async def _wait_for_recovery(self) -> None:
        task = self._recovery_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            await asyncio.wait((task,))

    async def _ramp(self, start: int, target: int) -> None:
        step = 1 if target > start else -1
        values = list(range(start + step, target + step, step)) or [target]
        for index, value in enumerate(values):
            if index:
                await asyncio.sleep(self._ramp_interval)
                if self._state.workout_state is not WorkoutState.RUNNING:
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
            async with asyncio.timeout(self._response_timeout):
                await ready
        except TimeoutError as err:
            raise EfcTimeoutError(
                "Timed out waiting for the device info and status frames"
            ) from err

    async def _write(self, data: bytes) -> None:
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            if self._last_write is not None:
                delay = self._last_write + self._write_spacing - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(self._response_timeout):
                    await self.transport.write_gatt_char(
                        WRITE_CHARACTERISTIC_UUID, data, response=True
                    )
            finally:
                self._last_write = loop.time()

    async def _send(self, data: bytes, label: str) -> None:
        try:
            await self._write(data)
        except _TRANSPORT_ERRORS as err:
            self._fail(err)
            raise EfcConnectionError(
                f"Writing {label} to the treadmill failed"
            ) from err

    async def _keepalive(self) -> None:
        """Query the device info so a dead link shows up as a failed write."""
        while True:
            await asyncio.sleep(self._keepalive_seconds)
            try:
                await self._write(device_info_query())
            except _TRANSPORT_ERRORS as err:
                self._fail(err)
                return

    def _notification(self, _characteristic: str, payload: bytes) -> None:
        if self._worker is not None:
            self._frames.put_nowait(bytes(payload))

    async def _consume(self, queue: asyncio.Queue[bytes | None]) -> None:
        while (payload := await queue.get()) is not None:
            try:
                await self._process(payload)
            except Exception:
                _LOGGER.exception("EFC notification processing failed")
            finally:
                queue.task_done()
        queue.task_done()

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
        self._state = self._state.with_frame(frame)
        ready = self._ready
        if (
            ready is not None
            and not ready.done()
            and self._state.system_id is not None
            and self._state.status_code is not None
        ):
            ready.set_result(None)
        callback = self.update_callback
        if callback is not None:
            try:
                result = callback(EfcUpdate(self._state, self._status, frame, payload))
                if result is not None:
                    await result
            except Exception:
                _LOGGER.exception("EFC update callback failed")

    def _on_disconnected(self) -> None:
        self._fail(EfcConnectionError("The treadmill disconnected"))

    def _fail(self, error: Exception) -> None:
        if self._closing or self._session_failure is not None or not self.connected:
            return
        self._session_failure = error
        self._status = ConnectionStatus.DISCONNECTED
        if self._connecting:
            ready = self._ready
            if ready is not None and not ready.done():
                failure = EfcConnectionError("The session failed while connecting")
                failure.__cause__ = error
                ready.set_exception(failure)
            return
        _LOGGER.warning("EFC session failed: %s", error)
        self._recovering = True
        self._recovery_task = asyncio.get_running_loop().create_task(
            self._recover(error)
        )

    async def _recover(self, error: Exception) -> None:
        try:
            async with self._lifecycle_lock:
                await self._teardown()
        finally:
            self._recovering = False
        if self._closing:
            return
        callback = self.connection_lost_callback
        if callback is None:
            return
        try:
            result = callback(error)
            if result is not None:
                await result
        except Exception:
            _LOGGER.exception("EFC connection lost callback failed")

    async def _teardown(self) -> None:
        self._status = ConnectionStatus.DISCONNECTED
        keepalive = self._keepalive_task
        self._keepalive_task = None
        if keepalive is not None:
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)
        await self._cancel_ramp()
        if self._transport_open:
            await self._quietly(
                "stop_notify",
                lambda: self.transport.stop_notify(NOTIFY_CHARACTERISTIC_UUID),
            )
            await self._quietly("disconnect", self.transport.disconnect)
            self._transport_open = False
        await self._stop_worker()
        ready = self._ready
        self._ready = None
        if ready is not None:
            if not ready.done():
                ready.cancel()
            elif not ready.cancelled():
                ready.exception()

    async def _quietly(self, label: str, action: Callable[[], Awaitable[None]]) -> None:
        try:
            async with asyncio.timeout(self._response_timeout):
                await action()
        except Exception:
            _LOGGER.debug(
                "EFC transport %s failed during teardown", label, exc_info=True
            )

    async def _stop_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        self._frames.put_nowait(None)
        if worker is not asyncio.current_task():
            await asyncio.gather(worker, return_exceptions=True)

    def _reset_session(self) -> None:
        self._state = EfcState()
        self._counters.reset()
        self._status_frame = None
        self._session_failure = None
        self._last_write = None
