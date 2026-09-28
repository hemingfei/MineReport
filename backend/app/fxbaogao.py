"""fxbaogao 连接器（#19 首接）：REST 直连（httpx + Bearer），不跑 MCP 协议。

实测边界（#3/#8，2026-09-28 Key 实测）：
- search：POST /mofoun/agent/search，body {keywords, startTime, pageNum(1-10), orgNames?}；
  响应 data[] 单页 20 条，条目字段 reportId/title/orgName/industryName/pageNum(页数)/
  pubTime(秒级时间戳)/pubTimeStr/paragraphs[]——无作者、无评级（靠 LLM 从 PDF 抽）。
- 标题与命中段落含 <em> 高亮，入库前剥离。
- download：GET /mofoun/agent/download?reportId= 返回 PDF 地址，键名不定
  （pdfurl/url/fileurl/downloadurl 防御性扫描），相对路径拼 dr.fxbaogao.com；
  URL 带时效拿到即下载，只存文件；下载扣账号 VIP 权益额度。
- startTime 支持 last3day/last7day/... 或毫秒时间戳字符串——增量游标用后者。

安全：客户端统一挂 host 校验钩子（connectors.guard_request，event hook 对重定向后
的每个请求都生效）；全局限速 1 req/s（跨实例共享模块级 limiter）。
凭据仅从环境变量 FXBAOGAO_API_KEY（经 Settings）读取。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from urllib.parse import urljoin

import httpx

from .config import get_settings
from .connectors import Connector, RateLimiter, ReportRef, FetchedFile, guard_request, register
from .errors import ConnectorError

log = logging.getLogger("minereport.fxbaogao")

SEARCH_PATH = "/mofoun/agent/search"
DOWNLOAD_PATH = "/mofoun/agent/download"

PAGE_SIZE = 20  # 实测单页 20 条；不足 20 视为末页

_EM_RE = re.compile(r"</?em[^>]*>", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_FILENAME_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

# download 端点响应里的候选键（上游 CLI 源码的防御性扫描集合同款）
_DOWNLOAD_URL_KEYS = ("pdfurl", "url", "fileurl", "downloadurl")

_GLOBAL_LIMITER: RateLimiter | None = None


def global_limiter() -> RateLimiter:
    """模块级单例：所有 FxbaogaoConnector 实例共享（spec：连接器全局 1 req/s）。"""
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is None:
        _GLOBAL_LIMITER = RateLimiter(get_settings().connector_rate_per_second)
    return _GLOBAL_LIMITER


# ---------- 纯解析函数（测试直测，不碰网络） ----------


def strip_highlight(text: str | None) -> str:
    """剥 <em> 高亮标签并去首尾空白。"""
    return _EM_RE.sub("", text or "").strip()


def parse_published_at(entry: dict) -> tuple[dt.datetime | None, dt.date | None]:
    """pubTime（秒级；>1e12 视为毫秒防御）→ (UTC 时刻, 日期)；缺失时回退 pubTimeStr。"""
    raw = entry.get("pubTime")
    if isinstance(raw, (int, float)) and raw > 0:
        seconds = raw / 1000 if raw > 1e12 else raw
        moment = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        return moment, moment.date()
    m = _DATE_RE.search(str(entry.get("pubTimeStr") or ""))
    if m:
        return None, dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None, None


def parse_search_response(payload: dict) -> list[ReportRef]:
    """search 响应 → ReportRef 列表；pubTime 不可解析的条目跳过并告警（不阻断整轮）。"""
    data = payload.get("data")
    if not isinstance(data, list):
        code, msg = payload.get("code"), payload.get("msg") or payload.get("message") or ""
        raise ConnectorError(
            "fxbaogao_bad_response",
            f"search 响应缺 data[]（code={code} msg={str(msg)[:200]}）",
        )
    refs: list[ReportRef] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        external_id = str(entry.get("reportId") or "").strip()
        title = strip_highlight(entry.get("title"))
        if not external_id or not title:
            continue
        moment, publish_date = parse_published_at(entry)
        if publish_date is None:
            log.warning("fxbaogao 条目 %s 无可解析 pubTime，跳过：%r", external_id, entry.get("pubTimeStr"))
            continue
        paragraphs = entry.get("paragraphs") or []
        snippet = "\n".join(
            strip_highlight(p.get("content")) for p in paragraphs if isinstance(p, dict) and p.get("content")
        ) or None
        pages = entry.get("pageNum")
        refs.append(
            ReportRef(
                external_id=external_id,
                title=title,
                publish_date=publish_date,
                published_at=moment,
                broker=strip_highlight(entry.get("orgName")) or None,
                industry=strip_highlight(entry.get("industryName")) or None,
                pages=int(pages) if isinstance(pages, (int, float)) and pages > 0 else None,
                snippet=snippet[:2000] if snippet else None,
                raw=entry,
            )
        )
    return refs


def extract_download_url(payload: dict) -> str | None:
    """download 端点响应 → PDF 地址（候选键防御性扫描，均缺返回 None）。"""
    if isinstance(payload, dict):
        for key in _DOWNLOAD_URL_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def safe_filename(title: str) -> str:
    return _FILENAME_ILLEGAL_RE.sub("_", title)[:80] or "report"


# ---------- 连接器实现 ----------


@register
class FxbaogaoConnector(Connector):
    connector_id = "fxbaogao"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        download_base: str | None = None,
        transport: httpx.BaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        max_pages: int | None = None,
        settings=None,
    ) -> None:
        s = settings or get_settings()
        self._api_key = api_key if api_key is not None else s.fxbaogao_api_key
        if not self._api_key:
            raise ConnectorError(
                "fxbaogao_not_configured", "FXBAOGAO_API_KEY 未配置（凭据仅从环境变量读取）"
            )
        self._download_base = download_base or s.fxbaogao_download_base
        self._limiter = rate_limiter or global_limiter()
        self._max_pages = max_pages or s.connector_max_pages_per_query
        # host 校验钩子永远在（含 MockTransport：host 闸门的回归测试正是借它验证）
        self._client = httpx.Client(
            base_url=(base_url or s.fxbaogao_api_base).rstrip("/"),
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=s.fxbaogao_timeout_seconds,
            transport=transport,
            event_hooks={"request": [guard_request]},
            follow_redirects=True,
        )

    # ---- 内部：限速 + 请求 + 平台错误归一 ----

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self._limiter.acquire()
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise ConnectorError("fxbaogao_unreachable", f"fxbaogao 请求失败：{e}") from e
        if r.status_code == 401:
            raise ConnectorError("fxbaogao_auth", f"鉴权失败（HTTP 401）：{r.text[:200]}")
        if r.status_code != 200:
            raise ConnectorError(
                "fxbaogao_http", f"fxbaogao 返回 {r.status_code}：{r.text[:200]}"
            )
        return r

    # ---- 契约实现 ----

    def discover(
        self,
        query: str,
        since: dt.datetime | None = None,
        *,
        orgs: list[str] | None = None,
    ) -> list[ReportRef]:
        """关键词搜索（元数据先行，不扣下载额度）。startTime：有增量游标传毫秒时间戳，
        否则 last3day；翻页至末页或 max_pages。"""
        start_time = (
            str(int(since.timestamp() * 1000)) if since is not None else "last3day"
        )
        refs: list[ReportRef] = []
        seen: set[str] = set()
        for page in range(1, self._max_pages + 1):
            body: dict = {"keywords": query, "startTime": start_time, "pageNum": page}
            if orgs:
                body["orgNames"] = orgs
            r = self._request("POST", SEARCH_PATH, json=body)
            try:
                payload = r.json()
            except ValueError as e:
                raise ConnectorError(
                    "fxbaogao_bad_response", f"search 响应不是 JSON：{r.text[:200]}"
                ) from e
            page_refs = parse_search_response(payload)
            for ref in page_refs:  # 跨页同 id 去重保序
                if ref.external_id not in seen:
                    seen.add(ref.external_id)
                    refs.append(ref)
            if len(page_refs) < PAGE_SIZE:
                break
        return refs

    def fetch(self, ref: ReportRef) -> FetchedFile:
        """取 PDF：download 端点换 URL（带时效）→ 立即下载字节（此步扣账号下载额度）。"""
        r = self._request("GET", DOWNLOAD_PATH, params={"reportId": ref.external_id})
        try:
            payload = r.json()
        except ValueError as e:
            raise ConnectorError(
                "fxbaogao_bad_response", f"download 响应不是 JSON：{r.text[:200]}"
            ) from e
        url = extract_download_url(payload)
        if url is None:
            raise ConnectorError(
                "fxbaogao_no_download_url",
                f"reportId={ref.external_id} 响应无 PDF 地址键（{_DOWNLOAD_URL_KEYS}）",
            )
        absolute = urljoin(self._download_base, url)
        file_resp = self._request("GET", absolute)
        if not file_resp.content:
            raise ConnectorError("fxbaogao_empty_file", f"reportId={ref.external_id} 下载内容为空")
        return FetchedFile(
            filename=f"{safe_filename(ref.title)}.pdf",
            content_type="application/pdf",
            data=file_resp.content,
        )

    def quota_hint(self) -> str:
        return "fxbaogao 下载将消耗账号的下载权益额度（高级 VIP 权益），确认继续？"

    @classmethod
    def view_url(cls, external_id: str) -> str:
        # 入库主键 reportId 可确定性推导阅读链接（#3 结论：无需存储）
        return f"https://www.fxbaogao.com/view?id={external_id}"

    def close(self) -> None:
        self._client.close()
