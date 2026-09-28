"""转换管道（#13）：pypdf 前置闸门 → markitdown 转正文 → spike 正则清洗。

- 加密 PDF：pypdf 空密码解密重写；解不开明确拒收（error_code=encrypted），
  绝不让 markitdown 静默吞掉（实测加密件它会正常出文/空串不可预期）。
- 扫描版：pypdf 逐页字符数闸门，平均低于 scan_min_chars_per_page 判扫描拒收
  （markitdown 对扫描版静默返回空串，#2 研究实测结论）。
- 清洗正则自 #6 spike 原样移植（压掉 17~23% 字符且主题更收敛）。
"""

from __future__ import annotations

import io
import re
import tempfile
import unicodedata
from pathlib import Path

from pypdf import PasswordType, PdfReader, PdfWriter

SUPPORTED_EXTENSIONS = (".pdf", ".docx")

# 清洗规则（#6 spike FOOTER_PATTERNS 移植，语义见各注释）
_FOOTER_PATTERNS = [
    r"请务必阅读正文之后的免责条款部分",
    r"^[-—]*\s*\d+\s*[-—]*$",  # 页码行
    r"^资料来源[:：].*$",  # 孤儿来源行（图已丢失）
    r"^图\s*[:：]?.{0,40}$",  # 孤儿图注
    r"^\(?\d{4}[ /年]\d{1,2}[ /月]\d{1,2}[日)]?.{0,10}$",  # 页眉日期行
]


class ConversionError(Exception):
    """带错误码的转换失败：error_code 稳定可编程判断（测试与前端提示都用它）。

    stage 由 worker 按失败位置回填（preflight/convert/persist），供分阶段重试观测。
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.stage: str | None = None


def normalize_title(title: str) -> str:
    """组合键用的标题归一：NFKC（全角→半角等）+ 去全部空白 + casefold（ADR-0001 缓解）。"""
    normalized = unicodedata.normalize("NFKC", title)
    normalized = "".join(normalized.split())
    return normalized.casefold()


def clean_markdown(md: str) -> str:
    """正则级去噪：剥页眉页脚/免责声明/页码/孤儿图注/孤立数字行，不做结构恢复。"""
    lines = md.splitlines()
    kept = []
    for ln in lines:
        stripped = ln.strip()
        if any(re.search(p, stripped) for p in _FOOTER_PATTERNS):
            continue
        # 丢弃纯分隔行 | --- | --- |
        if re.fullmatch(r"\|[\s\-|:]+\|", stripped):
            continue
        # 丢弃孤立数字/百分比行（图表坐标轴残留）
        if re.fullmatch(r"[\d\s%.\-]+", stripped) and len(stripped) < 20:
            continue
        kept.append(ln)
    return "\n".join(kept)


def preflight_pdf(data: bytes, filename: str) -> bytes:
    """PDF 前置闸门：返回可供 markitdown 消费的字节（原样或解密重写）。

    非加密件返回原对象（恒等判断可测）；加密件空密码解不开抛 encrypted；
    每页平均字符数低于闸门抛 scanned（对解密后的页面跑，加密扫描版同样拒收）。
    """
    reader = PdfReader(io.BytesIO(data))
    decrypted: bytes | None = None
    if reader.is_encrypted:
        if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise ConversionError(
                "encrypted", "加密 PDF 且空密码无法解密，请提供未加密版本"
            )
        buf = io.BytesIO()
        PdfWriter(clone_from=reader).write(buf)
        decrypted = buf.getvalue()

    from .config import get_settings

    pages = reader.pages
    total_chars = sum(len(page.extract_text() or "") for page in pages)
    if pages and total_chars / len(pages) < get_settings().scan_min_chars_per_page:
        raise ConversionError(
            "scanned",
            f"疑似扫描版/图片型 PDF（每页平均 {total_chars / len(pages):.0f} 字符），"
            "请提供文字版或另行 OCR",
        )
    return decrypted if decrypted is not None else data


def convert_to_markdown(data: bytes, filename: str) -> str:
    """markitdown 转正文（pdf 经 pdfminer、docx 经 mammoth）；不支持的后缀明确拒收。"""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ConversionError(
            "unsupported_format", f"不支持的文件类型 {ext}，仅支持 PDF/DOCX"
        )

    from markitdown import MarkItDown

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = MarkItDown().convert(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return result.text_content
