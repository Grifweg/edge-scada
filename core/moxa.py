"""
core/moxa.py — Moxa ioLogik digital output driver over Modbus TCP.

Hardware reference
──────────────────
  Device : Moxa ioLogik E1212 / E1214 (or compatible ioLogik series)
  Port   : 502 (Modbus TCP default)
  Unit ID: 1   (Moxa factory default; configurable on the device)

Modbus coil map  (Function Code 05 — Write Single Coil)
──────────────────
  DO0 → coil address 0  …  DO7 → coil address 7
  Coil value  0x0000 = de-energise  (False)
  Coil value  0xFF00 = energise     (True)

Connection model
──────────────────
  MoxaClient opens one TCP connection on first use and keeps it open for
  the lifetime of the instance.  Writes reuse that connection — no
  repeated connect/disconnect cycles that confuse the Moxa's TCP stack.

  On write failure the socket is closed and MoxaConnectionError is raised.
  The next set_output() call reconnects automatically.

  Call close() to release the connection explicitly (e.g. on shutdown or
  when the IP changes).

Protocol
──────────────────
  Native Modbus TCP — no third-party library.  Frame layout (12 bytes):
    MBAP header  TID(2) PID(2=0000) LEN(2=0006) UID(1)
    PDU          FC(1=05) ADDR(2) VALUE(2)

Error model
──────────────────
  All failures raise a MoxaError subclass.  A bounded TCP timeout
  (default 2 s) guarantees the call returns promptly when offline.
"""

from __future__ import annotations

import logging
import socket
import struct
from typing import Final

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_PORT    : Final[int]   = 502
_UNIT_ID : Final[int]   = 1
_TIMEOUT : Final[float] = 2.0
_DO_MIN  : Final[int]   = 0
_DO_MAX  : Final[int]   = 7
_TID     : Final[int]   = 1

# ── Exceptions ────────────────────────────────────────────────────────────────

class MoxaError(Exception):
    """Base class — catch this to handle any Moxa failure."""

class MoxaChannelError(MoxaError):
    """Channel number is outside the valid range DO0–DO7."""

class MoxaConnectionError(MoxaError):
    """TCP connection to the device could not be established or was lost."""

class MoxaResponseError(MoxaError):
    """Device returned an unexpected or Modbus exception response."""

# ── MoxaClient ────────────────────────────────────────────────────────────────

class MoxaClient:
    """
    Persistent Modbus TCP client for one Moxa ioLogik device.

    Connects on first set_output() call and reuses the socket for every
    subsequent write.  If the connection drops, the socket is discarded
    and MoxaConnectionError is raised; the next call reconnects.

    Args:
        ip:      Device IP address, e.g. '192.168.1.101'
        timeout: TCP connect + response timeout in seconds.  Default 2.0 s.
        unit_id: Modbus unit / slave ID.  Moxa factory default is 1.
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
        self._sock: socket.socket | None = None

    # ── Public ───────────────────────────────────────────────────────────────

    def set_output(self, channel: int, state: bool) -> None:
        """
        Energise or de-energise one digital output coil.

        Connects on first call; reuses the existing connection on subsequent
        calls.  Raises MoxaConnectionError if the write fails; the socket is
        discarded so the next call will reconnect.

        Args:
            channel: DO number, 0–7.
            state:   True = energise (ON), False = de-energise (OFF).
        """
        if not _DO_MIN <= channel <= _DO_MAX:
            raise MoxaChannelError(
                f'Channel {channel!r} is outside the valid range '
                f'DO{_DO_MIN}–DO{_DO_MAX}'
            )
        self._ensure_connected()
        try:
            self._send(channel, state)
        except OSError as exc:
            self.close()
            raise MoxaConnectionError(
                f'Connection to {self._ip} lost during write: {exc}'
            ) from exc
        log.info('Moxa  %-15s  DO%d = %s', self._ip, channel, 'ON' if state else 'OFF')

    def close(self) -> None:
        """Close the persistent socket.  Safe to call multiple times."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if self._sock is not None:
            return
        try:
            self._sock = socket.create_connection(
                (self._ip, _PORT), timeout=self._timeout
            )
        except OSError as exc:
            raise MoxaConnectionError(
                f'Cannot connect to {self._ip}:{_PORT}: {exc}'
            ) from exc

    def _send(self, channel: int, state: bool) -> None:
        assert self._sock is not None
        value = 0xFF00 if state else 0x0000
        pdu   = struct.pack('>BHH', 0x05, channel, value)
        mbap  = struct.pack('>HHHB', _TID, 0x0000, len(pdu) + 1, self._unit_id)
        self._sock.sendall(mbap + pdu)
        resp = _recvall(self._sock, 12)
        _check_response(resp, self._ip, channel)


# ── Module helpers ────────────────────────────────────────────────────────────

def _recvall(s: socket.socket, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise MoxaConnectionError('Connection closed by device during response')
        buf += chunk
    return buf


def _check_response(resp: bytes, ip: str, channel: int) -> None:
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
    """Write one coil without managing a MoxaClient instance (one-shot use)."""
    MoxaClient(ip, timeout=timeout, unit_id=unit_id).set_output(channel, state)
