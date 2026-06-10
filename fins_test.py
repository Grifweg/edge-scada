"""Quick FINS/TCP diagnostic — run: python fins_test.py"""
import socket, struct

PLC_IP   = '192.168.1.80'
PLC_PORT = 9600
MAGIC    = b'FINS'


def recv_exact(s, n):
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise RuntimeError('Connection closed')
        buf += chunk
    return buf


def transact(sock, client_node, server_node, fins_cmd, sid):
    header = bytes([0x80,0x00,0x02, 0x00,server_node,0x00, 0x00,client_node,0x00, sid])
    frame  = header + fins_cmd
    pkt    = struct.pack('>4sIII', MAGIC, 8+len(frame), 2, 0) + frame
    print(f'  TX ({len(pkt)}B): {pkt.hex(" ")}')
    sock.sendall(pkt)
    hdr  = recv_exact(sock, 16)
    _, length, _, err = struct.unpack('>4sIII', hdr)
    resp = recv_exact(sock, length - 8)
    print(f'  RX ({len(resp)}B): {resp.hex(" ")}')
    mres, sres = resp[12], resp[13]
    ok = mres == 0 and sres == 0
    print(f'  End code: MRES=0x{mres:02X} SRES=0x{sres:02X} -> {"OK" if ok else "ERROR"}')
    return resp, ok


with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.settimeout(3.0)
    print(f'Connecting to {PLC_IP}:{PLC_PORT} ...')
    s.connect((PLC_IP, PLC_PORT))
    print('Connected')

    # ── Handshake ────────────────────────────────────────────────────────────
    req = struct.pack('>4sIIII', MAGIC, 12, 0, 0, 0)
    s.sendall(req)
    hs = recv_exact(s, 24)
    _, _, _, _, cli_node, srv_node = struct.unpack('>4sIIIII', hs)
    print(f'Handshake: server_node={srv_node} (DA1)  client_node={cli_node} (SA1)')

    # ── Test 1: Read D0 ───────────────────────────────────────────────────────
    print('\nTEST 1 — Read D0 (1 word)')
    read_cmd = bytes([0x01,0x01, 0x82, 0x00,0x00, 0x00, 0x00,0x01])
    resp, ok = transact(s, cli_node, srv_node, read_cmd, 1)
    if ok and len(resp) >= 16:
        print(f'  D0 = {(resp[14]<<8)|resp[15]}')

    # ── Test 2: Write D1200 = 1 ──────────────────────────────────────────────
    print('\nTEST 2 — Write D1200 = 1')
    w1 = bytes([0x01,0x02, 0x82, 0x04,0xB0, 0x00, 0x00,0x01, 0x00,0x01])
    transact(s, cli_node, srv_node, w1, 2)

    # ── Test 3: Write D1200 = 0 ──────────────────────────────────────────────
    print('\nTEST 3 — Write D1200 = 0 (reset)')
    w0 = bytes([0x01,0x02, 0x82, 0x04,0xB0, 0x00, 0x00,0x01, 0x00,0x00])
    transact(s, cli_node, srv_node, w0, 3)
