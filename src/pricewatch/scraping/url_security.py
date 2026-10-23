"""Единая консервативная проверка URL и DNS до прикладных HTTP-запросов."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UrlSecurity:
    def __init__(self, resolver=socket.getaddrinfo) -> None:
        self._resolver = resolver

    def validate(self, url: str) -> str | None:
        if not isinstance(url, str) or not url:
            return "invalid URL"
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            port = parts.port
        except ValueError:
            return "invalid URL"
        if parts.scheme not in {"http", "https"} or not hostname or parts.username or parts.password:
            return "invalid URL"
        if port is not None and not 1 <= port <= 65535:
            return "invalid port"
        if hostname.lower() in {"localhost", "localhost.localdomain"}:
            return "unsafe hostname"
        try:
            addresses = {str(ipaddress.ip_address(hostname))}
        except ValueError:
            try:
                addresses = {item[4][0] for item in self._resolver(hostname, port)}
            except (OSError, ValueError):
                return "hostname cannot be resolved safely"
        if not addresses:
            return "hostname has no resolved addresses"
        for address in addresses:
            try:
                if not ipaddress.ip_address(address).is_global:
                    return "unsafe IP address"
            except ValueError:
                return "invalid resolved address"
        return None
