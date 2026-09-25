"""Validação de URLs externas antes de qualquer acesso de rede."""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    pass


def validate_public_http_url(url: str, *, https_only: bool = False) -> str:
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise UnsafeUrl("URL inválida.")
    if any(char in url for char in ("\\", "\r", "\n", "\x00")):
        raise UnsafeUrl("URL inválida.")
    parsed = urlsplit(url)
    allowed_schemes = {"https"} if https_only else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        raise UnsafeUrl("Protocolo não permitido.")
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise UnsafeUrl("Credenciais ou porta não permitidas.")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        }
    except (OSError, UnicodeError, ValueError) as exc:
        raise UnsafeUrl("Host não pôde ser validado.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeUrl("Destino de rede não permitido.")
    return url


def _validate_allowed_host(url: str, allowed_hosts: set[str] | None) -> None:
    if allowed_hosts is None:
        return
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    if host not in allowed_hosts:
        raise UnsafeUrl("Domínio de destino não permitido.")


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(
        self,
        *,
        https_only: bool = False,
        allowed_hosts: set[str] | None = None,
        max_redirects: int = 3,
    ) -> None:
        super().__init__()
        self.https_only = https_only
        self.allowed_hosts = allowed_hosts
        self.max_redirections = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_public_http_url(newurl, https_only=self.https_only)
        _validate_allowed_host(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    https_only: bool = False,
    allowed_hosts: set[str] | None = None,
    max_redirects: int = 3,
):
    validate_public_http_url(request.full_url, https_only=https_only)
    _validate_allowed_host(request.full_url, allowed_hosts)
    response = urllib.request.build_opener(
        _ValidatedRedirectHandler(
            https_only=https_only,
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
        )
    ).open(request, timeout=timeout)
    try:
        validate_public_http_url(response.geturl(), https_only=https_only)
        _validate_allowed_host(response.geturl(), allowed_hosts)
        return response
    except Exception:
        response.close()
        raise
