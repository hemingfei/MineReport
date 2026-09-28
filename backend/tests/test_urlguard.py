"""urlguard 安全闸门测试（仓库安全约束的回归测试）。"""

from __future__ import annotations

import pytest

from app.urlguard import assert_safe_http_url


def test_https_public_host_allowed(monkeypatch) -> None:
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port: [(None, None, None, None, ("93.184.215.14", 0))])
    assert_safe_http_url("https://api.example.com/v1")  # 不抛即通过


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.0.0.1.nip.io/v1",
        "http://[::1]:8000/v1",
        "http://192.168.1.10/v1",
        "http://10.0.0.5/v1",
        "http://169.254.169.254/latest/meta-data",  # 云元数据端点
        "http://0.0.0.0/v1",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http:///no-host",
        "https://example.com@127.0.0.1/v1",  # userinfo 混淆
    ],
)
def test_rejects_local_private_reserved(url: str, monkeypatch) -> None:
    # 域名形态的用例解析到固定地址，确保走的是地址判定而非 DNS 环境
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda host, port: [(None, None, None, None, ("192.168.0.1" if host else "127.0.0.1", 0))],
    )
    with pytest.raises(ValueError):
        assert_safe_http_url(url)


def test_domain_resolving_to_private_ip_rejected(monkeypatch) -> None:
    """DNS 解析到内网地址的域名同样拒绝（SSRF 主防线）。"""
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda host, port: [(None, None, None, None, ("10.1.2.3", 0))]
    )
    with pytest.raises(ValueError):
        assert_safe_http_url("https://evil.example.com/v1")


def test_unresolvable_host_rejected(monkeypatch) -> None:
    import socket

    def _raise(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr("socket.getaddrinfo", _raise)
    with pytest.raises(ValueError):
        assert_safe_http_url("https://no-such-host.invalid/v1")


def test_llm_client_guard_blocks_local_base_url() -> None:
    import secrets

    from app.errors import AnalysisError
    from app.llm import LLMClient

    with pytest.raises(AnalysisError) as e:
        LLMClient("http://127.0.0.1:8000/v1", "test-" + secrets.token_hex(8), "m")
    assert e.value.error_code == "unsafe_llm_url"
