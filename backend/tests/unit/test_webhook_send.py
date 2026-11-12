"""04번 웹훅 전송 (ADR-0041): 전체 기한·상태 코드만·주소 고정·프록시 무시·대상 검사(SSRF).

DB 없이 127.0.0.1의 합성 수신 서버로만 확인한다 (밖으로 나가지 않는다). 인증서는 시험 안에서 만든 자체 서명이다.
"""

from __future__ import annotations

import datetime as dt
import gc
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.services import webhooks

BODY, HEADERS = b'{"event_id":"synthetic"}', {"Content-Type": "application/json"}
MARGIN_S = 0.75  # 기한을 넘겨도 이만큼 안에는 돌아와야 한다 (느린 CI 여유)
Handler = Callable[[socket.socket, list[bytes]], None]


class SyntheticReceiver:
    """합성 수신 서버. 연결마다 handler(conn, seen)를 데몬 스레드로 돌린다. tls를 주면 TLS로 감싼다."""

    def __init__(self, handler: Handler, tls: ssl.SSLContext | None = None) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port: int = self.sock.getsockname()[1]
        self.seen: list[bytes] = []
        self.snis: list[str | None] = []
        self.accepted = threading.Event()
        if tls is not None:
            tls.sni_callback = lambda _s, name, _c: self.snis.append(name)
        self._handler, self._tls = handler, tls
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.set()
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        try:
            if self._tls is not None:
                conn = self._tls.wrap_socket(conn, server_side=True)
            self._handler(conn, self.seen)
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()


def read_request(conn: socket.socket) -> bytes:
    """요청을 끝까지(헤더 + Content-Length 본문) 읽는다. 덜 읽고 닫으면 RST가 나가 클라이언트가 응답을 못 읽는다."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), 0)
    while len(body) < length:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def respond_204(conn: socket.socket, seen: list[bytes]) -> None:
    seen.append(read_request(conn))
    conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")


def drip_headers(conn: socket.socket, seen: list[bytes]) -> None:
    """상태 줄만 보내고 헤더를 0.2초에 한 바이트씩 — 읽기마다의 timeout으로는 끝나지 않는다."""
    seen.append(read_request(conn))
    conn.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
    for _ in range(150):
        time.sleep(0.2)
        conn.sendall(b"a")


def drip_body(conn: socket.socket, seen: list[bytes]) -> None:
    """검토에서 재현한 경우: 200 헤더는 바로, 본문은 0.2초에 한 바이트씩 끝없이 (예전에는 이 동안 워커가 멈췄다)."""
    seen.append(read_request(conn))
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\nContent-Type: text/plain\r\n\r\n")
    for _ in range(150):
        time.sleep(0.2)
        conn.sendall(b"x")


def silent(conn: socket.socket, seen: list[bytes]) -> None:
    """요청을 읽고 아무 응답도 하지 않는다 (클라이언트가 끊을 때까지, 길어야 30초 붙잡는다)."""
    seen.append(read_request(conn))
    conn.settimeout(30)
    while conn.recv(65536):
        pass


def stall_before_tls(conn: socket.socket, seen: list[bytes]) -> None:
    """TCP 연결은 받고 TLS 핸드셰이크에 답하지 않는다. 클라이언트가 소켓을 닫으면 EOF를 남긴다."""
    conn.settimeout(10)
    try:
        while conn.recv(65536):
            pass
        seen.append(b"EOF")
    except OSError:
        pass


@pytest.fixture()
def receiver() -> Iterator[Callable[..., SyntheticReceiver]]:
    made: list[SyntheticReceiver] = []

    def make(handler: Handler, tls: ssl.SSLContext | None = None) -> SyntheticReceiver:
        made.append(SyntheticReceiver(handler, tls))
        return made[-1]

    yield make
    for r in made:
        r.close()


def self_signed(tmp: Path, name: str) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """name용 자체 서명 인증서 → (서버 TLS 설정, 그 인증서만 믿는 클라이언트 TLS 설정)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp / f"{name}.crt", tmp / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    return server, ssl.create_default_context(cafile=str(cert_path))


# ------------------------------------------------------------------ 전체 기한·상태 코드만


