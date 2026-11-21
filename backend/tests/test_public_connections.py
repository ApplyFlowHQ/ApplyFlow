"""Observe transport dials, Host and TLS identity against a test-owned listener."""

import asyncio
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta

import certifi
import httpcore
import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core import mail_sockets, ssrf


@pytest_asyncio.fixture
async def network(tmp_path, monkeypatch):
    # The only socket connected by this fixture goes to this process's ephemeral listener.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jobs.example.org")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("jobs.example.org")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem, private = tmp_path / "test.crt", tmp_path / "test.key"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pem, private)
    sni, hosts, dials, resolutions = [], [], [], []
    ctx.set_servername_callback(lambda sock, host, context: sni.append(host))
    monkeypatch.setattr(certifi, "where", lambda: str(pem))
    monkeypatch.setenv("HTTPS_PROXY", "http://unused-proxy.test:7777")
    monkeypatch.setenv("HTTP_PROXY", "http://unused-proxy.test:7777")
    monkeypatch.setenv("NO_PROXY", "")
    answers = {"jobs.example.org": ["1.1.1.1"], "board.example.org": ["1.1.1.1"]}
    refused = set()

    def lookup(host, port, **kwargs):
        resolutions.append(host)
        return [(0, 1, 0, "", (ip, port)) for ip in answers[host]]

    async def serve(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
            hosts.append(next(line[6:] for line in head if line.lower().startswith("host: ")))
            route = head[0].split()[1]
            destinations = {
                "/redirect": "https://jobs.example.org/final",
                "/same": "/final",
                "/other": "https://board.example.org/final",
                "/loop": "/loop",
                "/down": "http://jobs.example.org/",
            }
            location = destinations.get(route)
            response = f"302 Found\r\nLocation: {location}" if location else "200 OK"
            writer.write(f"HTTP/1.1 {response}\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello".encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    plain = await asyncio.start_server(serve, "127.0.0.1", 0)
    tls = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=ctx)
    original = httpcore.AnyIOBackend.connect_tcp

    async def dial(self, host, port, **kwargs):
        dials.append((host, port))
        assert ipaddress.ip_address(host).is_global
        if host in refused:
            raise httpcore.ConnectError("Synthetic unavailable address")
        local = tls if port in (443, 8443) else plain
        return await original(self, "127.0.0.1", local.sockets[0].getsockname()[1], **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", dial)
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", lookup)
    async with plain, tls:
        yield answers, refused, resolutions, dials, hosts, sni


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["1.1.1.1", "2606:4700:4700::1111", "::ffff:1.1.1.1"])
async def test_real_tls_identity_and_numeric_dial(network, ip):
    answers, _, lookups, dials, hosts, sni = network
    answers["jobs.example.org"] = [ip]
    result = await ssrf.safe_fetch_public_url("https://jobs.example.org:8443/final")
    expected = "1.1.1.1" if ip.startswith("::ffff:") else ip
    assert result[1:] == (200, "", b"hello")
    assert dials == [(expected, 8443)] and lookups == ["jobs.example.org"]
    assert hosts == ["jobs.example.org:8443"] and sni == ["jobs.example.org"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "::1",
        "fe80::1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::ffff:192.168.0.1",
        "64:ff9b::1",
        "168.63.129.16",
    ],
)
async def test_mixed_answers_rejected_before_dial(network, address):
    answers, _, _, dials, _, _ = network
    answers["jobs.example.org"].append(address)
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.safe_fetch_public_url("https://jobs.example.org")
    assert dials == []


