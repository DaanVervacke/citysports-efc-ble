from __future__ import annotations

import asyncio
import logging

import pytest
from citysports_efc_ble import (
    ConnectionStatus,
    EfcClient,
    EfcConnectionError,
    EfcNotReadyError,
    EfcTimeoutError,
    EfcUpdate,
    EfcValidationError,
    StatusFrame,
    WorkoutState,
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

from .conftest import (
    DEVICE_INFO,
    FakeTransport,
    counters,
    frame,
    load_capture,
    settle,
    status,
)


def make_client(transport: FakeTransport, **kwargs: object) -> EfcClient:
    options: dict[str, object] = {
        "write_spacing": 0.0,
        "ramp_interval": 0.15,
        "response_timeout": 1.0,
        "keepalive_seconds": 60.0,
        "allow_control": True,
    }
    options.update(kwargs)
    return EfcClient(transport, **options)  # type: ignore[arg-type]


async def running(client: EfcClient, transport: FakeTransport, speed: int) -> None:
    transport.notify(status(speed=speed, code=2))
    await settle()
    assert client.state.workout_state is WorkoutState.RUNNING


async def test_connect_reaches_ready(transport: FakeTransport) -> None:
    updates: list[EfcUpdate] = []

    async def on_update(update: EfcUpdate) -> None:
        updates.append(update)

    client = make_client(transport, update_callback=on_update)
    assert not client.connected
    await client.connect()
    assert client.ready
    assert client.connected
    assert client.status is ConnectionStatus.READY
    assert transport.writes == [device_info_query()]
    assert transport.responses == [True]
    assert client.state.system_id == "11:22:33:44:55:66"
    assert client.state.workout_state is WorkoutState.STANDBY
    assert [update.raw for update in updates][:1] == [DEVICE_INFO]
    await client.connect()
    assert transport.connect_calls == 1
    await client.disconnect()
    assert client.status is ConnectionStatus.DISCONNECTED
    assert transport.disconnect_calls == 1
    assert transport.stop_notify_calls == 1


async def test_context_manager(transport: FakeTransport) -> None:
    async with make_client(transport) as client:
        assert client.ready
    assert not client.connected


async def test_connect_times_out_without_status_frame() -> None:
    transport = FakeTransport(replies=[DEVICE_INFO])
    client = make_client(transport, response_timeout=0.05)
    with pytest.raises(EfcTimeoutError):
        await client.connect()
    assert not client.connected
    assert transport.disconnect_calls == 1


async def test_connect_propagates_transport_errors() -> None:
    transport = FakeTransport()
    transport.fail_connect = OSError("no adapter")
    client = make_client(transport)
    with pytest.raises(OSError, match="no adapter"):
        await client.connect()
    assert client.status is ConnectionStatus.DISCONNECTED


async def test_link_drop_while_connecting_fails_connect() -> None:
    transport = FakeTransport(replies=[])
    lost: list[Exception] = []
    client = make_client(transport, connection_lost_callback=lost.append)

    def drop(_data: bytes) -> None:
        assert transport.disconnected_callback is not None
        transport.disconnected_callback()

    transport.on_write = drop
    with pytest.raises(EfcConnectionError, match="while connecting"):
        await client.connect()
    await settle()
    assert len(lost) == 1
    assert not client.connected


async def test_client_reconnects_with_fresh_state(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    transport.notify(counters(elapsed=10))
    await settle()
    assert client.state.elapsed_seconds == 10
    await client.disconnect()
    await client.connect()
    assert client.state.elapsed_seconds is None
    await client.disconnect()


async def test_bad_frames_are_dropped(
    transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(transport)
    await client.connect()
    with caplog.at_level(logging.WARNING):
        transport.notify(b"\x1a\x01")
        await settle()
    assert "Dropped EFC frame" in caplog.text
    assert client.ready
    await client.disconnect()


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
    transport: FakeTransport,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(transport)
    await client.connect()

    async def broken(_payload: bytes) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client, "_process", broken)
    with caplog.at_level(logging.ERROR):
        transport.notify(status())
        await settle()
    assert "EFC notification processing failed" in caplog.text
    await client.disconnect()


async def test_notifications_before_connect_are_ignored(
    transport: FakeTransport,
) -> None:
    client = make_client(transport)
    client._notification("x", status())
    assert client._frames.empty()


async def test_controls_need_allow_control(transport: FakeTransport) -> None:
    client = make_client(transport, allow_control=False)
    await client.connect()
    with pytest.raises(EfcNotReadyError, match="disabled"):
        await client.start()
    await client.disconnect()


async def test_controls_need_ready(transport: FakeTransport) -> None:
    client = make_client(transport)
    with pytest.raises(EfcNotReadyError, match="not ready"):
        await client.stop()


async def test_simple_controls_write_frames(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await client.start()
    await client.pause()
    await client.resume()
    await client.stop()
    await client.request_sport_record()
    assert transport.writes[1:] == [
        start_command(),
        pause_command(),
        start_command(),
        stop_command(),
        sport_record_query(),
    ]
    await client.disconnect()


async def test_writes_are_spaced(transport: FakeTransport) -> None:
    client = make_client(transport, write_spacing=0.05)
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


def test_ramp_interval_has_a_floor(transport: FakeTransport) -> None:
    with pytest.raises(EfcValidationError, match="ramp_interval"):
        EfcClient(transport, ramp_interval=0.1)


async def test_speed_is_validated(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    with pytest.raises(EfcValidationError, match=r"between 1 and 12 km/h"):
        await client.set_speed(12.5)
    with pytest.raises(EfcValidationError, match=r"between 1 and 12 km/h"):
        await client.set_speed(0.5)
    with pytest.raises(EfcValidationError, match="finite"):
        await client.set_speed(float("inf"))
    assert transport.writes == [device_info_query()]
    await client.disconnect()


async def test_speed_without_status_frame_is_not_ready(
    transport: FakeTransport,
) -> None:
    client = make_client(transport)
    await client.connect()
    client._status_frame = None
    with pytest.raises(EfcNotReadyError, match="status frame"):
        await client.set_speed(2.0)
    await client.disconnect()


async def test_speed_is_written_once_when_not_running(
    transport: FakeTransport,
) -> None:
    client = make_client(transport)
    await client.connect()
    await client.set_speed(3.0)
    assert transport.writes[1:] == [speed_command(30)]
    await client.disconnect()


async def test_speed_ramps_up_and_down(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    await client.set_speed(1.3)
    assert transport.writes[1:] == [speed_command(v) for v in (11, 12, 13)]
    transport.writes.clear()
    await running(client, transport, 13)
    await client.set_speed(1.1)
    assert transport.writes == [speed_command(v) for v in (12, 11)]
    transport.writes.clear()
    await client.set_speed(1.3)
    assert transport.writes == [speed_command(13)]
    await client.disconnect()


async def test_ramp_starts_from_reported_speed(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 30)
    await client.set_speed(3.1)
    assert transport.writes[1:] == [speed_command(31)]
    await client.disconnect()


async def test_stop_cancels_ramp(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    ramp = asyncio.create_task(client.set_speed(5.0))
    await asyncio.sleep(0.2)
    await client.stop()
    await ramp
    assert transport.writes[-1] == stop_command()
    assert transport.writes[1:3] == [speed_command(11), speed_command(12)]
    assert len(transport.writes) < 10
    await client.disconnect()


async def test_new_speed_takes_over_ramp(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    first = asyncio.create_task(client.set_speed(5.0))
    await asyncio.sleep(0.05)
    transport.notify(status(speed=11, code=2))
    await settle()
    await client.set_speed(1.2)
    await first
    assert transport.writes[-1] == speed_command(12)
    assert speed_command(13) not in transport.writes
    await client.disconnect()


async def test_cancelling_caller_cancels_ramp(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    caller = asyncio.create_task(client.set_speed(5.0))
    await asyncio.sleep(0.05)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0.3)
    assert transport.writes[1:] == [speed_command(11)]
    await client.disconnect()


async def test_ramp_ends_when_belt_stops(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    ramp = asyncio.create_task(client.set_speed(5.0))
    await asyncio.sleep(0.05)
    transport.notify(status(speed=11, code=5))
    await ramp
    assert transport.writes[1:] == [speed_command(11)]
    await client.disconnect()


async def test_disconnect_cancels_ramp(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    ramp = asyncio.create_task(client.set_speed(5.0))
    await asyncio.sleep(0.05)
    await client.disconnect()
    await ramp
    assert not client.connected


async def test_incline(transport: FakeTransport) -> None:
    transport.replies = [DEVICE_INFO, status(max_incline=10)]
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


async def test_incline_on_flat_treadmill(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    with pytest.raises(EfcValidationError, match="does not support incline"):
        await client.set_incline(1)
    await client.disconnect()


async def test_failed_write_ends_session(transport: FakeTransport) -> None:
    lost: list[Exception] = []

    async def on_lost(error: Exception) -> None:
        lost.append(error)

    client = make_client(transport, connection_lost_callback=on_lost)
    await client.connect()
    transport.fail_writes = True
    with pytest.raises(EfcConnectionError, match="Writing start"):
        await client.start()
    assert client.status is ConnectionStatus.DISCONNECTED
    assert len(lost) == 1
    assert transport.disconnect_calls == 1
    await client._handle_session_failure(OSError("again"))
    assert len(lost) == 1
    client._session_failure = OSError("gone")
    with pytest.raises(EfcConnectionError, match="session failed"):
        await client._send(start_command(), "start")


async def test_failed_ramp_write_ends_session(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 10)
    transport.fail_writes = True
    with pytest.raises(EfcConnectionError, match="Writing speed"):
        await client.set_speed(2.0)
    assert not client.connected


async def test_link_drop_ends_session(
    transport: FakeTransport, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_error: Exception) -> None:
        raise RuntimeError("boom")

    client = make_client(transport, connection_lost_callback=broken)
    await client.connect()
    assert transport.disconnected_callback is not None
    with caplog.at_level(logging.WARNING):
        transport.disconnected_callback()
        await settle()
    assert not client.connected
    assert "connection lost callback failed" in caplog.text
    transport.disconnected_callback()
    await settle()


async def test_link_drop_during_disconnect_is_ignored(
    transport: FakeTransport,
) -> None:
    client = make_client(transport)
    await client.connect()

    async def drop_on_disconnect() -> None:
        assert transport.disconnected_callback is not None
        transport.disconnected_callback()

    transport.disconnect = drop_on_disconnect  # type: ignore[method-assign]
    await client.disconnect()
    await settle()
    assert not client._background


async def test_failure_while_closing_skips_cleanup(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    client._closing = True
    await client._handle_session_failure(OSError("late"))
    client._closing = False
    assert client.status is ConnectionStatus.DISCONNECTED
    assert transport.disconnect_calls == 0
    await client.disconnect()
    assert transport.disconnect_calls == 1


async def test_keepalive_queries_and_detects_loss(transport: FakeTransport) -> None:
    lost: list[Exception] = []
    client = make_client(
        transport, keepalive_seconds=0.02, connection_lost_callback=lost.append
    )
    await client.connect()
    await asyncio.sleep(0.07)
    assert transport.writes.count(device_info_query()) >= 2
    transport.fail_writes = True
    await asyncio.sleep(0.05)
    assert not client.connected
    assert len(lost) == 1


async def test_counters_reset_in_standby_and_wrap(transport: FakeTransport) -> None:
    client = make_client(transport)
    await client.connect()
    await running(client, transport, 20)
    transport.notify(counters(elapsed=5995, steps=9999, energy=9999))
    transport.notify(counters(elapsed=2, steps=3, energy=4))
    await settle()
    assert client.state.elapsed_seconds == 6002
    assert client.state.steps == 10003
    assert client.state.energy_kcal == 1000.4
    transport.notify(status(code=6))
    transport.notify(counters())
    await settle()
    assert client.state.elapsed_seconds == 0


async def test_replay_walking_session(transport: FakeTransport) -> None:
    updates: list[EfcUpdate] = []

    async def on_update(update: EfcUpdate) -> None:
        updates.append(update)

    client = make_client(transport, update_callback=on_update)
    await client.connect()
    for row in load_capture("wp9_walking.jsonl"):
        if row["dir"] == "rx":
            transport.notify(bytes.fromhex(row["hex"]))
    await settle()
    for _ in range(50):
        await asyncio.sleep(0)
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

    async def on_update(update: EfcUpdate) -> None:
        if update.raw == counters(elapsed=1):
            await client.disconnect()

    client = make_client(transport, update_callback=on_update)
    await client.connect()
    transport.notify(counters(elapsed=1))
    await settle()
    assert not client.connected
    assert client._worker is None


def test_frame_helper_matches_capture() -> None:
    assert frame(0x05, DEVICE_INFO[3:-1]) == DEVICE_INFO
