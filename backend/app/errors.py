"""管道错误基类：带稳定 error_code（可编程判断），worker 落任务 result。

ConversionError（转换管道）保持原位不动；AnalysisError 供分析/LLM 管道使用，
两者形状一致，worker 统一捕获。
"""

from __future__ import annotations


class PipelineError(Exception):
    error_code = "internal"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.stage: str | None = None


class AnalysisError(PipelineError):
    """分析管道失败：llm_not_configured / markdown_missing / llm_http /
    llm_invalid_json / schema_invalid / report_missing 等。"""


class MasterDataError(PipelineError):
    """主数据导入失败（#16）：akshare 接口超时 / 申万 xls 拉取失败 / 行数据非法等。"""


class ConnectorError(PipelineError):
    """连接器失败（#19）：凭据未配置 / 平台 HTTP 或鉴权错误 / 响应不可解析 /
    下载 URL 缺失 / host 不安全等。调度器据此走退避重试。"""
