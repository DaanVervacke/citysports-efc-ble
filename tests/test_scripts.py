import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from bleak.backends.device import BLEDevice
from citysports_efc_ble import EfcClient, EfcState, WorkoutState
from citysports_efc_ble.protocol import (
    device_info_query,
    speed_command,
    start_command,
    stop_command,
)
from scripts import _efc_common as common
from scripts import drive_efc_client, probe_efc

from .helpers import DEVICE_INFO, FakeTransport, counters, status

REAL_DEVICE_INFO = bytes.fromhex("1a050c00220017000194cdb172ab3c2a")


@pytest.fixture(autouse=True)
def _clear_script_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EFC_PROXY", "EFC_NOISE_PSK", "EFC_DEVICE_ADDRESS"):
        monkeypatch.delenv(name, raising=False)


def test_redact_frame() -> None:
    assert common.redact_frame(REAL_DEVICE_INFO) == DEVICE_INFO
    assert common.redact_frame(status()) == status()
    assert common.redact_frame(b"\x00") == b"\x00"


def test_state_record_redacts_and_names() -> None:
    state = EfcState(system_id="AA:BB:CC:DD:EE:FF", workout_state=WorkoutState.RUNNING)
    record = common.state_record(state)
    assert record["system_id"] == "11:22:33:44:55:66"
    assert record["workout_state"] == "RUNNING"
    assert record["fault"] is None
    assert common.state_record(EfcState())["system_id"] is None


def test_capture_writer(tmp_path: Path) -> None:
    path = tmp_path / "out" / "capture.jsonl"
    with common.CaptureWriter(path) as writer:
        writer.write("rx", REAL_DEVICE_INFO)
    writer.close()
    record = json.loads(path.read_text())
    assert record["dir"] == "rx"
    assert record["hex"] == DEVICE_INFO.hex()
    with common.CaptureWriter(None) as empty:
        empty.write("tx", b"\x01")


async def test_capture_transport_records_both_directions(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    inner = FakeTransport()
    with common.CaptureWriter(path) as writer:
        async with EfcClient(
            common.CaptureTransport(inner, writer), write_spacing_seconds=0
        ) as client:
            assert client.ready
    directions = [json.loads(line)["dir"] for line in path.read_text().splitlines()]
    assert directions[0] == "tx"
    assert "rx" in directions
    assert inner.stop_notify_calls == 1
    assert inner.disconnect_calls == 1


def test_proxy_host() -> None:
    assert common.proxy_host("192.168.1.2") == "192.168.1.2"
    assert common.proxy_host("http://proxy.local:6053") == "proxy.local"


def _sighting(
    address: str, name: str | None, uuids: tuple[str, ...] = ()
) -> common.Sighting:
    return common.Sighting(BLEDevice(address, name, None), name, -60, uuids)


async def test_find_treadmill() -> None:
    sightings = [
        _sighting("11:11:11:11:11:11", "Phone"),
        _sighting("22:22:22:22:22:22", "CITYSPORTS-LINKER"),
    ]

    async def scan(_seconds: float) -> list[common.Sighting]:
        return sightings

    found = await common.find_treadmill(scan, None, 1)
    assert found.address == "22:22:22:22:22:22"
    found = await common.find_treadmill(scan, "11:11:11:11:11:11", 1)
    assert found.address == "11:11:11:11:11:11"
    with pytest.raises(TimeoutError):
        await common.find_treadmill(scan, "33:33:33:33:33:33", 1)


async def test_local_scanner_is_yielded_without_proxy() -> None:
    async with common.open_scanner(None, None) as scan:
        assert scan is common.scan_local


def test_load_config(tmp_path: Path) -> None:
    assert common.load_config(tmp_path / "missing.json") == {}
    good = tmp_path / "good.json"
    good.write_text('{"address": "AA"}')
    assert common.load_config(good) == {"address": "AA"}
    not_object = tmp_path / "list.json"
    not_object.write_text("[]")
    with pytest.raises(ValueError, match=r"list\.json must contain a JSON object"):
        common.load_config(not_object)
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(ValueError, match=r"broken\.json is not valid JSON"):
        common.load_config(broken)


def test_probe_config_from_args() -> None:
    config = probe_efc.config_from_args(
        ["--address", "AA", "--capture-seconds", "5", "--list-advertisements"]
    )
    assert config.address == "AA"
    assert config.capture_seconds == 5
    assert config.list_advertisements
    assert config.proxy is None


def test_probe_main_reports_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(_config: probe_efc.ProbeConfig) -> None:
        raise RuntimeError("no adapter")

    monkeypatch.setattr(probe_efc, "capture", failing)
    assert probe_efc.main(["--capture-seconds", "0"]) == 1
    assert "no adapter" in caplog.text
    assert any(record.exc_info for record in caplog.records)


async def test_probe_lists_advertisements(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def local(_seconds: float) -> list[common.Sighting]:
        return [_sighting("22:22:22:22:22:22", "CITYSPORTS-LINKER")]

    monkeypatch.setattr(common, "scan_local", local)
    config = probe_efc.config_from_args(["--list-advertisements"])
    with caplog.at_level("INFO"):
        await probe_efc.list_advertisements(config)
    assert "efc=True" in caplog.text
    assert "DEVICE_ADDRESS" in caplog.text


async def test_probe_capture_records_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = FakeTransport()

    @asynccontextmanager
    async def scanner(
        _proxy: str | None, _noise_psk: str | None
    ) -> AsyncIterator[common.Scanner]:
        async def scan(_seconds: float) -> list[common.Sighting]:
            return [_sighting("22:22:22:22:22:22", "CITYSPORTS-LINKER")]

        yield scan

    def bleak_transport(device: BLEDevice) -> FakeTransport:
        assert device.address == "22:22:22:22:22:22"
        return transport

    monkeypatch.setattr(common, "open_scanner", scanner)
    monkeypatch.setattr(probe_efc, "BleakTransport", bleak_transport)
    output = tmp_path / "capture.jsonl"
    config = probe_efc.config_from_args(
        ["--capture-seconds", "0", "--output", str(output)]
    )
    await probe_efc.capture(config)
    assert transport.writes == [device_info_query()]
    assert transport.disconnect_calls == 1
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert records[0] == {"t": records[0]["t"], "dir": "tx", "hex": "a10500a4"}
    assert {"dir": "rx", "hex": DEVICE_INFO.hex()} in [
        {"dir": record["dir"], "hex": record["hex"]} for record in records
    ]


def test_drive_needs_confirmation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        drive_efc_client.config_from_args(
            ["--controls", "--config", str(tmp_path / "none.json")]
        )


def test_drive_rejects_invalid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "local.json"
    path.write_text("not json")
    with pytest.raises(SystemExit) as exc_info:
        drive_efc_client.config_from_args(["--config", str(path)])
    assert exc_info.value.code == 2
    assert "local.json is not valid JSON" in capsys.readouterr().err


def test_drive_merges_config(tmp_path: Path) -> None:
    path = tmp_path / "local.json"
    path.write_text('{"proxy": "proxy.local", "address": "AA"}')
    config = drive_efc_client.config_from_args(
        ["--config", str(path), "--address", "BB", "--duration", "1"]
    )
    assert config.proxy == "proxy.local"
    assert config.address == "BB"
    assert config.duration == 1
    assert not config.controls


def _run_config(**changes: Any) -> drive_efc_client.RunConfig:
    values: dict[str, Any] = {
        "proxy": None,
        "noise_psk": None,
        "address": None,
        "output": None,
        "scan_seconds": 0.0,
        "duration": 0.0,
        "controls": False,
        "speed": 1.2,
        "run_seconds": 0.0,
    }
    values.update(changes)
    return drive_efc_client.RunConfig(**values)


async def test_drive_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport()
    await drive_efc_client.run(_run_config(output=tmp_path / "c.jsonl"), transport)
    assert transport.writes == [device_info_query()]
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0])["frame"] == "DeviceInfoFrame"


