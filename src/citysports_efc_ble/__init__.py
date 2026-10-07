"""Python library for EFC treadmills over Bluetooth Low Energy."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

from .client import ConnectionLostCallback, EfcClient, UpdateCallback
from .const import (
    DEFAULT_KEEPALIVE_SECONDS,
    DEFAULT_RAMP_INTERVAL_SECONDS,
    DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    DEFAULT_WRITE_SPACING_SECONDS,
    MIN_RAMP_INTERVAL_SECONDS,
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
    DeviceInfoFrame,
    EfcFault,
    EfcFrame,
    EfcState,
    EfcUpdate,
    SportRecordFrame,
    StatusFrame,
    UnknownFrame,
    WorkoutState,
)
from .protocol import is_efc_advertisement, parse_frame
from .transport import BleakTransport
from .transport_types import BleTransport, DisconnectedCallback, NotificationCallback

try:
    __version__ = _version("citysports-efc-ble")
except _PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_KEEPALIVE_SECONDS",
    "DEFAULT_RAMP_INTERVAL_SECONDS",
    "DEFAULT_RESPONSE_TIMEOUT_SECONDS",
    "DEFAULT_WRITE_SPACING_SECONDS",
    "MIN_RAMP_INTERVAL_SECONDS",
    "BleTransport",
    "BleakTransport",
    "ConnectionLostCallback",
    "ConnectionStatus",
    "CounterTracker",
    "CountersFrame",
    "DeviceInfoFrame",
    "DisconnectedCallback",
    "EfcClient",
    "EfcConnectionError",
    "EfcControlDisabledError",
    "EfcError",
    "EfcFault",
    "EfcFrame",
    "EfcNotReadyError",
    "EfcProtocolError",
    "EfcState",
    "EfcTimeoutError",
    "EfcUpdate",
    "EfcValidationError",
    "NotificationCallback",
    "SportRecordFrame",
    "StatusFrame",
    "UnknownFrame",
    "UpdateCallback",
    "WorkoutState",
    "__version__",
    "is_efc_advertisement",
    "parse_frame",
]
