"""连接器契约与 fxbaogao 首接测试（spec 次缝合口 2：httpx MockTransport，不碰网络）。

覆盖：注册表、search 解析（<em> 剥离/pubTime 秒级/分页/startTime 增量）、download
防御性键扫描与相对路径拼接、host 校验钩子（含 localhost/私有地址/重定向的拒绝
用例——安全约束的回归测试）、全局限速。
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import socket

import httpx
import pytest

from app import fxbaogao as fx
from app.connectors import RateLimiter, available_connectors, build_connector
from app.errors import ConnectorError

# 2026-10-09T00:00Z 的秒级时间戳（pubTime 字段的实测口径）
PUB_TIME = 1_791_504_000


def fake_dns(monkeypatch, mapping: dict[str, str] | None = None) -> None:
    """假 DNS：未列出的 host 解析到公网 IP（host 闸门放行），列出的按映射解析。"""

    def _resolve(host, port):
        addr = (mapping or {}).get(host, "93.184.215.14")
        return [(None, None, None, None, (addr, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


def dummy_key() -> str:
    return "sk-test-" + secrets.token_hex(8)


def make_connector(handler, **kwargs) -> fx.FxbaogaoConnector:
    """测试构造：显式 key（不依赖 .env）+ MockTransport + 不睡眠的限速器。"""
    kwargs.setdefault("api_key", dummy_key())
    kwargs.setdefault("rate_limiter", RateLimiter(10000))
    kwargs.setdefault("transport", httpx.MockTransport(handler))
    return fx.FxbaogaoConnector(**kwargs)


def search_response(items: list[dict]) -> dict:
    return {"code": 200, "data": items}


def entry(report_id: int, title: str = "标题", **over) -> dict:
    base = {
        "reportId": report_id,
        "title": title,
        "orgName": "测试证券",
        "industryName": "信息技术",
        "pageNum": 12,
        "pubTime": PUB_TIME,
        "pubTimeStr": "2026-10-09 08:00:00",
        "paragraphs": [{"content": "命中<em>段落</em>", "pageNum": 3}],
    }
    base.update(over)
    return base


# ---------- 注册表与构造 ----------


def test_registry_and_build() -> None:
    assert "fxbaogao" in available_connectors()
    with pytest.raises(ConnectorError) as e:
        build_connector("nope")
    assert e.value.error_code == "unknown_connector"


def test_build_connector_triggers_builtin_load(monkeypatch) -> None:
    """worker 冷进程的首个注册表触点是 build_connector（run_subscription），
    此前无人调 available_connectors——查表前必须兜底装载。
    （真冷导入在测试进程不可模拟：app.fxbaogao 已被缓存，@register 不重放，
    故钉住"调 _load_builtins"这一契约本身，注册表用无凭据桩类。）"""
    import app.connectors as connectors

    class _Stub(connectors.Connector):
        connector_id = "stub-conn"

    called = []
    monkeypatch.setattr(connectors, "_load_builtins", lambda: called.append(1))
    monkeypatch.setattr(connectors, "_REGISTRY", {"stub-conn": _Stub})
    conn = build_connector("stub-conn")
    assert called, "build_connector 查表前未兜底装载内置连接器"
    assert isinstance(conn, _Stub)


def test_build_without_key_rejected(tweak_settings) -> None:
    tweak_settings(fxbaogao_api_key="")
    with pytest.raises(ConnectorError) as e:
        build_connector("fxbaogao")
    assert e.value.error_code == "fxbaogao_not_configured"


# ---------- 纯解析函数 ----------


def test_parse_search_strips_em_and_maps_fields() -> None:
    refs = fx.parse_search_response(
        search_response([entry(1, title="<em>AI算力</em>产业前瞻", orgName="华东<em>证券</em>")])
    )
    assert len(refs) == 1
    r = refs[0]
    assert r.external_id == "1"
    assert r.title == "AI算力产业前瞻"
    assert r.broker == "华东证券"
    assert r.industry == "信息技术"
    assert r.pages == 12
    assert r.snippet == "命中段落"
    assert r.publish_date == dt.date(2026, 10, 9)
    assert r.published_at is not None and r.published_at.tzinfo is not None


def test_parse_pubtime_millisecond_defensive() -> None:
    _, d = fx.parse_published_at({"pubTime": PUB_TIME * 1000 + 123})  # >1e12 视为毫秒
    assert d == dt.date(2026, 10, 9)


def test_parse_pubtime_str_fallback_and_skip() -> None:
    _, d = fx.parse_published_at({"pubTimeStr": "2026-01-05 10:00:00"})
    assert d == dt.date(2026, 1, 5)
    assert fx.parse_published_at({"pubTimeStr": "n/a"}) == (None, None)
    # 日期不可解析的条目整条跳过（不阻断整轮）
    refs = fx.parse_search_response(
        search_response([entry(2, pubTime=None, pubTimeStr=None), entry(3)])
    )
    assert [r.external_id for r in refs] == ["3"]


def test_parse_search_missing_data_raises() -> None:
    with pytest.raises(ConnectorError) as e:
        fx.parse_search_response({"code": 300011, "msg": "请传入正确Authorization"})
    assert e.value.error_code == "fxbaogao_bad_response"


def test_extract_download_url_defensive_keys() -> None:
    assert fx.extract_download_url({"fileurl": "/f/x.pdf"}) == "/f/x.pdf"
    assert fx.extract_download_url({"downloadurl": "https://cdn.example.com/y.pdf"}) == "https://cdn.example.com/y.pdf"
    assert fx.extract_download_url({"other": 1}) is None
    assert fx.extract_download_url({"url": ""}) is None


def test_extract_download_url_data_string_and_nested() -> None:
    """实测响应形态：URL 在 data 字段直接给字符串（2026-09-28 REST 直连真跑抓出）。"""
    assert (
        fx.extract_download_url({"code": 0, "msg": "ok", "data": "https://dr.fxbaogao.com/r/a.pdf?auth_key=1"})
        == "https://dr.fxbaogao.com/r/a.pdf?auth_key=1"
    )
    # data 内层字典同样防御扫描
    assert fx.extract_download_url({"code": 0, "data": {"pdfurl": "/b.pdf"}}) == "/b.pdf"
    # data 字符串优先于键扫描
    assert fx.extract_download_url({"url": "/c.pdf", "data": "/d.pdf"}) == "/d.pdf"


# ---------- discover（MockTransport） ----------


def test_discover_request_shape_and_since_window(monkeypatch) -> None:
    fake_dns(monkeypatch)
    since = dt.datetime(2026, 10, 8, tzinfo=dt.timezone.utc)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "path": request.url.path,
                "body": json.loads(request.read()),
                "auth": request.headers.get("Authorization"),
            }
        )
        return httpx.Response(200, json=search_response([entry(11)]))

    refs = make_connector(handler).discover("AI算力", since)
    assert [r.external_id for r in refs] == ["11"]
    assert captured[0]["path"] == "/mofoun/agent/search"
    assert captured[0]["body"] == {
        "keywords": "AI算力",
        "startTime": str(int(since.timestamp() * 1000)),
        "pageNum": 1,
    }
    assert captured[0]["auth"] is not None and captured[0]["auth"].startswith("Bearer ")


def test_discover_default_window_and_pagination(monkeypatch) -> None:
    fake_dns(monkeypatch)
    pages = [
        [entry(i) for i in range(1, 21)],  # 满 20 条 → 翻页
        [entry(100)],  # 1 条 → 末页
    ]
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=search_response(pages[len(bodies) - 1]))

    refs = make_connector(handler).discover("算力")
    assert len(refs) == 21
    assert [b["pageNum"] for b in bodies] == [1, 2]
    assert bodies[0]["startTime"] == "last3day"


def test_discover_orgs_filter_forwarded(monkeypatch) -> None:
    fake_dns(monkeypatch)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.read()))
        return httpx.Response(200, json=search_response([]))

    make_connector(handler).discover("算力", orgs=["中信证券"])
    assert bodies[0]["orgNames"] == ["中信证券"]


def test_discover_pagination_capped_by_max_pages(monkeypatch) -> None:
    fake_dns(monkeypatch)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.read())["pageNum"])
        return httpx.Response(200, json=search_response([entry(i) for i in range(1, 21)]))

    make_connector(handler, max_pages=2).discover("储能")
    assert calls == [1, 2]  # 每页都满 20 条，但到 max_pages 即停


def test_discover_auth_error_normalized(monkeypatch) -> None:
    fake_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 300011, "msg": "请传入正确Authorization"})

    with pytest.raises(ConnectorError) as e:
        make_connector(handler).discover("算力")
    assert e.value.error_code == "fxbaogao_auth"


# ---------- fetch（MockTransport） ----------


def test_fetch_downloads_relative_url_bytes(monkeypatch) -> None:
    fake_dns(monkeypatch)
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.path == "/mofoun/agent/download":
            return httpx.Response(200, json={"fileurl": "/files/abc.pdf?sig=1"})
        assert request.url.host == "dr.fxbaogao.com"
        return httpx.Response(200, content=b"%PDF-1.4 fake")

    ref = fx.parse_search_response(search_response([entry(7, title='深度:"算力"/元年')]))[0]
    out = make_connector(handler).fetch(ref)
    assert out.data == b"%PDF-1.4 fake"
    assert out.content_type == "application/pdf"
    assert out.filename == "深度_算力_元年.pdf"  # 连续非法字符合并为单个 _
    assert hits[0].startswith("https://api.fxbaogao.com/mofoun/agent/download?reportId=7")
    assert "https://dr.fxbaogao.com/files/abc.pdf" in hits[1]


def test_fetch_missing_url_key(monkeypatch) -> None:
    fake_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mofoun/agent/download"
        return httpx.Response(200, json={"code": 200})

    conn = make_connector(handler)
    ref = fx.parse_search_response(search_response([entry(8)]))[0]
    with pytest.raises(ConnectorError) as e:
        conn.fetch(ref)
    assert e.value.error_code == "fxbaogao_no_download_url"


def test_fetch_empty_file(monkeypatch) -> None:
    fake_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mofoun/agent/download":
            return httpx.Response(200, json={"pdfurl": "https://dr.fxbaogao.com/e.pdf"})
        return httpx.Response(200, content=b"")

    conn = make_connector(handler)
    ref = fx.parse_search_response(search_response([entry(9)]))[0]
    with pytest.raises(ConnectorError) as e:
        conn.fetch(ref)
    assert e.value.error_code == "fxbaogao_empty_file"


# ---------- host 校验钩子（安全约束回归测试） ----------

# 钩子抛 ValueError（urlguard 原样冒泡）；transport 不应被触达


@pytest.mark.parametrize(
    "url,dns",
    [
        ("https://api.fxbaogao.com/mofoun/agent/search", {"api.fxbaogao.com": "127.0.0.1"}),  # DNS→环回（SSRF 主防线）
        ("http://localhost:8080/x", {"localhost": "127.0.0.1"}),
        ("http://127.0.0.1:8000/x", {}),
        ("http://192.168.1.5/x", {}),  # 私有 IP 字面量
        ("http://10.0.0.5/x", {}),
        ("http://169.254.169.254/latest/meta-data", {}),  # 云元数据端点
        ("http://[::1]:8000/x", {}),
        ("ftp://dr.fxbaogao.com/x", {}),  # 非 http/https
    ],
)
def test_host_guard_blocks_unsafe_targets(monkeypatch, url, dns) -> None:
    fake_dns(monkeypatch, dns)
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json=search_response([]))

    conn = make_connector(handler)
    with pytest.raises(ValueError):
        conn._client.get(url)
    assert called == []


def test_host_guard_allows_public_and_rejects_private_redirect(monkeypatch) -> None:
    """api.fxbaogao.com 解析公网 IP 放行；重定向到私有地址时第二个请求仍被钩子拦下。"""
    fake_dns(monkeypatch, {"evil.fxbaogao.com": "10.6.6.6"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://evil.fxbaogao.com/leak"})
        return httpx.Response(200, json=search_response([]))

    conn = make_connector(handler)
    with pytest.raises(ValueError):
        conn._client.get("https://api.fxbaogao.com/redirect")
    # 钩子对重定向后的请求同样生效：私有目标未被 transport 触达
    assert seen == ["https://api.fxbaogao.com/redirect"]


# ---------- 全局限速 ----------


def test_rate_limiter_spaces_requests(monkeypatch) -> None:
    clock = {"now": 1000.0}
    sleeps: list[float] = []
    monkeypatch.setattr("app.connectors.time.sleep", lambda s: sleeps.append(s))
    limiter = RateLimiter(1.0, now=lambda: clock["now"])

    assert limiter.acquire() == 0.0  # 首个立即放行
    clock["now"] += 0.25
    assert limiter.acquire() == 0.75  # 距上次放行须补足 1s
    assert sleeps == [0.75]


def test_rate_limiter_no_limit() -> None:
    assert RateLimiter(0).acquire() == 0.0