@pytest.mark.parametrize("handler", [drip_headers, silent], ids=["header-drip", "silent"])
def test_slow_receiver_is_cut_at_total_deadline(receiver, handler) -> None:
    r = receiver(handler)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        webhooks.http_send(f"http://hooks.example.test:{r.port}/h", BODY, HEADERS, 1.0, addresses=["127.0.0.1"])
    assert time.monotonic() - t0 < 1.0 + MARGIN_S
    assert r.seen and r.seen[0].startswith(b"POST /h HTTP/1.1")  # 실제로 연결해 보냈다


def test_slow_body_is_not_read(receiver) -> None:
    r = receiver(drip_body)
    t0 = time.monotonic()
    assert webhooks.http_send(f"http://hooks.example.test:{r.port}/h", BODY, HEADERS, 1.0,
                              addresses=["127.0.0.1"]) == 200
    assert time.monotonic() - t0 < 1.0  # 본문(약 2.3일 분량)을 기다리지 않고 상태 코드만 본다


def test_name_resolution_counts_toward_the_deadline(monkeypatch) -> None:
    def hung_dns(*_a: object, **_k: object) -> list:
        time.sleep(5)
        return []

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", hung_dns)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        webhooks.http_send("http://hooks.example.test/h", BODY, HEADERS, 0.5)
    assert time.monotonic() - t0 < 0.5 + MARGIN_S  # 멈춘 이름 풀기 스레드는 기다리지 않고 버린다


# ------------------------------------------------------------------ 주소 고정·프록시 무시·TLS 이름 확인


def test_pinned_address_keeps_host_and_ignores_proxy_env(receiver, monkeypatch) -> None:
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    proxy = f"http://127.0.0.1:{closed.getsockname()[1]}"
    closed.close()  # 아무도 듣지 않는 포트: 프록시를 따르면 연결이 실패한다
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(k, proxy)
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    r = receiver(respond_204)
    with pytest.raises(httpx.ConnectError):  # 환경변수가 실제로 효과가 있다 (기본 httpx는 프록시로 간다)
        httpx.post(f"http://127.0.0.1:{r.port}/h", content=BODY, timeout=2.0)
    # .invalid는 풀리지 않는 이름 — 성공했다면 DNS 없이 고정 주소로 연결한 것이다
    assert webhooks.http_send(f"http://hooks.example.invalid:{r.port}/hook", BODY, HEADERS, 2.0,
                              addresses=["127.0.0.1"]) == 204
    request = r.seen[-1].decode()
    assert request.startswith("POST /hook HTTP/1.1") and f"\r\nHost: hooks.example.invalid:{r.port}\r\n" in request


def test_tls_sni_and_certificate_are_checked_against_the_name(receiver, tmp_path) -> None:
    server, trusted = self_signed(tmp_path, "hooks.example.test")
    r = receiver(respond_204, tls=server)
    assert webhooks.http_send(f"https://hooks.example.test:{r.port}/h", BODY, HEADERS, 2.0,
                              addresses=["127.0.0.1"], verify=trusted) == 204
    assert r.snis == ["hooks.example.test"]  # 고정한 IP가 아니라 이름으로 SNI
    other_server, other_trusted = self_signed(tmp_path, "other.example.test")
    other = receiver(respond_204, tls=other_server)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):  # 이름이 다른 인증서는 거부
        webhooks.http_send(f"https://hooks.example.test:{other.port}/h", BODY, HEADERS, 2.0,
                           addresses=["127.0.0.1"], verify=other_trusted)
    assert other.seen == []


def test_socket_is_closed_when_deadline_hits_during_tls_handshake(receiver, tmp_path) -> None:
    """TLS 핸드셰이크 중에 전체 기한이 닿아도 TCP 소켓을 바로 닫는다 (검토 2026-11-12 W3, ADR-0061).

    httpcore는 핸드셰이크가 일반 예외로 끝날 때만 소켓을 닫고, 전체 기한(asyncio.wait_for)의 취소로 끝나면 닫지 않아
    가비지 수집 때까지 열려 있었다(20회 시도에 소켓 20개). 수집을 꺼 두고 서버가 바로 EOF를 받는지 본다."""
    _, trusted = self_signed(tmp_path, "hooks.example.test")
    r = receiver(stall_before_tls)
    gc.disable()
    try:
        for _ in range(3):
            with pytest.raises(TimeoutError):
                webhooks.http_send(f"https://hooks.example.test:{r.port}/h", BODY, HEADERS, 0.3,
                                   addresses=["127.0.0.1"], verify=trusted)
        deadline = time.monotonic() + 2
        while len(r.seen) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        closed = list(r.seen)
    finally:
        gc.enable()
    assert closed == [b"EOF"] * 3, "기한으로 끊긴 TLS 연결의 소켓이 열린 채 남았다"


