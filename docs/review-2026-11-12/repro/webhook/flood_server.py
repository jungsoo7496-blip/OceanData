import socket, sys, threading, time
pf = sys.argv[1]
srv = socket.create_server(("127.0.0.1", 0)); open(pf, "w").write(str(srv.getsockname()[1]))
def h(c):
    try:
        data = b""
        while b"\r\n\r\n" not in data: data += c.recv(65536)
        end = time.monotonic() + 12; msg = b"HTTP/1.1 100 Continue\r\n\r\n" * 4000
        while time.monotonic() < end: c.sendall(msg)
    except Exception: pass
while True:
    c, _ = srv.accept(); threading.Thread(target=h, args=(c,), daemon=True).start()
