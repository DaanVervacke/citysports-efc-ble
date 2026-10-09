import asyncio
import logging
import math
from typing import Any

import pytest
from citysports_efc_ble import (
    ConnectionStatus,
    EfcClient,
    EfcConnectionError,
    EfcControlDisabledError,
    EfcNotReadyError,
    EfcTimeoutError,
    EfcUpdate,
    EfcValidationError,
    StatusFrame,
    WorkoutState,
)
from citysports_efc_ble.const import (
    NOTIFY_CHARACTERISTIC_UUID,
    WRITE_CHARACTERISTIC_UUID,
)
from citysports_efc_ble.protocol import (
    device_info_query,
    incline_command,
    pause_command,
    speed_command,
    sport_record_query,
    start_command,
    stop_command,
)
from citysports_efc_ble.transport_types import BleTransport

from .helpers import (
    DEVICE_INFO,
    WAIT_SECONDS,
    FakeTransport,
    counters,
    drain,
    frame,
    load_capture,
    make_client,
    status,
    wait_for,
    wait_for_writes,
)


async def running(client: EfcClient, transport: FakeTransport, speed: int) -> None:
    transport.notify(status(speed=speed, code=2))
    await drain(client)
    assert client.state.workout_state is WorkoutState.RUNNING


class LostRecorder:
    def __init__(self) -> None:
        self.errors: list[Exception] = []
        self.called = asyncio.Event()

    def __call__(self, error: Exception) -> None:
        self.errors.append(error)
        self.called.set()


def test_client_package_reexports_transport_types() -> None:
    assert BleTransport.__name__ == "BleTransport"


async def test_connect_reaches_ready(transport: FakeTransport) -> None:
    updates: list[EfcUpdate] = []

    async def on_update(update: EfcUpdate) -> None:
        updates.append(update)

    client = make_client(transport, update_callback=on_update)
    await client.connect()
    assert client.ready
    assert client.connected
    assert client.status is ConnectionStatus.READY
    assert transport.writes == [device_info_query()]
    assert transport.write_characteristics == [WRITE_CHARACTERISTIC_UUID]
    assert transport.notify_characteristics == [NOTIFY_CHARACTERISTIC_UUID]
    assert transport.responses == [True]
    assert client.state.system_id == "11:22:33:44:55:66"
    assert client.state.workout_state is WorkoutState.STANDBY
    assert [update.raw for update in updates][:1] == [DEVICE_INFO]
    await client.connect()
    assert transport.connect_calls == 1
    await client.disconnect()
    assert transport.disconnect_calls == 1
    assert transport.stop_notify_calls == 1
    assert client.status.value == "disconnected"


def test_new_client_is_disconnected(transport: FakeTransport) -> None:
    client = make_client(transport)
    assert not client.connected
    assert not client.ready
    assert client.status is ConnectionStatus.DISCONNECTED


async def test_sync_update_callback(transport: FakeTransport) -> None:
    updates: list[EfcUpdate] = []
    client = make_client(transport, update_callback=updates.append)
    await client.connect()
    assert [update.raw for update in updates][:1] == [DEVICE_INFO]
    await client.disconnect()


async def test_state_and_status_are_read_only(
    connected_client: EfcClient,
) -> None:
    with pytest.raises(AttributeError):
        connected_client.state = connected_client.state  # type: ignore[misc]
    with pytest.raises(AttributeError):
        connected_client.status = ConnectionStatus.READY  # type: ignore[misc]


async def test_context_manager(transport: FakeTransport) -> None:
    async with make_client(transport) as client:
        assert client.ready
    assert not client.connected


async def test_connect_times_out_without_status_frame() -> None:
    transport = FakeTransport(replies=[DEVICE_INFO])
    lost = LostRecorder()
    client = make_client(
        transport, response_timeout_seconds=0.05, connection_lost_callback=lost
    )
    with pytest.raises(EfcTimeoutError) as caught:
        await client.connect()
    assert isinstance(caught.value, TimeoutError)
    assert not client.connected
    assert transport.disconnect_calls == 1
    assert transport.stop_notify_calls == 1
    assert lost.errors == []


