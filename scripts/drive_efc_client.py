"""Drive the EFC library against a real treadmill, read-only by default."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from citysports_efc_ble import (
    BleakTransport,
    BleTransport,
    EfcClient,
    EfcError,
    EfcUpdate,
    WorkoutState,
    __version__,
)

from scripts import _efc_common as common

_LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG = Path(__file__).resolve().with_name("drive_efc_client.local.json")


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Settings for one client run."""

    proxy: str | None
    noise_psk: str | None
    address: str | None
    output: Path | None
    scan_seconds: float
    duration: float
    controls: bool
    speed: float
    run_seconds: float


def print_update(update: EfcUpdate) -> None:
    """Print the state after one frame as one JSON line."""
    record = {
        "status": update.status.value,
        "frame": type(update.frame).__name__,
        "state": common.state_record(update.state),
    }
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")


class Monitor:
    """Print every update and signal when the belt runs."""

    def __init__(self) -> None:
        """Start with the running event cleared."""
        self.running = asyncio.Event()

    async def on_update(self, update: EfcUpdate) -> None:
        """Print ``update`` and set ``running`` once the belt runs."""
        print_update(update)
        if update.state.workout_state is WorkoutState.RUNNING:
            self.running.set()


async def exercise(
    client: EfcClient,
    config: RunConfig,
    monitor: Monitor,
    start_timeout: float = 10.0,
) -> None:
    """Run the read-only window and, when enabled, a short workout.

    Raises:
        TimeoutError: The belt did not run within ``start_timeout`` seconds
            after the start command. The stop command is still sent.
    """
    if not config.controls:
        await asyncio.sleep(config.duration)
        return
    try:
        await client.start()
        async with asyncio.timeout(start_timeout):
            await monitor.running.wait()
        await client.set_speed(config.speed)
        await asyncio.sleep(config.run_seconds)
    except BaseException:
        await _stop_after_failure(client)
        raise
    if client.ready:
        await client.stop()
    await asyncio.sleep(config.duration)


async def _stop_after_failure(client: EfcClient) -> None:
    """Send the stop command and log a failure instead of raising it."""
    if not client.ready:
        return
    try:
        await client.stop()
    except EfcError:
        _LOGGER.warning("Stop command after a failed run failed", exc_info=True)


async def run(config: RunConfig, transport: BleTransport | None = None) -> None:
    """Connect, print every update and run the requested exercise."""
    async with common.open_scanner(config.proxy, config.noise_psk) as scan:
        if transport is None:
            device = await common.find_treadmill(
                scan, config.address, config.scan_seconds
            )
            transport = BleakTransport(device)
        monitor = Monitor()
        with common.CaptureWriter(config.output) as writer:
            client = EfcClient(
                common.CaptureTransport(transport, writer),
                allow_control=config.controls,
                update_callback=monitor.on_update,
            )
            async with client:
                await exercise(client, config, monitor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--proxy")
    parser.add_argument("--noise-psk")
    parser.add_argument("--address")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--scan-seconds", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--controls", action="store_true")
    parser.add_argument("--confirm-controls", action="store_true")
    parser.add_argument("--speed", type=float, default=2.0)
    parser.add_argument("--run-seconds", type=float, default=10.0)
    return parser


def config_from_args(argv: Sequence[str] | None = None) -> RunConfig:
    """Merge the command line with the optional JSON config file.

    Raises:
        SystemExit: ``--controls`` was given without ``--confirm-controls``,
            or the config file is not a JSON object.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    if args.controls and not args.confirm_controls:
        parser.error("--controls needs --confirm-controls")
    try:
        stored = common.load_config(args.config)
    except ValueError as err:
        parser.error(str(err))
    return RunConfig(
        proxy=args.proxy or stored.get("proxy"),
        noise_psk=args.noise_psk or stored.get("noise_psk"),
        address=args.address or stored.get("address"),
        output=args.output,
        scan_seconds=args.scan_seconds,
        duration=args.duration,
        controls=args.controls,
        speed=args.speed,
        run_seconds=args.run_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv, drive the client and return the exit code."""
    config = config_from_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        return 1
    except Exception as err:
        _LOGGER.warning("Client run failed: %s", err, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
