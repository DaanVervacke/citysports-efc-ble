from __future__ import annotations

import asyncio
import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
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

from .conftest import DEVICE_INFO, FakeTransport, counters, status

SCRIPTS = Path(__file__).parent.parent / "scripts"
REAL_DEVICE_INFO = bytes.fromhex("1a050c00220017000194cdb172ab3c2a")


def _load(name: str) -> ModuleType:
    spec = spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("probe_efc")
library_test = _load("test_efc_client")
common = probe.common


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
    writer = common.CaptureWriter(path)
    writer.write("rx", REAL_DEVICE_INFO)
    writer.close()
    writer.close()
    record = json.loads(path.read_text())
    assert record["dir"] == "rx"
    assert record["hex"] == DEVICE_INFO.hex()
    common.CaptureWriter(None).write("tx", b"\x01")


async def test_capture_transport_records_both_directions(tmp_path: Path) -> None:
    path = tmp_path / "capture.jsonl"
    inner = FakeTransport()
    writer = common.CaptureWriter(path)
    async with EfcClient(
        common.CaptureTransport(inner, writer), write_spacing=0
    ) as client:
        assert client.ready
    writer.close()
    directions = [json.loads(line)["dir"] for line in path.read_text().splitlines()]
    assert directions[0] == "tx"
    assert "rx" in directions
    assert inner.stop_notify_calls == 1
    assert inner.disconnect_calls == 1


def test_proxy_host() -> None:
    assert common.proxy_host("192.168.1.2") == "192.168.1.2"
    assert common.proxy_host("http://proxy.local:6053") == "proxy.local"


def _sighting(address: str, name: str | None, uuids: tuple[str, ...] = ()) -> Any:
    return common.Sighting(BLEDevice(address, name, None), name, -60, uuids)


async def test_find_treadmill() -> None:
    sightings = [
        _sighting("11:11:11:11:11:11", "Phone"),
        _sighting("22:22:22:22:22:22", "CITYSPORTS-LINKER"),
    ]

    async def scan(_seconds: float) -> list[Any]:
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
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    with pytest.raises(TypeError, match="JSON object"):
        common.load_config(bad)


def test_probe_config_from_args(monkeypatch: pytest.MonkeyPatch) -> None:
    config = probe.config_from_args(
        ["--address", "AA", "--capture-seconds", "5", "--list-advertisements"]
    )
    assert config.address == "AA"
    assert config.capture_seconds == 5
    assert config.list_advertisements
    assert config.proxy is None


def test_probe_main_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(_config: Any) -> None:
        raise RuntimeError("no adapter")

    monkeypatch.setattr(probe, "capture", failing)
    assert probe.main(["--capture-seconds", "0"]) == 1


async def test_probe_lists_advertisements(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scan(_seconds: float) -> list[Any]:
        return [_sighting("22:22:22:22:22:22", "CITYSPORTS-LINKER")]

    async def local(_seconds: float) -> list[Any]:
        return await scan(_seconds)

    monkeypatch.setattr(probe.common, "scan_local", local)
    config = probe.config_from_args(["--list-advertisements"])
    with caplog.at_level("INFO"):
        await probe.list_advertisements(config)
    assert "efc=True" in caplog.text
    assert "DEVICE_ADDRESS" in caplog.text


def test_library_test_needs_confirmation(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        library_test.config_from_args(
            ["--controls", "--config", str(tmp_path / "none.json")]
        )


def test_library_test_merges_config(tmp_path: Path) -> None:
    path = tmp_path / "local.json"
    path.write_text('{"proxy": "proxy.local", "address": "AA"}')
    config = library_test.config_from_args(
        ["--config", str(path), "--address", "BB", "--duration", "1"]
    )
    assert config.proxy == "proxy.local"
    assert config.address == "BB"
    assert config.duration == 1
    assert not config.controls


def _run_config(**changes: Any) -> Any:
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
    return library_test.RunConfig(**values)


async def test_library_test_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport()
    await library_test.run(_run_config(output=tmp_path / "c.jsonl"), transport)
    assert transport.writes == [device_info_query()]
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0])["frame"] == "DeviceInfoFrame"


async def test_library_test_controls() -> None:
    transport = FakeTransport()

    def answer(data: bytes) -> None:
        if data == start_command():
            transport.notify(status(speed=10, code=2))
            transport.notify(counters(elapsed=1))

    transport.on_write = answer
    await library_test.run(_run_config(controls=True), transport)
    assert transport.writes[1] == start_command()
    assert speed_command(12) in transport.writes
    assert transport.writes[-1] == stop_command()


async def test_exercise_stops_when_belt_never_runs() -> None:
    transport = FakeTransport()
    monitor = library_test.Monitor()
    async with EfcClient(
        transport,
        allow_control=True,
        write_spacing=0,
        update_callback=monitor.on_update,
    ) as client:
        with pytest.raises(TimeoutError):
            await library_test.exercise(
                client, _run_config(controls=True), monitor, start_timeout=0.05
            )
    assert transport.writes[-1] == stop_command()


def test_library_test_main_reports_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def failing(_config: Any) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("no adapter")

    monkeypatch.setattr(library_test, "run", failing)
    assert library_test.main(["--config", str(tmp_path / "none.json")]) == 1