async def test_connect_wraps_transport_errors() -> None:
    transport = FakeTransport()
    transport.fail_connect = OSError("no adapter")
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    with pytest.raises(EfcConnectionError) as caught:
        await client.connect()
    assert isinstance(caught.value.__cause__, OSError)
    assert client.status is ConnectionStatus.DISCONNECTED
    assert lost.errors == []


async def test_start_notify_error_cleans_up() -> None:
    transport = FakeTransport()
    transport.fail_start_notify = OSError("notify failed")
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    with pytest.raises(EfcConnectionError) as caught:
        await client.connect()
    assert isinstance(caught.value.__cause__, OSError)
    assert transport.disconnect_calls == 1
    assert not client.connected
    assert lost.errors == []


async def test_link_drop_while_connecting_fails_connect() -> None:
    transport = FakeTransport(replies=[])
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    transport.on_write = lambda _data: transport.drop()
    with pytest.raises(EfcConnectionError, match="while connecting") as caught:
        await client.connect()
    assert isinstance(caught.value.__cause__, EfcConnectionError)
    assert lost.errors == []
    assert not client.connected
    assert transport.disconnect_calls == 1


async def test_link_drop_after_ready_frame_fails_connect(
    transport: FakeTransport,
) -> None:
    lost = LostRecorder()

    def on_update(update: EfcUpdate) -> None:
        if isinstance(update.frame, StatusFrame):
            transport.drop()

    client = make_client(
        transport, update_callback=on_update, connection_lost_callback=lost
    )
    with pytest.raises(EfcConnectionError, match="session failed"):
        await client.connect()
    assert lost.errors == []
    assert not client.connected


