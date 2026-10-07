"""Shared fixtures for the EFC test suite."""

from collections.abc import AsyncIterator

import pytest
from citysports_efc_ble import EfcClient

from .helpers import FakeTransport, make_client


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
async def connected_client(transport: FakeTransport) -> AsyncIterator[EfcClient]:
    client = make_client(transport)
    await client.connect()
    yield client
    await client.disconnect()
