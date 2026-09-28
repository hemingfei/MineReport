"""出站 URL 安全闸门（仓库安全约束）：仅 http/https，拒绝 localhost/环回/私有/保留/链路本地地址。

所有对外请求（LLM 端点、后续连接器的 fxbaogao REST）发请求前必须过这道闸。
域名解析出的全部地址都要校验（防 DNS 里解析到内网 IP 的 SSRF）。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _is_ip_literal(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _address_allowed(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_unspecified
    )


def assert_safe_http_url(url: str) -> None:
    """不安全即抛 ValueError；调用方转为各自的 PipelineError。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"scheme not allowed: {u.scheme!r}")
    host = u.hostname or ""
    if not host:
        raise ValueError("url has no host")

    if _is_ip_literal(host):
        candidates = [host]
    else:
        candidates = []
        try:
            for info in socket.getaddrinfo(host, None):
                candidates.append(info[4][0])
        except socket.gaierror as e:
            raise ValueError(f"cannot resolve host: {host}") from e

    for addr in set(candidates):
        if addr == "localhost" or not _address_allowed(addr):
            raise ValueError(f"host not allowed: {addr}")
