"""转换管道（#13）：pypdf 前置闸门 → 引擎出正文 → 正则清洗。

- PDF 走 pymupdf4llm（版式/表格识别，选型见 ADR-0002）：markitdown 的
  词坐标"表单启发式"会把研报首页复杂版式切成碎管道表，已弃用（ADR-0002）。
- docx 仍走 markitdown（mammoth）。
- 加密 PDF：pypdf 空密码解密重写；解不开明确拒收（error_code=encrypted），
  绝不让引擎静默吞掉（实测加密件它会正常出文/空串不可预期）。
- 扫描版：pypdf 逐页字符数闸门，平均低于 scan_min_chars_per_page 判扫描拒收
  （引擎对扫描版静默返回空串，#2 研究实测结论）。
- 清洗正则自 #6 spike 移植并随引擎换代调整（保留 GFM 表格分隔行、
  剥图表文字残渣块与 Word 模板隐藏域）。
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
    """正则级去噪：剥页眉页脚/免责声明/页码/孤儿图注/图表文字残渣/隐藏域，不做结构恢复。

    注意：`|---|` 表格分隔行必须保留——GFM 渲染成 <table> 的必要条件（ADR-0002 教训）。
    """
    # pymupdf4llm 的图表文字残渣（坐标轴刻度等，包在 HTML 注释对里）整块剥除
    md = re.sub(
        r"<!--\s*Start of picture text\s*-->.*?<!--\s*End of picture text\s*-->\n?",
        "",
        md,
        flags=re.DOTALL,
    )
    # Word 模板隐藏域残留（东吴系模板的 [Table_EPS]/[Table_Tag] 等）
    md = re.sub(r"\s*\[Table_\w+\]", "", md)
    # pymupdf4llm 的高亮标记，只留正文
    md = md.replace("<mark>", "").replace("</mark>", "")
    lines = md.splitlines()
    kept = []
    for ln in lines:
        stripped = ln.strip()
        if any(re.search(p, stripped) for p in _FOOTER_PATTERNS):
            continue
        # 丢弃孤立数字/百分比行（页码、图表坐标轴残留）
        if re.fullmatch(r"[\d\s%.\-]+", stripped) and len(stripped) < 20:
            continue
        kept.append(ln)
    return "\n".join(kept)


def preflight_pdf(data: bytes, filename: str) -> bytes:
    """PDF 前置闸门：返回可供转换引擎消费的字节（原样或解密重写）。

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
    """引擎出正文：PDF 走 pymupdf4llm（ADR-0002）、docx 走 markitdown（mammoth）；不支持的后缀明确拒收。"""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ConversionError(
            "unsupported_format", f"不支持的文件类型 {ext}，仅支持 PDF/DOCX"
        )

    if ext == ".pdf":
        import pymupdf
        import pymupdf4llm

        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return pymupdf4llm.to_markdown(doc, show_progress=False)

    from markitdown import MarkItDown

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = MarkItDown().convert(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return result.text_content
