"""Developer probe that logs EFC treadmill frames without sending controls."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from citysports_efc_ble import BleakTransport, EfcClient, __version__

_LOGGER = logging.getLogger(__name__)


def _load_common() -> ModuleType:
    """Load the shared helpers that ship next to this script."""
    path = Path(__file__).resolve().with_name("_efc_common.py")
    spec = importlib.util.spec_from_file_location("_efc_common", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load shared helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


common = _load_common()


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Settings for one probe run."""

    proxy: str | None
    noise_psk: str | None
    address: str | None
    output: Path | None
    scan_seconds: float
    capture_seconds: float
    list_advertisements: bool
    show_identities: bool


async def list_advertisements(config: ProbeConfig) -> None:
    """Log every advertisement seen during the scan window."""
    async with common.open_scanner(config.proxy, config.noise_psk) as scan:
        sightings = await scan(config.scan_seconds)
    for sighting in sightings:
        address = (
            sighting.device.address if config.show_identities else "DEVICE_ADDRESS"
        )
        _LOGGER.info(
            "ADVERTISEMENT address=%s name=%s rssi=%s efc=%s service_uuids=%s",
            address,
            sighting.name,
            sighting.rssi,
            sighting.is_efc,
            list(sighting.service_uuids),
        )
    _LOGGER.info("Advertisement scan complete, unique devices=%d", len(sightings))


async def capture(config: ProbeConfig) -> None:
    """Connect read-only and log every frame for the capture window."""
    writer = common.CaptureWriter(config.output)
    try:
        async with common.open_scanner(config.proxy, config.noise_psk) as scan:
            device = await common.find_treadmill(
                scan, config.address, config.scan_seconds
            )
            transport = common.CaptureTransport(BleakTransport(device), writer)
            async with EfcClient(transport) as client:
                _LOGGER.info(
                    "Connected manufacturer=%s model=%s revision=%s",
                    client.state.manufacturer_code,
                    client.state.model_code,
                    client.state.revision,
                )
                await asyncio.sleep(config.capture_seconds)
    finally:
        writer.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--proxy", default=os.getenv("EFC_PROXY"))
    parser.add_argument("--noise-psk", default=os.getenv("EFC_NOISE_PSK"))
    parser.add_argument("--address", default=os.getenv("EFC_DEVICE_ADDRESS"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--scan-seconds", type=float, default=10.0)
    parser.add_argument("--capture-seconds", type=float, default=30.0)
    parser.add_argument(
        "--list-advertisements",
        action="store_true",
        help="List every advertisement and do not connect",
    )
    parser.add_argument(
        "--show-identities",
        action="store_true",
        help="Show real Bluetooth addresses in the log",
    )
    return parser


def config_from_args(argv: Sequence[str] | None = None) -> ProbeConfig:
    """Parse the command line into a probe config."""
    args = _parser().parse_args(argv)
    return ProbeConfig(
        proxy=args.proxy,
        noise_psk=args.noise_psk,
        address=args.address,
        output=args.output,
        scan_seconds=args.scan_seconds,
        capture_seconds=args.capture_seconds,
        list_advertisements=args.list_advertisements,
        show_identities=args.show_identities,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the probe command."""
    config = config_from_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    run = list_advertisements if config.list_advertisements else capture
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        return 1
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Probe failed: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
