citysports-efc-ble
==================

Async Python library for treadmills that speak the EFC protocol over
Bluetooth Low Energy, such as the CITYSPORTS WP9. Requires Python 3.14 or
newer.

The library is unofficial and reverse-engineered. It is not endorsed by
CITYSPORTS or EQiSports and can break when the firmware changes.

Install
-------

.. code-block:: bash

   uv add citysports-efc-ble

Usage
-----

:class:`~citysports_efc_ble.EfcClient` takes a
:class:`~citysports_efc_ble.BleTransport`.
:class:`~citysports_efc_ble.BleakTransport` wraps a Bleak ``BLEDevice``.
Tests can pass a fake transport instead.

.. code-block:: python

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

``connect()`` returns once a device info frame and a status frame arrived
and the session is :attr:`~citysports_efc_ble.ConnectionStatus.READY`.
Every valid frame updates ``client.state`` and is passed to the optional
``update_callback`` as an :class:`~citysports_efc_ble.EfcUpdate`.

Controls
--------

Control methods raise :class:`~citysports_efc_ble.EfcNotReadyError` unless
the client was built with ``allow_control=True`` and the session is
``READY``.

.. code-block:: python

   async with EfcClient(BleakTransport(device), allow_control=True) as client:
       await client.start()
       await asyncio.sleep(4)
       await client.set_speed(3.0)
       await client.stop()

``set_speed`` ramps in 0.1 steps from the last reported speed while the
belt runs, one step every ``ramp_interval`` seconds. A new control call
cancels a running ramp. ``pause``, ``resume``, ``set_incline`` and
``request_sport_record`` come from the EQiSports app and are untested on
real hardware.

Connection loss
---------------

The client detects a dropped link through the disconnect callback of the
transport and through a failed write. A keepalive sends the device info
query every 30 seconds. When a session fails:

- pending control calls raise
  :class:`~citysports_efc_ble.EfcConnectionError`
- ``client.status`` becomes ``DISCONNECTED``
- the optional ``connection_lost_callback`` receives the exception

Call ``connect()`` again to start a new session.

.. toctree::
   :maxdepth: 2

   api