async def test_drive_controls() -> None:
    transport = FakeTransport()

    def answer(data: bytes) -> None:
        if data == start_command():
            transport.notify(status(speed=10, code=2))
            transport.notify(counters(elapsed=1))

    transport.on_write = answer
    await drive_efc_client.run(_run_config(controls=True), transport)
    assert transport.writes[1] == start_command()
    assert speed_command(12) in transport.writes
    assert transport.writes[-1] == stop_command()


async def test_exercise_stops_when_belt_never_runs() -> None:
    transport = FakeTransport()
    monitor = drive_efc_client.Monitor()
    async with EfcClient(
        transport,
        allow_control=True,
        write_spacing_seconds=0,
        update_callback=monitor.on_update,
    ) as client:
        with pytest.raises(TimeoutError):
            await drive_efc_client.exercise(
                client, _run_config(controls=True), monitor, start_timeout=0.05
            )
    assert transport.writes[-1] == stop_command()


async def test_exercise_keeps_original_error_when_stop_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport()

    def fail_after_start(data: bytes) -> None:
        if data == start_command():
            transport.fail_writes = True

    transport.on_write = fail_after_start
    monitor = drive_efc_client.Monitor()
    async with EfcClient(
        transport,
        allow_control=True,
        write_spacing_seconds=0,
        update_callback=monitor.on_update,
    ) as client:
        with pytest.raises(TimeoutError):
            await drive_efc_client.exercise(
                client, _run_config(controls=True), monitor, start_timeout=0.05
            )
    assert transport.writes[-1] == start_command()
    assert "Stop command after a failed run failed" in caplog.text


def test_drive_main_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(_config: drive_efc_client.RunConfig) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("no adapter")

    monkeypatch.setattr(drive_efc_client, "run", failing)
    assert drive_efc_client.main(["--config", str(tmp_path / "none.json")]) == 1
    assert "no adapter" in caplog.text
    assert any(record.exc_info for record in caplog.records)
