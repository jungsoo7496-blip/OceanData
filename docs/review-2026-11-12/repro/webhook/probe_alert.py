"""Operator alert webhook (monitoring.post_webhook): hostile endpoints vs the 5 s total deadline."""
import os, sys, time, threading
sys.path.insert(0, "/home/user/oceandata/backend"); sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
os.environ["NO_PROXY"] = "127.0.0.1"; os.environ["no_proxy"] = "127.0.0.1"
for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
from datetime import datetime, UTC
from app.services import monitoring
import test_webhook_send as T
monitoring._ALERT_WEBHOOK_TIMEOUT_S = 1.0

def flood100(conn, seen):
    seen.append(T.read_request(conn)); conn.settimeout(20)
    end = time.monotonic() + 8; msg = b"HTTP/1.1 100 Continue\r\n\r\n" * 2000
    while time.monotonic() < end: conn.sendall(msg)

def stall_raw(conn, seen):
    conn.settimeout(20)
    try:
        while conn.recv(65536): pass
    except Exception: pass

def keepalive_then_hang_close(conn, seen):
    seen.append(T.read_request(conn)); conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    conn.settimeout(5)
    try:
        while conn.recv(65536): pass
    except Exception: pass

cases = [("flood-100 (http)", flood100, "http"), ("stall-before-TLS (https)", stall_raw, "https"),
         ("200 keepalive (http)", keepalive_then_hang_close, "http")]
for name, h, scheme in cases:
    rx = T.SyntheticReceiver(h)
    t0 = time.monotonic()
    monitoring.post_webhook(f"{scheme}://127.0.0.1:{rx.port}/ops", "k", {"type": "watch.opened"}, datetime.now(UTC))
    print(f"{name:28s} returned in {time.monotonic()-t0:5.2f}s (limit 1.0)")
    rx.close()

# DNS capacity: hold 4 slots with hung lookups, then see what the next alert does
import socket
orig = socket.getaddrinfo
gate = threading.Event()
def slow_dns(host, *a, **k):
    if isinstance(host, str) and host.endswith(".hang.test"):
        gate.wait(6); raise socket.gaierror(socket.EAI_NONAME, "hung")
    return orig(host, *a, **k)
socket.getaddrinfo = slow_dns
for i in range(5):
    t0 = time.monotonic()
    monitoring.post_webhook(f"https://h{i}.hang.test/ops", None, {"type": "x"}, datetime.now(UTC))
    print(f"alert #{i} with hung DNS returned in {time.monotonic()-t0:5.2f}s")
print("slots free now:", monitoring._ALERT_DNS_SLOTS._value)
gate.set(); time.sleep(0.5)
print("slots free after DNS threads end:", monitoring._ALERT_DNS_SLOTS._value)