async def test_cancelled_connect_cleans_up() -> None:
    transport = FakeTransport(replies=[])
    lost = LostRecorder()
    client = make_client(
        transport, response_timeout_seconds=30.0, connection_lost_callback=lost
    )
    task = asyncio.create_task(client.connect())
    await wait_for_writes(transport, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.connected
    assert transport.stop_notify_calls == 1
    assert transport.disconnect_calls == 1
    assert lost.errors == []


async def test_disconnect_cancels_connect() -> None:
    transport = FakeTransport(replies=[])
    client = make_client(transport, response_timeout_seconds=30.0)
    task = asyncio.create_task(client.connect())
    await wait_for_writes(transport, 1)
    await client.disconnect()
    with pytest.raises(EfcConnectionError, match="cancelled"):
        await task
    assert not client.connected
    assert transport.disconnect_calls == 1


async def test_client_reconnects_with_fresh_state(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    transport.notify(counters(elapsed=10))
    await drain(client)
    assert client.state.elapsed_seconds == 10
    await client.disconnect()
    await client.connect()
    fresh = client.state
    assert fresh.elapsed_seconds is None
    await client.disconnect()


async def test_reconnect_after_link_drop(transport: FakeTransport) -> None:
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    await client.connect()
    transport.drop()
    await wait_for(lost.called)
    assert not client.connected
    await client.connect()
    assert client.ready
    assert transport.connect_calls == 2
    await client.disconnect()


async def test_connect_during_recovery_keeps_new_session(
    transport: FakeTransport,
) -> None:
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    await client.connect()
    transport.drop()
    await client.connect()
    assert client.ready
    assert len(lost.errors) == 1
    assert transport.disconnect_calls == 1
    assert transport.connect_calls == 2
    await asyncio.sleep(0)
    assert client.ready
    await client.disconnect()
    assert transport.disconnect_calls == 2


async def test_connect_after_cancelled_recovery_opens_new_session(
    transport: FakeTransport,
) -> None:
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    await client.connect()
    transport.hang_stop_notify = True
    transport.drop()
    recovery = client._recovery_task
    assert recovery is not None
    for _ in range(3):
        await asyncio.sleep(0)
    recovery.cancel()
    await asyncio.gather(recovery, return_exceptions=True)
    assert not client.connected
    transport.hang_stop_notify = False
    async with asyncio.timeout(WAIT_SECONDS):
        await client.connect()
    assert client.ready
    assert transport.connect_calls == 2
    assert lost.errors == []
    await client.disconnect()


async def test_connect_waiting_on_lock_lets_recovery_run_first(
    transport: FakeTransport,
) -> None:
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    await client.connect()
    lock = client._lifecycle_lock
    await lock.acquire()
    second = asyncio.create_task(client.connect())
    for _ in range(3):
        await asyncio.sleep(0)
    transport.drop()
    lock.release()
    await second
    assert client.ready
    assert len(lost.errors) == 1
    assert transport.connect_calls == 2
    await client.disconnect()


async def test_lost_callback_can_reconnect(transport: FakeTransport) -> None:
    client: EfcClient
    reconnected = asyncio.Event()

    async def on_lost(_error: Exception) -> None:
        await client.connect()
        reconnected.set()

    client = make_client(transport, connection_lost_callback=on_lost)
    await client.connect()
    transport.drop()
    await wait_for(reconnected)
    assert client.ready
    await client.disconnect()


async def test_lost_callback_can_disconnect(transport: FakeTransport) -> None:
    client: EfcClient
    done = asyncio.Event()

    async def on_lost(_error: Exception) -> None:
        await client.disconnect()
        done.set()

    client = make_client(transport, connection_lost_callback=on_lost)
    await client.connect()
    transport.drop()
    await wait_for(done)
    assert not client.connected


async def test_no_callback_after_disconnect_returned(
    transport: FakeTransport,
) -> None:
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    await client.connect()
    transport.drop()
    await client.disconnect()
    await asyncio.sleep(0)
    assert lost.errors == []
    assert transport.disconnect_calls == 1


async def test_disconnect_waits_for_running_callback(
    transport: FakeTransport,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[bool] = []

    async def on_lost(_error: Exception) -> None:
        started.set()
        await release.wait()
        finished.append(True)

    client = make_client(transport, connection_lost_callback=on_lost)
    await client.connect()
    transport.drop()
    await wait_for(started)
    closing = asyncio.create_task(client.disconnect())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert finished == [True]


async def test_second_link_drop_calls_callback_once(
    transport: FakeTransport,
) -> None:
    lost = LostRecorder()
    client = make_client(transport, connection_lost_callback=lost)
    await client.connect()
    transport.drop()
    transport.drop()
    await wait_for(lost.called)
    transport.drop()
    await client.disconnect()
    assert len(lost.errors) == 1


async def test_link_drop_logs_callback_errors(
    transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    called = asyncio.Event()

    def broken(_error: Exception) -> None:
        called.set()
        raise RuntimeError("boom")

    client = make_client(transport, connection_lost_callback=broken)
    await client.connect()
    with caplog.at_level(logging.WARNING):
        transport.drop()
        await wait_for(called)
        await client.disconnect()
    assert "EFC session failed" in caplog.text
    assert "connection lost callback failed" in caplog.text


async def test_link_drop_without_callback(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    transport.drop()
    await client.connect()
    assert client.ready
    await client.disconnect()


async def test_teardown_errors_are_logged_at_debug(
    connected_client: EfcClient,
    transport: FakeTransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport.fail_stop_notify = OSError("stop failed")
    transport.fail_disconnect = OSError("disconnect failed")
    with caplog.at_level(logging.DEBUG):
        await connected_client.disconnect()
    assert not connected_client.connected
    assert "transport stop_notify failed during teardown" in caplog.text
    assert "transport disconnect failed during teardown" in caplog.text
    assert "stop failed" in caplog.text


async def test_teardown_calls_time_out(
    transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(transport, response_timeout_seconds=0.05)
    await client.connect()
    transport.hang_stop_notify = True
    with caplog.at_level(logging.DEBUG):
        await client.disconnect()
    assert "transport stop_notify failed during teardown" in caplog.text
    assert transport.disconnect_calls == 1


async def test_bad_frames_are_dropped(
    connected_client: EfcClient,
    transport: FakeTransport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        transport.notify(b"\x1a\x01")
        await drain(connected_client)
    assert "Dropped EFC frame" in caplog.text
    assert connected_client.ready


async def test_update_callback_errors_are_logged(
    transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(_update: EfcUpdate) -> None:
        raise RuntimeError("boom")

    client = make_client(transport, update_callback=broken)
    with caplog.at_level(logging.ERROR):
        await client.connect()
    assert "EFC update callback failed" in caplog.text
    await client.disconnect()


async def test_processing_errors_are_logged(
    connected_client: EfcClient,
    transport: FakeTransport,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_payload: bytes) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(connected_client, "_process", broken)
    with caplog.at_level(logging.ERROR):
        transport.notify(status())
        await drain(connected_client)
    assert "EFC notification processing failed" in caplog.text


async def test_notifications_after_disconnect_are_ignored(
    transport: FakeTransport,
) -> None:
    updates: list[EfcUpdate] = []
    client = make_client(transport, update_callback=updates.append)
    await client.connect()
    await client.disconnect()
    count = len(updates)
    transport.notify(status())
    await asyncio.sleep(0)
    assert len(updates) == count


async def test_controls_need_allow_control(transport: FakeTransport) -> None:
    client = make_client(transport, allow_control=False)
    await client.connect()
    with pytest.raises(EfcControlDisabledError, match="disabled"):
        await client.start()
    with pytest.raises(EfcNotReadyError):
        await client.stop()
    await client.disconnect()


async def test_controls_need_ready(transport: FakeTransport) -> None:
    client = make_client(transport)
    with pytest.raises(EfcNotReadyError, match="not ready") as caught:
        await client.stop()
    assert not isinstance(caught.value, EfcControlDisabledError)


async def test_simple_controls_write_frames(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await connected_client.start()
    await connected_client.pause()
    await connected_client.resume()
    await connected_client.stop()
    await connected_client.request_sport_record()
    assert transport.writes[1:] == [
        start_command(),
        pause_command(),
        start_command(),
        stop_command(),
        sport_record_query(),
    ]
    assert set(transport.write_characteristics) == {WRITE_CHARACTERISTIC_UUID}


async def test_writes_are_spaced(transport: FakeTransport) -> None:
    client = make_client(transport, write_spacing_seconds=0.05)
    await client.connect()
    await client.start()
    await client.stop()
    gaps = [
        later - earlier
        for earlier, later in zip(
            transport.write_times, transport.write_times[1:], strict=False
        )
    ]
    assert all(gap >= 0.045 for gap in gaps)
    await client.disconnect()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"ramp_interval_seconds": 0.1}, "ramp_interval_seconds"),
        ({"ramp_interval_seconds": math.inf}, "finite"),
        ({"response_timeout_seconds": 0}, "response_timeout_seconds"),
        ({"response_timeout_seconds": math.nan}, "finite"),
        ({"keepalive_seconds": -1.0}, "keepalive_seconds"),
        ({"keepalive_seconds": math.inf}, "finite"),
        ({"write_spacing_seconds": -0.1}, "write_spacing_seconds"),
        ({"write_spacing_seconds": True}, "number"),
        ({"write_spacing_seconds": "1"}, "number"),
    ],
)
def test_timings_are_validated(
    transport: FakeTransport, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(EfcValidationError, match=message):
        EfcClient(transport, **options)


def test_zero_write_spacing_is_allowed(transport: FakeTransport) -> None:
    EfcClient(transport, write_spacing_seconds=0)


async def test_speed_is_validated(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    with pytest.raises(EfcValidationError, match=r"between 1 and 12 km/h"):
        await connected_client.set_speed(12.5)
    with pytest.raises(EfcValidationError, match=r"between 1 and 12 km/h"):
        await connected_client.set_speed(0.5)
    with pytest.raises(EfcValidationError, match="finite"):
        await connected_client.set_speed(float("inf"))
    with pytest.raises(EfcValidationError, match="number"):
        await connected_client.set_speed(True)
    with pytest.raises(EfcValidationError, match="number"):
        await connected_client.set_speed("3")  # type: ignore[arg-type]
    assert transport.writes == [device_info_query()]


async def test_speed_without_status_frame_is_not_ready(
    connected_client: EfcClient,
) -> None:
    connected_client._status_frame = None
    with pytest.raises(EfcNotReadyError, match="status frame"):
        await connected_client.set_speed(2.0)


async def test_speed_is_written_once_when_not_running(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await connected_client.set_speed(3.0)
    assert transport.writes[1:] == [speed_command(30)]


async def test_speed_ramps_up_and_down(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    await connected_client.set_speed(1.3)
    assert transport.writes[1:] == [speed_command(v) for v in (11, 12, 13)]
    transport.writes.clear()
    await running(connected_client, transport, 13)
    await connected_client.set_speed(1.1)
    assert transport.writes == [speed_command(v) for v in (12, 11)]
    transport.writes.clear()
    await connected_client.set_speed(1.3)
    assert transport.writes == [speed_command(13)]


async def test_ramp_starts_from_reported_speed(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 30)
    await connected_client.set_speed(3.1)
    assert transport.writes[1:] == [speed_command(31)]


async def test_stop_cancels_ramp(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    ramp = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 3)
    await connected_client.stop()
    await ramp
    assert transport.writes[1:] == [
        speed_command(11),
        speed_command(12),
        stop_command(),
    ]


async def test_new_speed_takes_over_ramp(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    first = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 2)
    transport.notify(status(speed=11, code=2))
    await drain(connected_client)
    await connected_client.set_speed(1.2)
    await first
    assert transport.writes[1:] == [speed_command(11), speed_command(12)]


async def test_cancelling_caller_cancels_ramp(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    caller = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0)
    ramps = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__name__", "") == "_ramp"
    ]
    assert ramps == []
    assert transport.writes[1:] == [speed_command(11)]


async def test_ramp_ends_when_belt_stops(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    ramp = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 2)
    transport.notify(status(speed=11, code=5))
    await ramp
    assert transport.writes[1:] == [speed_command(11)]


async def test_disconnect_cancels_ramp(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    ramp = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 2)
    await connected_client.disconnect()
    await ramp
    assert not connected_client.connected


async def test_link_drop_during_ramp_fails_set_speed(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    ramp = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 2)
    transport.drop()
    with pytest.raises(EfcConnectionError, match="session failed"):
        await ramp


async def test_set_speed_rereads_status_after_cancelling_ramp(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    first = asyncio.create_task(connected_client.set_speed(5.0))
    await wait_for_writes(transport, 2)
    transport.notify(status(speed=11, code=5))
    await connected_client.set_speed(2.0)
    await first
    assert transport.writes[-1] == speed_command(20)


async def test_incline() -> None:
    transport = FakeTransport(replies=[DEVICE_INFO, status(max_incline=10)])
    client = make_client(transport)
    await client.connect()
    await client.set_incline(4)
    assert transport.writes[-1] == incline_command(4)
    with pytest.raises(EfcValidationError, match="between 0 and 10"):
        await client.set_incline(11)
    with pytest.raises(EfcValidationError, match="whole percent"):
        await client.set_incline(2.5)  # type: ignore[arg-type]
    with pytest.raises(EfcValidationError, match="whole percent"):
        await client.set_incline(True)
    await client.disconnect()


async def test_incline_on_flat_treadmill(connected_client: EfcClient) -> None:
    with pytest.raises(EfcValidationError, match="does not support incline"):
        await connected_client.set_incline(1)


async def test_failed_write_ends_session(transport: FakeTransport) -> None:
    lost = asyncio.Event()
    errors: list[Exception] = []

    async def on_lost(error: Exception) -> None:
        errors.append(error)
        lost.set()

    client = make_client(transport, connection_lost_callback=on_lost)
    await client.connect()
    transport.fail_writes = True
    with pytest.raises(EfcConnectionError, match="Writing start") as caught:
        await client.start()
    assert isinstance(caught.value.__cause__, OSError)
    assert client.status is ConnectionStatus.DISCONNECTED
    await wait_for(lost)
    assert len(errors) == 1
    assert transport.disconnect_calls == 1
    with pytest.raises(EfcConnectionError, match="session failed") as again:
        await client.start()
    assert isinstance(again.value.__cause__, OSError)
    await client.disconnect()
    assert len(errors) == 1


async def test_write_timeout_ends_session(transport: FakeTransport) -> None:
    client = make_client(transport, response_timeout_seconds=0.05)
    await client.connect()
    transport.hang_writes = True
    with pytest.raises(EfcConnectionError, match="Writing stop") as caught:
        await client.stop()
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert not client.connected
    await client.disconnect()


async def test_programming_errors_propagate(
    connected_client: EfcClient,
    transport: FakeTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise KeyError("bug")

    monkeypatch.setattr(transport, "write_gatt_char", broken)
    with pytest.raises(KeyError):
        await connected_client.start()
    assert connected_client.ready


async def test_failed_ramp_write_ends_session(
    connected_client: EfcClient, transport: FakeTransport
) -> None:
    await running(connected_client, transport, 10)
    transport.fail_writes = True
    with pytest.raises(EfcConnectionError, match="Writing speed"):
        await connected_client.set_speed(2.0)
    assert not connected_client.connected


async def test_keepalive_queries_and_detects_loss(transport: FakeTransport) -> None:
    lost = LostRecorder()
    client = make_client(
        transport, keepalive_seconds=0.02, connection_lost_callback=lost
    )
    await client.connect()
    await wait_for_writes(transport, 3)
    assert transport.writes[:3] == [device_info_query()] * 3
    transport.fail_writes = True
    await wait_for(lost.called)
    assert not client.connected
    assert len(lost.errors) == 1
    assert isinstance(lost.errors[0], OSError)


@pytest.mark.parametrize("code", [0, 6])
async def test_counters_reset_in_idle_and_standby(
    connected_client: EfcClient, transport: FakeTransport, code: int
) -> None:
    await running(connected_client, transport, 20)
    transport.notify(counters(elapsed=5995, steps=9999, energy=9999))
    transport.notify(counters(elapsed=2, steps=3, energy=4))
    await drain(connected_client)
    assert connected_client.state.elapsed_seconds == 6002
    assert connected_client.state.steps == 10003
    assert connected_client.state.energy_kcal == 1000.4
    transport.notify(status(code=code))
    transport.notify(counters())
    await drain(connected_client)
    assert connected_client.state.elapsed_seconds == 0


async def test_replay_walking_session(transport: FakeTransport) -> None:
    updates: list[EfcUpdate] = []

    async def on_update(update: EfcUpdate) -> None:
        updates.append(update)

    client = make_client(transport, update_callback=on_update)
    await client.connect()
    for row in load_capture("wp9_walking.jsonl"):
        if row["dir"] == "rx":
            transport.notify(bytes.fromhex(row["hex"]))
    await drain(client)
    state = client.state
    assert state.elapsed_seconds == 121
    assert state.distance_m == 187
    assert state.energy_kcal == 6.1
    assert state.steps == 262
    assert state.workout_state is WorkoutState.SUMMARY
    seen = {
        update.frame.workout_state
        for update in updates
        if isinstance(update.frame, StatusFrame)
    }
    assert {
        WorkoutState.COUNTDOWN,
        WorkoutState.RUNNING,
        WorkoutState.SUMMARY,
        WorkoutState.STANDBY,
    } <= seen
    await client.disconnect()


async def test_update_callback_can_disconnect(transport: FakeTransport) -> None:
    client: EfcClient
    done = asyncio.Event()

    async def on_update(update: EfcUpdate) -> None:
        if update.raw == counters(elapsed=1):
            await client.disconnect()
            done.set()

    client = make_client(transport, update_callback=on_update)
    await client.connect()
    transport.notify(counters(elapsed=1))
    await wait_for(done)
    assert not client.connected


def test_frame_helper_matches_capture() -> None:
    assert frame(0x05, DEVICE_INFO[3:-1]) == DEVICE_INFO