@pytest.mark.asyncio
async def test_fallback_snapshot_and_same_origin_redirect(network):
    answers, refused, lookups, dials, hosts, _ = network

    class FirstSnapshot(list):
        def __iter__(self):
            answers["jobs.example.org"] = ["9.9.9.9"]
            return super().__iter__()

    answers["jobs.example.org"] = FirstSnapshot(["1.1.1.1", "8.8.8.8"])
    refused.add("1.1.1.1")
    await ssrf.safe_fetch_public_url("https://jobs.example.org/same")
    assert lookups == ["jobs.example.org", "jobs.example.org"]
    assert dials == [("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443)]
    assert hosts == ["jobs.example.org"] * 2


@pytest.mark.asyncio
async def test_new_authority_and_protocol(network):
    _, _, _, dials, hosts, sni = network
    await ssrf.safe_fetch_public_url("http://board.example.org/redirect")
    assert dials == [("1.1.1.1", 80), ("1.1.1.1", 443)]
    assert hosts == ["board.example.org", "jobs.example.org"] and sni == ["jobs.example.org"]


@pytest.mark.asyncio
async def test_redirect_revalidation_and_no_tls_pool_sharing(network):
    answers, _, _, dials, hosts, sni = network
    answers["board.example.org"] = ["127.0.0.1"]
    with pytest.raises(ssrf.SSRFBlockedError):
        await ssrf.safe_fetch_public_url("https://jobs.example.org/other")
    assert dials == [("1.1.1.1", 443)]
    answers["board.example.org"] = ["1.1.1.1"]
    with pytest.raises(httpx.ConnectError, match="(?i)certificate"):
        await ssrf.safe_fetch_public_url("https://jobs.example.org/other")
    assert sni[-2:] == ["jobs.example.org", "board.example.org"]
    assert hosts == ["jobs.example.org"] * 2


@pytest.mark.asyncio
async def test_redirect_and_body_limits(network):
    with pytest.raises(ssrf.SSRFBlockedError, match="limit"):
        await ssrf.safe_fetch_public_url("http://jobs.example.org/loop", max_redirects=1)
    with pytest.raises(ssrf.SSRFBlockedError, match="downgrade"):
        await ssrf.safe_fetch_public_url("https://jobs.example.org/down")
    with pytest.raises(ValueError, match="size"):
        await ssrf.safe_fetch_public_url("http://jobs.example.org/final", max_bytes=4)


@pytest.mark.parametrize(
    "url",
    [
        "file:///sample",
        "https://user@jobs.example.org",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f.1/",
        "https://[fe80::1%25lo]/",
        "https://jobs.example.org:/",
        "https://jobs.example.org:0/",
        "https://jobs.example.org:22/",
        "https://a\\b/",
        "https://a\nb/",
    ],
)
def test_url_policy(url):
    with pytest.raises(ssrf.SSRFBlockedError):
        ssrf.validate_external_url(url)


def test_mail_dials_validated_addresses(monkeypatch):
    calls = []

    class Socket:
        def __init__(self, family, kind):
            self.family = family

        def settimeout(self, timeout):
            pass

        def close(self):
            pass

        def connect(self, dest):
            calls.append((self.family, dest))
            if len(calls) == 1:
                raise ConnectionRefusedError()

    monkeypatch.setattr(mail_sockets, "validate_mail_host", lambda *args: ["1.1.1.1", "2606:4700:4700::1111"])
    monkeypatch.setattr(mail_sockets.socket, "socket", Socket)
    client = object.__new__(mail_sockets.PublicSMTP)
    client._get_socket("mail.example.org", 587, 10)
    assert calls == [
        (socket_family, (ip, 587))
        for socket_family, ip in ((ssrf.socket.AF_INET, "1.1.1.1"), (ssrf.socket.AF_INET6, "2606:4700:4700::1111"))
    ]


@pytest.mark.asyncio
async def test_operating_system_connect_receives_only_checked_ip(monkeypatch):
    # Abort at the OS boundary, so this test cannot initiate external traffic.
    observed = []

    def record_connect(sock, endpoint):
        observed.append(endpoint[:2])
        raise ConnectionRefusedError("Synthetic closed socket")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 0, "", ("1.1.1.1", 443))])
    monkeypatch.setattr(ssrf.socket.socket, "connect", record_connect)
    with pytest.raises(httpx.ConnectError):
        await ssrf.safe_fetch_public_url("https://jobs.example.org/")
    assert observed == [("1.1.1.1", 443)]


@pytest.mark.asyncio
async def test_total_fetch_deadline(monkeypatch):
    async def delayed(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    monkeypatch.setattr(ssrf, "validate_public_hostname_and_resolve", lambda *a: ["1.1.1.1"])
    with pytest.raises(TimeoutError):
        await ssrf.safe_fetch_public_url(
            "https://jobs.example.org", timeout_seconds=0.05, transport=httpx.MockTransport(delayed)
        )
