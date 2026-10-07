"""EFC exceptions."""


class EfcError(Exception):
    """Base EFC error."""


class EfcConnectionError(EfcError):
    """The session failed, or a write could not be sent."""


class EfcTimeoutError(EfcError):
    """The treadmill did not answer before the timeout."""


class EfcProtocolError(EfcError):
    """A frame is malformed or has an invalid checksum."""


class EfcValidationError(EfcError, ValueError):
    """A command argument is outside the supported range."""


class EfcNotReadyError(EfcError):
    """Controls are disabled or the session is not ``READY``."""
