"""
core/plc.py — Omron FINS/TCP memory writer.

Uses FINS over TCP (port 9600) instead of UDP so the connection survives
the CJ2M's node-address protection that blocks raw UDP from unregistered nodes.

Protocol flow
─────────────
  1. TCP connect to PLC:9600
  2. Node Address Request  → PLC assigns us a client node number
  3. FINS Memory Area Write using the assigned node numbers
  4. TCP close

Frame structure (FINS/TCP)
──────────────────────────
  Every TCP message is prefixed with a 16-byte FINS/TCP header:
    [FINS][Length 4B][Command 4B][Error 4B][...data...]
  Commands:  0 = Node Address Request
             1 = Node Address Response
             2 = FINS command
             3 = FINS response
"""

from __future__ import annotations

import dataclasses
import logging
import re
import socket
import struct
from typing import Final, NamedTuple

log = logging.getLogger(__name__)

# ── Protocol constants ────────────────────────────────────────────────────────

_FINS_PORT      : Final[int]   = 9600
_DEFAULT_TIMEOUT: Final[float] = 2.0
_MAGIC          : Final[bytes] = b'FINS'

_MRC_WRITE: Final[int] = 0x01
_SRC_WRITE: Final[int] = 0x02
_MRC_READ : Final[int] = 0x01
_SRC_READ : Final[int] = 0x01

# ── Memory area registry ──────────────────────────────────────────────────────

class _AreaDef(NamedTuple):
    bit_code : int
    word_code: int

_AREAS: Final[dict[str, _AreaDef]] = {
    'CIO': _AreaDef(0x30, 0xB0),
    'W'  : _AreaDef(0x31, 0xB1),
    'H'  : _AreaDef(0x32, 0xB2),
    'A'  : _AreaDef(0x33, 0xB3),
    'D'  : _AreaDef(0x02, 0x82),
}

# ── End-code lookup ───────────────────────────────────────────────────────────

_END_CODES: Final[dict[tuple[int, int], str]] = {
    (0x01, 0x01): 'local node not in network',
    (0x02, 0x02): 'destination node not in network',
    (0x03, 0x01): 'communications controller error',
    (0x04, 0x01): 'destination node busy',
    (0x05, 0x01): 'response timeout at remote node',
    (0x10, 0x01): 'command too long',
    (0x11, 0x01): 'command too short',
    (0x11, 0x02): 'area mismatch',
    (0x21, 0x02): 'address range exceeded',
    (0x21, 0x08): 'no access rights (FINS protection active — check PLC Ethernet settings)',
    (0x22, 0x01): 'area is read-only',
    (0x25, 0x02): 'address out of range',
    (0x40, 0x01): 'service already processing',
}

# ── Exceptions ────────────────────────────────────────────────────────────────

class FINSError(Exception):
    """Base class for all FINS communication errors."""

class FINSTimeoutError(FINSError):
    """No TCP response received within the configured timeout."""

class FINSAddressError(FINSError):
    """Address string is malformed or references an unsupported area."""

class FINSResponseError(FINSError):
    """PLC returned a non-zero end-code, or the response is structurally invalid."""

# ── Parsed address types ──────────────────────────────────────────────────────

@dataclasses.dataclass(slots=True, frozen=True)
class _BitAddr:
    area: _AreaDef
    word: int
    bit:  int

@dataclasses.dataclass(slots=True, frozen=True)
class _WordAddr:
    area: _AreaDef
    word: int

# ── Address parsing ───────────────────────────────────────────────────────────

_BIT_PAT  = re.compile(r'^([A-Z]+)(\d+)\.(\d{1,2})$')
_WORD_PAT = re.compile(r'^([A-Z]+)(\d+)$')
_AREA_NAMES = ', '.join(_AREAS)


def _parse_bit(raw: str) -> _BitAddr:
    s = raw.strip().upper()
    m = _BIT_PAT.match(s)
    if not m:
        raise FINSAddressError(
            f"Cannot parse bit address {raw!r} — expected AREA WORD.BIT (e.g. W100.00)"
        )
    area_name, word_str, bit_str = m.groups()
    area = _AREAS.get(area_name)
    if area is None:
        raise FINSAddressError(f"Unknown area {area_name!r} in {raw!r} — supported: {_AREA_NAMES}")
    bit = int(bit_str)
    if not 0 <= bit <= 15:
        raise FINSAddressError(f"Bit position {bit} in {raw!r} out of range 0–15")
    return _BitAddr(area=area, word=int(word_str), bit=bit)


def _parse_word(raw: str) -> _WordAddr:
    s = raw.strip().upper()
    m = _WORD_PAT.match(s)
    if not m:
        raise FINSAddressError(
            f"Cannot parse word address {raw!r} — expected AREA WORD (e.g. D1200)"
        )
    area_name, word_str = m.groups()
    area = _AREAS.get(area_name)
    if area is None:
        raise FINSAddressError(f"Unknown area {area_name!r} in {raw!r} — supported: {_AREA_NAMES}")
    return _WordAddr(area=area, word=int(word_str))


# ── FINSClient ────────────────────────────────────────────────────────────────

