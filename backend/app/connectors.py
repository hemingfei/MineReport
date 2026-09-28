"""连接器契约与注册表（#19，spec：pull 模式）。

连接器只实现两个方法：`discover(query, since) -> [ReportRef]`（元数据先行，不扣
下载额度）与 `fetch(ref) -> FetchedFile`（拿文件，fxbaogao 的 PDF 下载扣 VIP 权益
额度）。去重（ADR-0001 组合键 + ExternalRef）、下载编排、入库回调、退避/死信全部
收敛在调度器层（app/scheduler.py），连接器不各写一份——新平台接入 = 实现两个
方法 + 注册，无需新决策。

所有出站请求必须过 urlguard host 校验（httpx event hook 挂在客户端上，含重定向
后的每个请求），仅 http/https，拒绝 localhost/环回/私有/保留地址。
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .errors import ConnectorError
from .urlguard import assert_safe_http_url


@dataclass
class ReportRef:
    """discover 的返回条目：外部平台的研报元数据（fxbaogao search 实测字段，
    无作者/评级——靠后续 LLM 从 PDF 抽）。"""

    external_id: str
    title: str  # <em> 高亮已剥离
    publish_date: dt.date
    published_at: dt.datetime | None = None  # 秒级时间戳换算的精确时刻（增量游标用）
    broker: str | None = None
    industry: str | None = None
    pages: int | None = None
    snippet: str | None = None
    raw: dict = field(default_factory=dict)  # 平台原始条目（审计）


@dataclass
class FetchedFile:
    """fetch 的返回：文件字节。fxbaogao 的下载 URL 带时效，拿到即下载，只存文件不存 URL。"""

    filename: str
    content_type: str
    data: bytes


class Connector:
    """连接器基类：子类设 connector_id 并实现 discover/fetch。"""

    connector_id: str = ""

    def discover(
        self,
        query: str,
        since: dt.datetime | None = None,
        *,
        orgs: list[str] | None = None,
    ) -> list[ReportRef]:
        raise NotImplementedError

    def fetch(self, ref: ReportRef) -> FetchedFile:
        raise NotImplementedError

    def quota_hint(self) -> str | None:
        """手动下载前的额度提示文案（无额度概念的平台返回 None）。"""
        return None

    @classmethod
    def view_url(cls, external_id: str) -> str | None:
        """外部条目的阅读链接（确定性推导，无需入库）；平台无公开阅读页返回 None。"""
        return None


# ---------- 注册表 ----------

_REGISTRY: dict[str, type[Connector]] = {}
_BUILTINS_LOADED = False


def _load_builtins() -> None:
    """惰性导入内置连接器（@register 类装饰器自注册）。API 进程不跑调度，
    若无人 import app.fxbaogao，注册表会缺它——首次查表前兜底装载。"""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from . import fxbaogao  # noqa: F401  # 副作用：注册 FxbaogaoConnector

    _BUILTINS_LOADED = True


def register(cls: type[Connector]) -> type[Connector]:
    """类装饰器注册；重复注册视为模块重载，后者覆盖。"""
    assert cls.connector_id, f"{cls.__name__} 缺 connector_id"
    _REGISTRY[cls.connector_id] = cls
    return cls


def available_connectors() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def connector_class(connector_id: str) -> type[Connector] | None:
    """取注册类（view_url 等类方法免实例化/凭据调用）。"""
    _load_builtins()
    return _REGISTRY.get(connector_id)


def build_connector(connector_id: str) -> Connector:
    """按注册表构造连接器实例（凭据在构造时从 Settings 读取）。

    未注册的 id 抛 ConnectorError；已注册但凭据未配置由连接器构造器自判
    （fxbaogao：api_key 为空即拒）。
    """
    cls = _REGISTRY.get(connector_id)
    if cls is None:
        raise ConnectorError("unknown_connector", f"连接器未注册：{connector_id!r}")
    return cls()


# ---------- host 校验钩子（安全约束：全部出站请求，含重定向） ----------


def guard_request(request: httpx.Request) -> None:
    """httpx event hook：发请求前校验目标 URL（scheme + host 全地址解析）。"""
    assert_safe_http_url(str(request.url))


# ---------- 全局限速（spec：连接器 1 req/s） ----------


class RateLimiter:
    """线程安全的最小间隔节流器：acquire() 睡到距上次放行满 min_interval。

    跨连接器实例共享（模块级单例 _GLOBAL_LIMITER），对 fxbaogao 全局生效。
    now 参数是测试注入口（fake clock），生产走 time.monotonic。
    """

    def __init__(self, per_second: float, now: Callable[[], float] = time.monotonic) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._now = now
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> float:
        """放行当前调用，返回实际睡眠的秒数（测试断言用）。"""
        if self._min_interval <= 0:
            return 0.0
        with self._lock:
            wait = self._last + self._min_interval - self._now()
            wait = max(0.0, wait)
            # 以放行时刻推进窗口：睡眠发生在锁内，多线程排队自然串行
            slept = wait
            if wait > 0:
                time.sleep(wait)
            self._last = self._now()
            return slept
