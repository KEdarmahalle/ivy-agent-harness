"""Outbound HTTP with strict URL validation and DNS-pinned connections."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


class OutboundRequestError(ValueError):
    """Raised when an outbound request violates network policy."""


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        except OSError as exc:
            raise OutboundRequestError(f"cannot resolve outbound host: {host}") from exc
    if not addresses:
        raise OutboundRequestError(f"no addresses found for outbound host: {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise OutboundRequestError("outbound requests to non-public IP addresses are forbidden")
    return addresses


def validate_outbound_url(url: str, *, allowed_hosts: list[str] | None = None) -> tuple[object, list[str]]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OutboundRequestError("only absolute http and https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundRequestError("credentials embedded in outbound URLs are forbidden")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if allowed_hosts is not None:
        allowed = {item.encode("idna").decode("ascii").lower().rstrip(".") for item in allowed_hosts}
        if host not in allowed:
            raise OutboundRequestError("outbound host is not in the configured allowlist")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed, _resolve_public_addresses(host, port)


def safe_http_request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_bytes: int = 262_144,
    allowed_hosts: list[str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Make one non-redirecting HTTP request to a validated, pinned public IP."""
    parsed, addresses = validate_outbound_url(url, allowed_hosts=allowed_hosts)
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    address = addresses[0]
    sock = socket.create_connection((address, port), timeout=timeout)
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)

    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    connection.sock = sock
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.putrequest(method.upper(), target, skip_accept_encoding=True)
        connection.putheader("Host", parsed.netloc)
        connection.putheader("Connection", "close")
        for name, value in (headers or {}).items():
            if name.lower() not in {"host", "connection", "content-length", "transfer-encoding"}:
                connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OutboundRequestError(f"outbound response exceeds {max_bytes} bytes")
        return response.status, {k.lower(): v for k, v in response.getheaders()}, data
    finally:
        connection.close()
