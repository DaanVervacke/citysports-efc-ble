API reference
=============

Import every name on this page from ``citysports_efc_ble``.

Client
------

.. autoclass:: citysports_efc_ble.EfcClient
   :members:
   :special-members: __init__

Timing defaults
---------------

Default values of the ``EfcClient`` timing options, in seconds.

.. autodata:: citysports_efc_ble.const.DEFAULT_RESPONSE_TIMEOUT_SECONDS

.. autodata:: citysports_efc_ble.const.DEFAULT_KEEPALIVE_SECONDS

.. autodata:: citysports_efc_ble.const.DEFAULT_WRITE_SPACING_SECONDS

.. autodata:: citysports_efc_ble.const.DEFAULT_RAMP_INTERVAL_SECONDS

.. autodata:: citysports_efc_ble.const.MIN_RAMP_INTERVAL_SECONDS

Transports
----------

.. autoclass:: citysports_efc_ble.BleTransport
   :members:

.. autoclass:: citysports_efc_ble.BleakTransport
   :members:
   :special-members: __init__

State
-----

.. autoclass:: citysports_efc_ble.EfcState
   :members:

.. autoclass:: citysports_efc_ble.EfcUpdate
   :members:

.. autoclass:: citysports_efc_ble.ConnectionStatus
   :members:
   :undoc-members:

.. autoclass:: citysports_efc_ble.WorkoutState
   :members:
   :undoc-members:

.. autoclass:: citysports_efc_ble.EfcFault
   :members:
   :undoc-members:

Frames
------

.. autofunction:: citysports_efc_ble.parse_frame

.. autodata:: citysports_efc_ble.models.EfcFrame

.. autoclass:: citysports_efc_ble.StatusFrame
   :members:

.. autoclass:: citysports_efc_ble.CountersFrame
   :members:

.. autoclass:: citysports_efc_ble.DeviceInfoFrame
   :members:

.. autoclass:: citysports_efc_ble.SportRecordFrame
   :members:

.. autoclass:: citysports_efc_ble.UnknownFrame
   :members:

.. autoclass:: citysports_efc_ble.CounterTracker
   :members:
   :special-members: __init__

Callbacks
---------

.. autodata:: citysports_efc_ble.client.NotificationCallback

.. autodata:: citysports_efc_ble.client.DisconnectedCallback

.. autodata:: citysports_efc_ble.client.UpdateCallback

.. autodata:: citysports_efc_ble.client.ConnectionLostCallback

Advertisements
--------------

.. autofunction:: citysports_efc_ble.is_efc_advertisement

Exceptions
----------

.. autoclass:: citysports_efc_ble.EfcError
   :members:
   :show-inheritance:

.. autoclass:: citysports_efc_ble.EfcConnectionError
   :members:
   :show-inheritance:

.. autoclass:: citysports_efc_ble.EfcNotReadyError
   :members:
   :show-inheritance:

.. autoclass:: citysports_efc_ble.EfcControlDisabledError
   :members:
   :show-inheritance:

.. autoclass:: citysports_efc_ble.EfcProtocolError
   :members:
   :show-inheritance:

.. autoclass:: citysports_efc_ble.EfcTimeoutError
   :members:
   :show-inheritance:

.. autoclass:: citysports_efc_ble.EfcValidationError
   :members:
   :show-inheritance:
