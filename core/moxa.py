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

Protocol
──────────────────
  Native Modbus TCP — no third-party library.  Each write opens one TCP
  connection, sends a 12-byte MBAP-framed FC-05 request, reads the 12-byte
  echo response, then exits the context manager.  Python's socket context
  manager sends FIN on exit so the Moxa frees the connection slot immediately.
  No reconnect tasks or library buffers are left open after the call returns.

  Frame layout (12 bytes):
    MBAP header  TID(2) PID(2=0000) LEN(2=0006) UID(1)
    PDU          FC(1=05) ADDR(2) VALUE(2)

Error model
──────────────────
  All failures raise a MoxaError subclass — never return a sentinel or
  silently swallow exceptions.  A bounded TCP timeout (default 2 s)
  guarantees the call returns promptly even when the device is offline.
"""

from __future__ import annotations

import logging
import socket
import struct
from typing import Final

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_PORT    : Final[int]   = 502
_UNIT_ID : Final[int]   = 1      # Moxa ioLogik factory default
_TIMEOUT : Final[float] = 2.0    # seconds — bounds connect AND receive
_DO_MIN  : Final[int]   = 0
_DO_MAX  : Final[int]   = 7
_TID     : Final[int]   = 1      # Modbus TCP Transaction ID (fixed; fresh socket per call)

# ── Exceptions ────────────────────────────────────────────────────────────────

class MoxaError(Exception):
    """Base class — catch this to handle any Moxa failure."""

class MoxaChannelError(MoxaError):
    """Channel number is outside the valid range DO0–DO7.
    Raised before any network I/O — always a configuration error."""

class MoxaConnectionError(MoxaError):
    """TCP connection to the device could not be established or was lost."""

class MoxaResponseError(MoxaError):
    """Device returned an unexpected or Modbus exception response."""

# ── MoxaClient ────────────────────────────────────────────────────────────────

class MoxaClient:
    """
    Modbus TCP client for one Moxa ioLogik device.

    Opens a fresh TCP connection for every write, sends the FC-05 frame,
    reads the echo response, then closes the socket.  The OS sends FIN on
    socket close, freeing the Moxa's connection slot immediately — no
    reconnect background tasks are left running.

    Args:
        ip:      Device IP address, e.g. '192.168.1.101'
        timeout: TCP connect + response timeout in seconds.  Default 2.0 s.
        unit_id: Modbus unit / slave ID.  Moxa factory default is 1.

    Raises:
        MoxaChannelError    — channel outside 0–7  (before any I/O)
        MoxaConnectionError — TCP connect or I/O error
        MoxaResponseError   — device returned a Modbus exception
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
        """
        if not _DO_MIN <= channel <= _DO_MAX:
            raise MoxaChannelError(
                f'Channel {channel!r} is outside the valid range '
                f'DO{_DO_MIN}–DO{_DO_MAX}'
            )
        self._write_coil(channel, state)
        log.info('Moxa  %-15s  DO%d = %s', self._ip, channel, 'ON' if state else 'OFF')

    # ── Internals ─────────────────────────────────────────────────────────────

    def _write_coil(self, channel: int, state: bool) -> None:
        value = 0xFF00 if state else 0x0000
        pdu   = struct.pack('>BHH', 0x05, channel, value)
        mbap  = struct.pack('>HHHB', _TID, 0x0000, len(pdu) + 1, self._unit_id)
        req   = mbap + pdu

        try:
            with socket.create_connection(
                (self._ip, _PORT), timeout=self._timeout
            ) as s:
                # RST on close so the Moxa frees its connection-table slot
                # immediately.  Without this the Moxa stays in CLOSE_WAIT and
                # refuses new connections from this IP until an internal
                # timeout expires (observed behaviour with ioLogik firmware).
                s.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack('ii', 1, 0),
                )
                s.sendall(req)
                resp = _recvall(s, 12)
        except MoxaError:
            raise
        except OSError as exc:
            raise MoxaConnectionError(
                f'Cannot communicate with {self._ip}:{_PORT}: {exc}'
            ) from exc

        _check_response(resp, self._ip, channel)


# ── Module helpers ────────────────────────────────────────────────────────────

def _recvall(s: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from s; raise MoxaConnectionError on early EOF."""
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise MoxaConnectionError('Connection closed by device during response')
        buf += chunk
    return buf


def _check_response(resp: bytes, ip: str, channel: int) -> None:
    """Validate the FC-05 echo response."""
    fc = resp[7]
    if fc & 0x80:
        ex_code = resp[8] if len(resp) > 8 else '?'
        raise MoxaResponseError(
            f'Device {ip} returned Modbus exception FC={fc:#04x} code={ex_code}'
        )
    if fc != 0x05:
        raise MoxaResponseError(
            f'Device {ip} returned unexpected function code {fc:#04x}'
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
        set_output('192.168.1.101', 0, True)   # energise DO0
        set_output('192.168.1.101', 3, False)  # de-energise DO3
    """
    MoxaClient(ip, timeout=timeout, unit_id=unit_id).set_output(channel, state)
