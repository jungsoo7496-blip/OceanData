import sys, socket, ssl, tempfile, pathlib, threading, time
sys.path.insert(0, "/home/user/oceandata/backend"); sys.path.insert(0, "/home/user/oceandata/backend/tests/unit")
import httpx
from app.services import webhooks
import test_webhook_send as T

u = httpx.URL("https://hooks.example.test:8443/h?x=1")
for a in ("2606:4700:4700::1111", "::1", "93.184.216.34", "fe80::1%eth0"):
    try:
        print(a, "->", str(u.copy_with(host=a)), u.copy_with(host=a).host, u.copy_with(host=a).netloc)
    except Exception as e:
        print(a, "-> FAIL", type(e).__name__, e)

# real IPv6 TLS connection with pinned address
tmp = pathlib.Path(tempfile.mkdtemp())
server_ctx, trusted = T.self_signed(tmp, "hooks.example.test")
srv = socket.create_server(("::1", 0), family=socket.AF_INET6)
port = srv.getsockname()[1]
seen = []
def serve():
    c, _ = srv.accept()
    c = server_ctx.wrap_socket(c, server_side=True)
    seen.append(T.read_request(c)); c.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"); c.close()
threading.Thread(target=serve, daemon=True).start()
try:
    print("v6 send ->", webhooks.http_send(f"https://hooks.example.test:{port}/h", b"{}", {}, 2.0, addresses=["::1"], verify=trusted))
    print(seen[0].split(b"\r\n")[:3])
except Exception as e:
    print("v6 send FAIL", type(e).__name__, e)
