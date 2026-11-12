"""Deadline overrun probe: hostile TLS receivers vs webhooks._attempt (real transport, no DB).

Each case reports wall-clock of asyncio.run(_attempt(...)) incl. loop teardown, and thread/fd deltas.
"""
import asyncio, os, socket, ssl, sys, tempfile, threading, time, pathlib
sys.path.insert(0, "/home/user/oceandata/backend")
sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
sys.path.insert(0, "/home/user/oceandata/backend")
from app.services import webhooks
from tests.conftest import test_settings
import test_webhook_send as T

TIMEOUT = 1.5
tmp = pathlib.Path(tempfile.mkdtemp())
server_ctx, trusted = T.self_signed(tmp, "hooks.example.test")
webhooks._tls_context.cache_clear()
webhooks._tls_context = lambda: trusted          # trust the synthetic cert
webhooks._lookup = lambda host, port: ["127.0.0.1"]   # name -> loopback (allow_private=True)

S = test_settings(webhook_allow_private_targets=True, webhook_timeout_s=TIMEOUT)

def nfd(): return len(os.listdir("/proc/self/fd"))

closed_log = {}

def mk(handler, tls=True):
    return T.SyntheticReceiver(handler, tls=server_ctx if tls else None)

# ---- hostile handlers (conn is already TLS-wrapped for tls=True cases) ----
def h_ok(conn, seen):
    seen.append(T.read_request(conn)); conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

def h_keepalive_204(conn, seen):
    seen.append(T.read_request(conn))
    conn.sendall(b"HTTP/1.1 204 No Content\r\nConnection: keep-alive\r\nKeep-Alive: timeout=60\r\n\r\n")
    t0 = time.monotonic(); conn.settimeout(10)
    try:
        while conn.recv(65536): pass
    except Exception: pass
    closed_log["keepalive_closed_after_s"] = round(time.monotonic() - t0, 3)

def h_flood_100(conn, seen):
    seen.append(T.read_request(conn)); conn.settimeout(20)
    end = time.monotonic() + 15
    msg = b"HTTP/1.1 100 Continue\r\n\r\n" * 2000
    while time.monotonic() < end: conn.sendall(msg)

def h_trickle_103(conn, seen):
    seen.append(T.read_request(conn))
    for _ in range(300):
        conn.sendall(b"HTTP/1.1 103 Early Hints\r\nLink: </x>; rel=preload\r\n\r\n"); time.sleep(0.05)

def h_huge_header(conn, seen):
    seen.append(T.read_request(conn)); conn.sendall(b"HTTP/1.1 200 OK\r\nX: ")
    chunk = b"a" * 65536
    for _ in range(200): conn.sendall(chunk)

def h_chunked_trickle(conn, seen):
    seen.append(T.read_request(conn))
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    t0 = time.monotonic(); conn.settimeout(0.2)
    for _ in range(100):
        try: conn.sendall(b"1\r\nx\r\n")
        except Exception: break
        time.sleep(0.1)
    closed_log["chunked_server_loop_s"] = round(time.monotonic() - t0, 3)

def h_stall_tls_after_accept(conn, seen):  # (used with tls=False: raw socket, never answers TLS)
    conn.settimeout(20)
    try:
        while conn.recv(65536): pass
    except Exception: pass

def h_tls_record_trickle(conn, seen):      # tls=False: raw; read ClientHello then trickle a TLS record
    conn.settimeout(20)
    conn.recv(65536)
    conn.sendall(b"\x16\x03\x03\x00\x50")   # handshake record header, 80 bytes promised
    for _ in range(79):
        time.sleep(0.2); conn.sendall(b"\x02")

def h_redirect(conn, seen):
    seen.append(T.read_request(conn)); conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1:1/\r\nContent-Length: 0\r\n\r\n")

def h_101(conn, seen):
    seen.append(T.read_request(conn)); conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: x\r\nConnection: Upgrade\r\n\r\n")
    time.sleep(3)

def h_garbage(conn, seen):
    seen.append(T.read_request(conn)); conn.sendall(b"\x00\x01garbage\r\n\r\n")

cases = [
    ("ok-204", h_ok, True), ("keepalive-204", h_keepalive_204, True), ("flood-100-continue", h_flood_100, True),
    ("trickle-103", h_trickle_103, True), ("huge-header-fast", h_huge_header, True), ("chunked-trickle-body", h_chunked_trickle, True),
    ("stall-before-tls", h_stall_tls_after_accept, False), ("tls-record-trickle", h_tls_record_trickle, False),
    ("redirect-302", h_redirect, True), ("101-upgrade", h_101, True), ("garbage", h_garbage, True),
]

def run_case(name, handler, tls):
    rx = mk(handler, tls)
    cl = webhooks._Claim("d", "w", 1, f"https://hooks.example.test:{rx.port}/h", b'{"a":1}', {"Content-Type": "application/json"})
    th0, fd0 = threading.active_count(), nfd()
    t0 = time.monotonic()
    res = asyncio.run(webhooks._attempt(cl, S, None))
    el = time.monotonic() - t0
    time.sleep(0.4)  # let server-side observe closes
    print(f"{name:24s} result={res!s:30.30} elapsed={el:5.2f}s (limit {TIMEOUT}) threads {th0}->{threading.active_count()} fds {fd0}->{nfd()}")
    rx.close()

for name, h, tls in cases:
    run_case(name, h, tls)
print(closed_log)