def test_next_address_is_tried_only_when_connect_fails(receiver) -> None:
    r = receiver(respond_204)  # 127.0.0.1에서만 듣는다 → 127.0.0.2의 같은 포트는 연결 거부
    assert webhooks.http_send(f"http://hooks.example.test:{r.port}/h", BODY, HEADERS, 2.0,
                              addresses=["127.0.0.2", "127.0.0.1"]) == 204
    assert len(r.seen) == 1
    with pytest.raises(httpx.ConnectError):
        webhooks.http_send(f"http://hooks.example.test:{r.port}/h", BODY, HEADERS, 2.0, addresses=["127.0.0.2"])


# ------------------------------------------------------------------ 대상 검사 (SSRF)


@pytest.mark.parametrize("host", [
    "100.64.0.1", "100.127.255.254",  # 공유 주소(CGNAT) — 예전 검사가 놓치던 대역
    "10.0.0.1", "127.0.0.1", "169.254.169.254", "0.0.0.0", "198.18.0.1", "224.0.0.1", "255.255.255.255",
    "[::1]", "[::ffff:10.0.0.1]", "[::ffff:100.64.0.1]", "[64:ff9b::a00:1]", "[2002:a00:1::1]", "[fc00::1]",
    "[fe80::1]", "[ff02::1]",
])
def test_non_global_ip_literals_are_refused(host: str) -> None:
    with pytest.raises(webhooks.TargetRefused):
        webhooks.check_url(f"https://{host}/x", allow_private=False, resolve=False)


@pytest.mark.parametrize("url,expect", [("https://1.1.1.1/x", ["1.1.1.1"]),
                                        ("https://[2606:4700:4700::1111]:8443/x", ["2606:4700:4700::1111"])])
def test_global_ip_literals_are_allowed(url: str, expect: list[str]) -> None:
    assert webhooks.check_url(url, allow_private=False, resolve=True) == expect


@pytest.mark.parametrize("url", ["http://1.1.1.1/x", "https://user:pw@1.1.1.1/x", "https://1.1.1.1:99999/x",
                                 "https://1.1.1.1:0/x", "https://h:abc/x", "https:///x", "https://localhost./x",
                                 "https://hooks.internal/x", "https://printer.local/x"])
def test_url_rules(url: str) -> None:
    with pytest.raises(webhooks.TargetRefused):
        webhooks.check_url(url, allow_private=False, resolve=False)


def _fake_dns(answers: dict[str, list[str]]) -> Callable[..., list]:
    def getaddrinfo(host: str, port: int, *_a: object, **_k: object) -> list:
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
                for a in answers[host]]

    return getaddrinfo


def test_names_are_resolved_checked_and_pinned(monkeypatch) -> None:
    monkeypatch.setattr(webhooks.socket, "getaddrinfo", _fake_dns({
        "hooks.example.org": ["93.184.216.34", "93.184.216.34", "2606:2800:220:1::"],
        "cgnat.example.org": ["100.64.0.1"], "intranet.example.org": ["10.0.0.5"],
        "mixed.example.org": ["93.184.216.34", "127.0.0.1"]}))
    # 연결할 주소(중복 제거, 순서 유지) — 보낼 때는 이 주소로만 연결한다
    assert webhooks.check_url("https://hooks.example.org/x", allow_private=False, resolve=True) == [
        "93.184.216.34", "2606:2800:220:1::"]
    texts, reasons = set(), set()
    for name in ("cgnat.example.org", "intranet.example.org", "mixed.example.org", "nx.example.org"):
        with pytest.raises(webhooks.TargetRefused) as e:
            webhooks.check_url(f"https://{name}/x", allow_private=False, resolve=True)
        texts.add(str(e.value))
        reasons.add(e.value.reason)
    # 기관에 보이는 문구는 하나 (이름이 없는지·사설 주소인지 드러나지 않게), 자세한 이유는 서버 로그용으로만
    assert texts == {webhooks.REFUSED} and len(reasons) == 4
    # 개발용 설정에서는 검사하지 않고 풀린 주소를 그대로 쓴다
    assert webhooks.check_url("https://intranet.example.org/x", allow_private=True, resolve=True) == ["10.0.0.5"]
