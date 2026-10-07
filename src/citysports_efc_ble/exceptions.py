"""Exceptions raised by the EFC client.

Every exception derives from ``EfcError``. ``EfcTimeoutError`` is also a
``TimeoutError`` and ``EfcValidationError`` is also a ``ValueError``.
"""

__all__ = [
    "EfcConnectionError",
    "EfcControlDisabledError",
    "EfcError",
    "EfcNotReadyError",
    "EfcProtocolError",
    "EfcTimeoutError",
    "EfcValidationError",
]


class EfcError(Exception):
    """Base class for every error this library raises.

    Catch it to handle any EFC failure in one place.
    """


class EfcConnectionError(EfcError):
    """The session failed, or a write could not be sent.

    The original transport error is chained as ``__cause__``.
    """


class EfcTimeoutError(EfcError, TimeoutError):
    """The treadmill did not answer before the timeout."""


class EfcProtocolError(EfcError):
    """A frame is malformed or has an invalid checksum."""


class EfcValidationError(EfcError, ValueError):
    """An argument is outside the supported range or has the wrong type."""


class EfcNotReadyError(EfcError):
    """Controls are disabled or the session is not ``READY``."""


class EfcControlDisabledError(EfcNotReadyError):
    """The client was created with ``allow_control`` set to False."""
