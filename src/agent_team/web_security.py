from __future__ import annotations

import ipaddress
from http.server import BaseHTTPRequestHandler


def validate_web_bind(host: str, unsafe_allow_remote: bool) -> None:
    if not unsafe_allow_remote and not is_loopback_bind(host):
        raise ValueError(
            "Refusing to bind the unauthenticated web interface to a non-loopback address; "
            "pass --unsafe-allow-remote only in a protected environment."
        )


def allowed_hosts(handler: BaseHTTPRequestHandler) -> set[str]:
    server_host = str(handler.server.server_address[0]).lower()
    port = int(handler.server.server_port)
    names = {server_host, "127.0.0.1", "localhost", "::1", "[::1]"}
    allowed: set[str] = set()
    for name in names:
        if not name:
            continue
        allowed.add(name)
        allowed.add(f"{name}:{port}")
    return allowed


def is_loopback_bind(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if not normalized:
        return False
    try:
        return ipaddress.ip_address(normalized.strip("[]")).is_loopback
    except ValueError:
        return False
