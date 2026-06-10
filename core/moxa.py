"""
core/moxa.py — Moxa ioLogik digital output driver over Modbus TCP.

Hardware reference
──────────────────
  Device : Moxa ioLogik E1212 / E1214 (or compatible ioLogik series)
  Port   : 502 (Modbus TCP default)
  Unit ID: 1   (Moxa factory default; configurable on the device)

Modbus coil map  (Function Code 05 — Write Single Coil)
──────────────────
  DO0 → coil address 0
  DO1 → coil address 1
  …
  DO7 → coil address 7

  Coil value  0x0000 = de-energise  (False)
  Coil value  0xFF00 = energise     (True)
  (pymodbus translates Python bool to the correct wire values automatically)

Error model
──────────────────
  All failures raise a MoxaError subclass — never return a sentinel or
  silently swallow exceptions.  The calling code decides whether to log,
  retry, or degrade gracefully.  A bounded TCP timeout (default 2 s)
  guarantees the call returns promptly even when the device is offline.
"""

from __future__ import annotations

import logging
from typing import Final

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_PORT          : Final[int]   = 502
_UNIT_ID       : Final[int]   = 1      # Moxa ioLogik factory default
_TIMEOUT       : Final[float] = 2.0    # seconds — bounds connect AND receive
_DO_MIN        : Final[int]   = 0
_DO_MAX        : Final[int]   = 7

# ── Exceptions ────────────────────────────────────────────────────────────────

class MoxaError(Exception):
    """Base class — catch this to handle any Moxa failure."""

class MoxaChannelError(MoxaError):
    """Channel number is outside the valid range DO0–DO7.
    Raised before any network I/O — always a configuration error."""

class MoxaConnectionError(MoxaError):
    """TCP connection to the device could not be established.
    Indicates the device is offline, unreachable, or refusing connections."""

class MoxaResponseError(MoxaError):
    """Device is reachable but returned a Modbus exception response.
    Indicates a protocol-level refusal (address error, access denied, etc.)."""

# ── MoxaClient ────────────────────────────────────────────────────────────────

class MoxaClient:
    """
    Modbus TCP client for one Moxa ioLogik device.

    A new TCP connection is opened and closed on every call.  This is
    deliberate: Moxa ioLogik devices have a limited connection table, and
    writes are infrequent (state-change driven), so persistent connections
    would only add reconnect complexity for no real benefit.

    Args:
        ip:      Device IP address, e.g. '192.168.1.20'
        timeout: TCP connect + Modbus response timeout in seconds.
                 Bounds the worst-case blocking time when the device is
                 offline.  Default 2.0 s.
        unit_id: Modbus unit / slave ID.  Moxa factory default is 1.

    Raises:
        MoxaChannelError    — channel outside 0–7  (before any I/O)
        MoxaConnectionError — TCP connect failed
        MoxaResponseError   — device returned a Modbus exception
        MoxaError           — other I/O or protocol error
    """

    def __init__(
        self,
        ip:      str,
        *,
        timeout: float = _TIMEOUT,
        unit_id: int   = _UNIT_ID,
    ) -> None:
        self._ip      = ip
        self._timeout = timeout
        self._unit_id = unit_id

    # ── Public ───────────────────────────────────────────────────────────────

    def set_output(self, channel: int, state: bool) -> None:
        """
        Energise or de-energise one digital output coil.

        Args:
            channel: DO number, 0–7.
            state:   True = energise (ON), False = de-energise (OFF).

        Raises:
            MoxaChannelError    — channel outside 0–7
            MoxaConnectionError — device unreachable
            MoxaResponseError   — device refused the write
            MoxaError           — other I/O error
        """
        self._validate_channel(channel)
        self._write_coil(channel, state)
        log.info('Moxa  %-15s  DO%d = %s', self._ip, channel, 'ON' if state else 'OFF')

    # ── Internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_channel(channel: int) -> None:
        if not _DO_MIN <= channel <= _DO_MAX:
            raise MoxaChannelError(
                f'Channel {channel!r} is outside the valid range '
                f'DO{_DO_MIN}–DO{_DO_MAX}'
            )

    def _write_coil(self, channel: int, state: bool) -> None:
        """Open a TCP connection, write one coil, close the connection."""
        client = ModbusTcpClient(self._ip, port=_PORT, timeout=self._timeout)
        try:
            self._connect(client)
            self._send(client, channel, state)
        finally:
            client.close()

    def _connect(self, client: ModbusTcpClient) -> None:
        """Establish the TCP connection or raise MoxaConnectionError."""
        try:
            connected = client.connect()
        except (ConnectionException, ModbusException, OSError) as exc:
            raise MoxaConnectionError(
                f'Cannot connect to {self._ip}:{_PORT}: {exc}'
            ) from exc
        if not connected:
            raise MoxaConnectionError(
                f'Cannot connect to {self._ip}:{_PORT} '
                f'(device offline or port closed)'
            )

    def _send(self, client: ModbusTcpClient, channel: int, state: bool) -> None:
        """Write the coil and validate the Modbus response."""
        try:
            result = client.write_coil(channel, state, device_id=self._unit_id)
        except ConnectionException as exc:
            raise MoxaConnectionError(
                f'Connection to {self._ip} lost during write: {exc}'
            ) from exc
        except ModbusException as exc:
            raise MoxaError(
                f'Modbus protocol error on {self._ip}: {exc}'
            ) from exc
        except OSError as exc:
            raise MoxaError(
                f'OS error communicating with {self._ip}: {exc}'
            ) from exc

        if result is None or result.isError():
            raise MoxaResponseError(
                f'Device {self._ip} refused write to DO{channel}: {result}'
            )


# ── Module-level convenience function ─────────────────────────────────────────

def set_output(
    ip:      str,
    channel: int,
    state:   bool,
    *,
    timeout: float = _TIMEOUT,
    unit_id: int   = _UNIT_ID,
) -> None:
    """
    Write one Moxa digital output without managing a MoxaClient instance.

    Example::

        from core.moxa import set_output
        set_output('192.168.1.20', 0, True)   # energise DO0
        set_output('192.168.1.20', 3, False)  # de-energise DO3

    Raises:
        MoxaChannelError    — channel outside 0–7
        MoxaConnectionError — device unreachable
        MoxaResponseError   — device refused the write
        MoxaError           — other I/O error
    """
    MoxaClient(ip, timeout=timeout, unit_id=unit_id).set_output(channel, state)
