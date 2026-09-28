"""跨测试文件共享的纯 helper 与测试资产常量（无 DB、无 teardown，import 即用）。

与 conftest 的分工：conftest 放 fixture（DB 工厂、环境开关、夹具注入），
本模块放纯函数与常量——LLM mock 工厂、夹具文件名、合成正文样本。
（架构保养⑦上提：原 make_llm 定义在 test_analysis.py 被 4 个文件跨模块
借用；SAMPLES_DIR/DONGWU 在 test_reports.py 与 test_analysis.py 双份且
同名不同义——一边是 PDF 文件名、一边是 LLM 夹具 JSON 名，上提后以
DONGWU_PDF / DONGWU_JSON 区分。）
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import httpx

from app.llm import LLMClient

# 真实研究样本资产（research/ 不入公开仓库；缺席环境相关测试自动跳过）
SAMPLES_DIR = Path(__file__).resolve().parents[2] / "research" / "markitdown-samples"
DONGWU_PDF = "dongwu-002635-anjie-20241231.pdf"

# #15 spike 的 18 份真实 LLM 输出（已拷贝入库，测试可直接读）
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "analysis-spike"
DONGWU_JSON = "dongwu-002635-anjie-20241231.cleaned.fulltext.json"


def fixture_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# 合成研报正文：首页含发布日期（带 PDF 拆字空格）与代码，正文提及 002635
MD_WITH_ANCHORS = (
    "证券研究报告·公司点评·电子\n"
    "安洁科技（002635）动态跟踪点评报告：竞争力稳步提升，静待下游复苏\n"
    "2024 年 12月 31日\n"
    + "公司为国际主流客户提供精密功能件与结构件，消费电子与新能源汽车双轮驱动。" * 200
)


# mock 凭据运行时随机生成（仓库约定：源码不含任何凭据字面量，含假的）
def mock_api_key() -> str:
    return "test-" + secrets.token_hex(8)


def make_llm(responses: list) -> LLMClient:
    """responses 每项：str=正常内容 / int=HTTP 错误码。队列耗尽后重复末项。"""
    queue = list(responses)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, int):
            return httpx.Response(item, text="mock llm error")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": item}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 340},
                "model": "mock-llm",
            },
        )

    client = LLMClient(
        "https://llm-mock.invalid/v1", mock_api_key(), "mock-llm",
        transport=httpx.MockTransport(handler),
    )
    client.mock_calls = calls  # 测试回读：断言调用次数与消息形状
    return client
