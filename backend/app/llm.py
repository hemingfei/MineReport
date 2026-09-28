"""OpenAI 兼容 chat 端点客户端（#15 分析管道的外部依赖缝合点）。

httpx 直连而非 openai SDK：chat/completions 协议面很小，直连少一个重依赖，
且 transport 可注入——测试用 httpx.MockTransport 预录响应（spec 次缝合口 1），
不碰网络。凭据只从 Settings（环境变量 LLM_*）读取，源码不写字面量。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from .errors import AnalysisError
from .urlguard import assert_safe_http_url


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str


class LLMClient:
    """POST {base_url}/chat/completions。transport 参数是测试专用注入口：
    注入 MockTransport 时跳过 host 闸门（MockTransport 不发真请求）。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if transport is None:
            try:
                assert_safe_http_url(base_url)
            except ValueError as e:
                raise AnalysisError("unsafe_llm_url", f"LLM 端点不安全：{e}") from e
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def chat(
        self, messages: list[dict], temperature: float = 0.1, json_mode: bool = True
    ) -> ChatResult:
        payload: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as e:
            raise AnalysisError("llm_unreachable", f"LLM 端点请求失败：{e}") from e

        # 部分兼容端点不支持 response_format：降级重试一次（spike 同款兜底）
        if r.status_code == 400 and json_mode and "response_format" in r.text:
            return self.chat(messages, temperature=temperature, json_mode=False)
        if r.status_code != 200:
            raise AnalysisError(
                "llm_http", f"LLM 端点返回 {r.status_code}：{r.text[:300]}"
            )

        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise AnalysisError(
                "llm_invalid_response", f"LLM 响应缺 choices/message：{str(data)[:300]}"
            ) from e
        usage = data.get("usage") or {}
        return ChatResult(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            model=data.get("model") or self.model,
        )


def parse_llm_json(content: str) -> dict:
    """LLM 输出 → dict：剥 markdown 围栏；整体解析失败时回退首个 {...} 提取
    （spike 验证过的恢复路径，模型偶尔在 JSON 前后带解释文字）。"""
    import re

    txt = content.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", txt)
        txt = re.sub(r"\n?```\s*$", "", txt)
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.S)
        if m is None:
            raise AnalysisError(
                "llm_invalid_json", f"LLM 输出无法解析为 JSON：{content[:200]}"
            )
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise AnalysisError(
                "llm_invalid_json", f"LLM 输出无法解析为 JSON：{content[:200]}"
            ) from e
    if not isinstance(obj, dict):
        raise AnalysisError("llm_invalid_json", "LLM 输出不是 JSON 对象")
    return obj