class FINSClient:
    """
    FINS/TCP client for one Omron CJ2M PLC.

    Opens a new TCP connection per call (FINS/TCP stateless usage).
    The FINS/TCP node-address handshake is performed automatically so
    the PLC assigns us a valid source node — this bypasses the UDP
    node-address protection that causes error 0x2108.
    """

    def __init__(
        self,
        ip:      str,
        *,
        port:    int   = _FINS_PORT,
        timeout: float = _DEFAULT_TIMEOUT,
        node:    int | None = None,
    ) -> None:
        self._ip      = ip
        self._port    = port
        self._timeout = timeout
        self._node    = node       # if None, derived from TCP handshake
        self._sid     = 0

    # ── Public ───────────────────────────────────────────────────────────────

    def write_bit(self, addr: str, value: bool) -> None:
        """Write one bit.  addr format: 'AREA WORD.BIT', e.g. 'W100.00'."""
        a = _parse_bit(addr)
        cmd = bytes([
            _MRC_WRITE, _SRC_WRITE,
            a.area.bit_code,
            (a.word >> 8) & 0xFF,
            a.word        & 0xFF,
            a.bit,
            0x00, 0x01,
            0x01 if value else 0x00,
        ])
        self._transact(cmd)
        log.info('FINS  %-15s  %s = %s', self._ip, addr, 'ON' if value else 'OFF')

    def write_word(self, addr: str, value: int) -> None:
        """Write one 16-bit word.  addr format: 'AREA WORD', e.g. 'D1200'."""
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f'Word value {value} is outside 0–65535')
        a = _parse_word(addr)
        cmd = bytes([
            _MRC_WRITE, _SRC_WRITE,
            a.area.word_code,
            (a.word  >> 8) & 0xFF,
            a.word         & 0xFF,
            0x00,
            0x00, 0x01,
            (value >> 8) & 0xFF,
            value        & 0xFF,
        ])
        self._transact(cmd)
        log.info('FINS  %-15s  %s = %d (0x%04X)', self._ip, addr, value, value)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _transact(self, command: bytes) -> None:
        sid = self._next_sid()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            try:
                sock.connect((self._ip, self._port))
            except OSError as exc:
                raise FINSError(f'Cannot connect to {self._ip}:{self._port}: {exc}') from exc

            client_node, server_node = self._handshake(sock)
            da1 = self._node if self._node is not None else server_node

            header     = self._build_header(sid, client_node, da1)
            fins_frame = header + command
            tcp_pkt    = struct.pack('>4sIII', _MAGIC, 8 + len(fins_frame), 2, 0) + fins_frame
            sock.sendall(tcp_pkt)

            # Read FINS/TCP response header (16 bytes)
            tcp_resp = self._recv_exact(sock, 16)
            magic, length, _, tcp_err = struct.unpack('>4sIII', tcp_resp)
            if magic != _MAGIC:
                raise FINSResponseError(f'Invalid FINS/TCP magic from {self._ip}')
            if tcp_err:
                raise FINSResponseError(
                    f'FINS/TCP transport error 0x{tcp_err:08X} from {self._ip}'
                )

            fins_resp = self._recv_exact(sock, length - 8)
            self._validate(fins_resp, sid)

        except socket.timeout:
            raise FINSTimeoutError(
                f'No response from {self._ip}:{self._port} within {self._timeout}s'
            ) from None
        finally:
            sock.close()

    def _handshake(self, sock: socket.socket) -> tuple[int, int]:
        """FINS/TCP node-address handshake.  Returns (client_node, server_node)."""
        req = struct.pack('>4sIIII', _MAGIC, 12, 0, 0, 0)  # client node = 0 → auto-assign
        sock.sendall(req)

        resp = self._recv_exact(sock, 24)
        # CJ2M sends [client_node][server_node] — opposite of the spec wording.
        # Empirically: last value matches last IP octet (= PLC FINS node).
        magic, length, cmd, error, client_node, server_node = struct.unpack('>4sIIIII', resp)

        if magic != _MAGIC or cmd != 1:
            raise FINSError(
                f'FINS/TCP handshake from {self._ip} returned unexpected response '
                f'(cmd=0x{cmd:08X})'
            )
        if error:
            raise FINSError(
                f'FINS/TCP handshake error 0x{error:08X} from {self._ip}'
            )
        log.debug('FINS/TCP  %s  server_node=%d  client_node=%d', self._ip, server_node, client_node)
        return client_node, server_node

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise FINSError(f'Connection closed by {self._ip} during receive')
            buf += chunk
        return buf

    def _build_header(self, sid: int, sa1: int, da1: int) -> bytes:
        return bytes([
            0x80,  # ICF
            0x00,  # RSV
            0x02,  # GCT
            0x00,  # DNA
            da1,   # DA1 — PLC node (from handshake)
            0x00,  # DA2
            0x00,  # SNA
            sa1,   # SA1 — our node (assigned by handshake)
            0x00,  # SA2
            sid,   # SID
        ])

    def _validate(self, resp: bytes, expected_sid: int) -> None:
        if len(resp) < 14:
            raise FINSResponseError(
                f'Response from {self._ip} too short: {len(resp)} bytes'
            )
        if resp[9] != expected_sid:
            raise FINSResponseError(
                f'SID mismatch from {self._ip}: sent {expected_sid}, got {resp[9]}'
            )
        mres, sres = resp[12], resp[13]
        if mres or sres:
            desc = _END_CODES.get((mres, sres), 'see Omron FINS manual')
            raise FINSResponseError(
                f'FINS end-code from {self._ip}: '
                f'MRES=0x{mres:02X} SRES=0x{sres:02X} — {desc}'
            )

    def _next_sid(self) -> int:
        self._sid = (self._sid % 255) + 1
        return self._sid


# ── Module-level convenience functions ───────────────────────────────────────

def write_bit(
    ip: str, addr: str, value: bool,
    *, port: int = _FINS_PORT, timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    FINSClient(ip, port=port, timeout=timeout).write_bit(addr, value)


def write_word(
    ip: str, addr: str, value: int,
    *, port: int = _FINS_PORT, timeout: float = _DEFAULT_TIMEOUT,
) -> None:
    FINSClient(ip, port=port, timeout=timeout).write_word(addr, value)
