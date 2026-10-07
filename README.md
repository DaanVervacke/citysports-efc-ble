# citysports-efc-ble

[![Check](https://github.com/DaanVervacke/citysports-efc-ble/actions/workflows/check.yml/badge.svg)](https://github.com/DaanVervacke/citysports-efc-ble/actions/workflows/check.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Async Python library for treadmills that speak the EFC protocol over
Bluetooth Low Energy, such as the CITYSPORTS WP9 (`CITYSPORTS-LINKER`).
Requires Python >= 3.14.

> Unofficial and reverse-engineered. Not endorsed by CITYSPORTS or
> EQiSports. It can break when the firmware changes.

The protocol facts come from HCI captures of a CITYSPORTS WP9 1400W and
from the notes in
[Trught/eqisports_ble_protocol](https://github.com/Trught/eqisports_ble_protocol).
The library supports EFC only. ES and FTMS treadmills need another library,
for example [pyftms](https://github.com/dudanov/pyftms) for FTMS.

## Install

```bash
uv add citysports-efc-ble
```

## Usage

`EfcClient` accepts an injected `BleTransport`. Tests can use a fake
transport without Bluetooth hardware.

```python
import asyncio

from bleak import BleakScanner

from citysports_efc_ble import BleakTransport, EfcClient


async def main() -> None:
    device = await BleakScanner.find_device_by_address("AA:BB:CC:DD:EE:FF")
    if device is None:
        raise SystemExit("Treadmill not found")
    async with EfcClient(BleakTransport(device)) as client:
        print(client.state)


asyncio.run(main())
```

Inside Home Assistant, pass the `BLEDevice` from
`bluetooth.async_ble_device_from_address` instead of scanning.
`is_efc_advertisement(name, service_uuids)` returns True for the EFC
service UUID or a local name starting with `CITYSPORTS`.

`connect()` subscribes to notifications, sends the device info query and
returns once a device info frame and a status frame arrived. `client.status`
then reads `READY`. Every valid frame updates `client.state` and is passed
to the optional `update_callback` as an `EfcUpdate`.

`EfcState` holds speeds in km/h and distances in metres, also on imperial
units. The counters (`elapsed_seconds`, `distance_m`, `energy_kcal`,
`steps`) belong to one workout. The treadmill sends them only while
someone walks on the belt, freezes them in the summary state and resets
them in standby. The client continues elapsed time, steps and energy past
the 16-bit wraps the EQiSports app expects at 6000 s, 10000 steps and
1000 kcal.

### Controls

Control methods raise `EfcNotReadyError` unless the client was built with
`allow_control=True` and the session is `READY`. Without
`allow_control=True` they raise `EfcControlDisabledError`, a subclass of
`EfcNotReadyError`.

```python
async with EfcClient(BleakTransport(device), allow_control=True) as client:
    await client.start()
    await asyncio.sleep(4)
    await client.set_speed(3.0)
    await client.stop()
```

| Method | Value | Tested on a WP9 |
| --- | --- | --- |
| `start` | | yes |
| `stop` | | yes |
| `set_speed` | km/h within the reported range | yes |
| `pause` | | no |
| `resume` | | no |
| `set_incline` | whole percent within the reported range | no, the WP9 is flat |
| `request_sport_record` | | no |

`set_speed` ramps in 0.1 steps from the last reported speed while the belt
runs, one step every `ramp_interval_seconds` seconds (0.3 by default, 0.15 at
least), like the EQiSports app. A new control call cancels a running ramp.
When the belt is not running, the target is written once. The treadmill
starts the belt at its minimum speed after the countdown, whatever was
written before.

Speeds and inclines outside the range in the status frame raise
`EfcValidationError` before anything is sent. `connect()` raises
`EfcConnectionError` when the transport fails and `EfcTimeoutError`, also a
`TimeoutError`, when the treadmill does not answer. All exceptions derive
from `EfcError`.

## Connection loss

The client detects a dropped link through the disconnect callback of the
transport and through a failed write. A keepalive sends the device info
query every 30 seconds, because the treadmill can stay silent for close to
a minute in standby. When a session fails:

- pending control calls raise `EfcConnectionError`
- `client.status` becomes `ConnectionStatus.DISCONNECTED`
- an optional `connection_lost_callback` receives the exception

Call `connect()` again to start a new session.

## Protocol

| Direction | Frame | Meaning |
| --- | --- | --- |
| in | `1A 01 09` | speed range, incline range, speed, incline, status byte |
| in | `1A 02 0C` | elapsed s, distance m, energy 0.1 kcal, steps, heart rate |
| in | `1A 05 0C` | manufacturer, model, revision, system id |
| in | `1A 04 10` | sport record, workout counter at payload bytes 6 and 7 |
| out | `A1 05 00 A4` | device info query |
| out | `A1 03 01 01 A2` | start or resume |
| out | `A1 03 01 03 A0` | pause |
| out | `A1 03 01 05 A6` | stop |
| out | `A1 01 02 01 vv xx` | speed in 0.1 km/h (0.1 mph on imperial units) |
| out | `A1 02 02 01 vv xx` | incline in whole percent |

The last byte of every frame is the XOR of all earlier bytes. Writes go to
`ffeeddcc-bbaa-9988-7766-554433221101` with response, at least 150 ms
apart. Notifications arrive on `ffeeddcc-bbaa-9988-7766-554433221102`.
Bit 7 of the status byte marks imperial units and bits 0 to 4 hold the
workout state (`WorkoutState`) or a fault code (`EfcFault`).

## Probe

The developer probe logs raw frames through an ESPHome Bluetooth proxy, or
through the local Bluetooth adapter when `--proxy` is omitted. It never
sends control writes.

```bash
uv run python -m scripts.probe_efc --list-advertisements
uv run python -m scripts.probe_efc --address "AA:BB:CC:DD:EE:FF" \
  --capture-seconds 60 --output /tmp/efc.jsonl
```

Add `--proxy` and `--noise-psk` to go through an ESPHome proxy. The flags
fall back to the `EFC_PROXY`, `EFC_NOISE_PSK` and `EFC_DEVICE_ADDRESS`
environment variables. Capture files replace the system id with a fixed
fake address.

## Standalone library test

`scripts/drive_efc_client.py` drives the library against a real treadmill.
The default run is read-only.

```bash
uv run python -m scripts.drive_efc_client --address "AA:BB:CC:DD:EE:FF" --duration 30
```

Connection settings can live in the ignored file
`scripts/drive_efc_client.local.json`:

```json
{
  "proxy": "the-ip-of-your-esphome-bluetooth-proxy",
  "noise_psk": "the-encryption-key-of-your-esphome-bluetooth-proxy",
  "address": "AA:BB:CC:DD:EE:FF"
}
```

Controls need `--controls` and `--confirm-controls`. With them the script
starts the belt, ramps to `--speed`, waits `--run-seconds` and stops. Keep
the belt empty or stand on it yourself, and keep the safety key attached.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the gate and the pull
request rules.

```bash
uv sync
uv run python -m scripts.check
```

The gate runs a version drift check, format, Ruff, mypy, branch-covered
tests, coverage, `uv build --no-sources` and `uv audit` in that order.

## License

MIT. See [LICENSE](LICENSE).
