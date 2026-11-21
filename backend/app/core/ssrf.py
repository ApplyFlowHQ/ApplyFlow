"""Connection-bound destination checks for public job pages and mail servers."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit

import httpx

_WEB_PORTS = {80, 443, 8080, 8443}
_MAIL_PORTS = {25, 465, 587, 143, 993}
_BLOCKED_NAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata",
}
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)
_NUMERIC_NAME = re.compile(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*", re.I)
_V6_UNICAST = ipaddress.ip_network("2000::/3")
_TRANSITION = (ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"))


class SSRFBlockedError(ValueError):
    """An outbound destination could not be established as public."""


def _permitted_ip(value: str) -> str:
    if "%" in value:
        raise SSRFBlockedError("Scoped addresses are not public destinations")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SSRFBlockedError("Invalid resolved address") from exc
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address not in _V6_UNICAST or any(address in net for net in _TRANSITION):
            raise SSRFBlockedError("IPv6 destination is outside permitted global unicast space")
    if not address.is_global or address.is_multicast or address.is_reserved or str(address) == "168.63.129.16":
        raise SSRFBlockedError("Destination is not permitted public unicast")
    return str(address)


def validate_public_hostname_and_resolve(host: str, port: int | None = None) -> list[str]:
    """Return a complete, validated address set. Reject mixed DNS answers."""
    cleaned = host.removesuffix(".").lower()
    if not cleaned or "%" in cleaned or cleaned in _BLOCKED_NAMES or cleaned.endswith(".localhost"):
        raise SSRFBlockedError("Disallowed hostname")
    try:
        address = ipaddress.ip_address(cleaned)
    except ValueError:
        if (
            _NUMERIC_NAME.fullmatch(cleaned)
            or len(cleaned) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in cleaned.split("."))
        ):
            raise SSRFBlockedError("Invalid or ambiguous hostname") from None
    else:
        return [_permitted_ip(str(address))]

    for attempt in range(2):
        try:
            answers = socket.getaddrinfo(cleaned, port or 443, type=socket.SOCK_STREAM)
            break
        except socket.gaierror as exc:
            if attempt:
                raise SSRFBlockedError("Unable to resolve destination") from exc
            time.sleep(0.15)
    addresses = list(dict.fromkeys(_permitted_ip(item[4][0]) for item in answers))
    if not addresses:
        raise SSRFBlockedError("No public destination addresses")
    return addresses


def _check_reference(value: str) -> None:
    if not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in value):
        raise SSRFBlockedError("Invalid URL characters")


def _web_url(value: str) -> httpx.URL:
    _check_reference(value)
    try:
        parts = urlsplit(value)
        if not parts.netloc or any(c in parts.netloc for c in "@%") or parts.netloc.endswith(":"):
            raise SSRFBlockedError("Missing or ambiguous URL authority")
        if parts.netloc.startswith("[") and not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parts.netloc):
            raise SSRFBlockedError("Invalid IP literal authority")
        if parts.port is not None and parts.port not in _WEB_PORTS:
            raise SSRFBlockedError("Unsupported web port")
        url = httpx.URL(value)
    except (ValueError, httpx.InvalidURL) as exc:
        raise SSRFBlockedError("Invalid public web URL") from exc
    if url.scheme not in ("http", "https") or not url.raw_host:
        raise SSRFBlockedError("Only absolute HTTP and HTTPS URLs are supported")
    return url


def validate_external_url(url: str) -> str:
    parsed = _web_url(url)
    validate_public_hostname_and_resolve(parsed.raw_host.decode("ascii"), parsed.port)
    return str(parsed)


def validate_mail_host(host: str, port: int) -> list[str]:
    if port not in _MAIL_PORTS:
        raise SSRFBlockedError("Unsupported mail port")
    return validate_public_hostname_and_resolve(host, port)


async def safe_fetch_public_url(
    url: str,
    *,
    max_bytes: int = 1_500_000,
    max_redirects: int = 4,
    timeout_seconds: float = 10.0,
    user_agent: str = "ApplyFlowGlobalBot/1.0 (+https://applyflow.local/bot)",
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, int, str, bytes]:
    """Fetch within one deadline. Each dial and redirect has its own connection pool."""
    if transport is not None and type(transport) is not httpx.MockTransport:
        raise ValueError("Only an in-memory mock transport can override public fetching")
    current = _web_url(url)
    async with asyncio.timeout(timeout_seconds):
        for hop in range(max_redirects + 1):
            hostname = current.raw_host.decode("ascii")
            port = current.port or (443 if current.scheme == "https" else 80)
            addresses = await asyncio.to_thread(validate_public_hostname_and_resolve, hostname, port)
            for index, address in enumerate(addresses):
                # Mocks consume logical URLs. Production HTTPX always receives a numeric origin.
                target = current if transport is not None else current.copy_with(host=address)
                headers = {
                    "Host": current.netloc.decode("ascii"),
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/json,text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                }
                try:
                    async with httpx.AsyncClient(
                        transport=transport, trust_env=False, timeout=timeout_seconds
                    ) as client:
                        async with client.stream(
                            "GET", target, headers=headers, extensions={"sni_hostname": hostname}
                        ) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                location = response.headers.get("location", "")
                                _check_reference(location)
                                if hop == max_redirects:
                                    raise SSRFBlockedError("Redirect limit exceeded")
                                if location.startswith("//"):
                                    next_url = _web_url(current.scheme + ":" + location)
                                elif urlsplit(location).scheme:
                                    next_url = _web_url(location)
                                else:
                                    next_url = _web_url(str(current.join(location)))
                                if current.scheme == "https" and next_url.scheme == "http":
                                    raise SSRFBlockedError("HTTPS downgrade rejected")
                                current = next_url
                                break
                            if response.headers.get("content-encoding", "identity").lower() != "identity":
                                raise SSRFBlockedError("Unsupported content encoding on bounded download")
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=16384):
                                if len(body) + len(chunk) > max_bytes:
                                    raise ValueError("Response exceeded maximum allowed size")
                                body.extend(chunk)
                            return (
                                str(current),
                                response.status_code,
                                response.headers.get("content-type", ""),
                                bytes(body),
                            )
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if index + 1 == len(addresses):
                        raise
    raise SSRFBlockedError("Redirect limit exceeded")
