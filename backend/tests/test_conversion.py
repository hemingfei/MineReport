"""conversion 纯函数层测试：标题归一、清洗正则、加密/扫描闸门、pymupdf4llm 转换。

夹具用 research/markitdown-samples/ 的真实 PDF（spec Testing Decisions 指定的测试资产）。
"""

from __future__ import annotations

import pytest

from app import conversion
from app.conversion import ConversionError


def _has_samples() -> bool:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "research"
        / "markitdown-samples"
        / "pdf"
        / "dongwu-002635-anjie-20241231.pdf"
    ).exists()


def _pdf(name: str) -> bytes:
    if not _has_samples():
        pytest.skip("research/markitdown-samples 本地资产不在仓库，相关测试跳过")
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "research" / "markitdown-samples" / "pdf" / name
    return p.read_bytes()


# ---------- 标题归一 ----------

def test_normalize_title_fullwidth_space_case() -> None:
    # 全角字母/数字、混排空白、大小写归一后应相等（ADR-0001 缓解误合并）
    a = conversion.normalize_title("安洁科技（002635）：ＡＩ 算力　深度报告")
    b = conversion.normalize_title("安洁科技（002635）：ai算力 深度报告")
    assert a == b
    assert " " not in a and "\u3000" not in a


def test_normalize_title_keeps_raw_meaning() -> None:
    assert conversion.normalize_title("  晨会纪要  ") == "晨会纪要"


# ---------- 清洗正则（spike 移植） ----------

def test_clean_markdown_strips_noise_lines() -> None:
    md = "\n".join(
        [
            "证券研究报告",
            "请务必阅读正文之后的免责条款部分",  # 免责声明
            "- 3 -",  # 页码
            "资料来源：Wind，东吴证券研究所",  # 孤儿来源行
            "图1：全球算力市场规模（亿美元）",  # 孤儿图注
            "2024年12月31日",  # 页眉日期行
            "12.5",  # 孤立数字
            "30%",  # 孤立百分比
            "安洁科技是全球消费电子精密件龙头。",
        ]
    )
    cleaned = conversion.clean_markdown(md)
    assert "安洁科技是全球消费电子精密件龙头。" in cleaned
    for noise in ["免责条款", "资料来源", "- 3 -", "图1", "12.5", "30%"]:
        assert noise not in cleaned, noise


def test_clean_markdown_keeps_gfm_table_separator() -> None:
    """`|---|` 分隔行必须保留（GFM 渲染成表格的必要条件，ADR-0002 教训）。"""
    md = "|盈利预测与估值|2024E|\n|---|---|\n|营业总收入|4199|"
    assert conversion.clean_markdown(md) == md


def test_clean_markdown_strips_picture_text_block() -> None:
    """pymupdf4llm 的图表文字残渣（坐标轴刻度，HTML 注释对包裹）整块剥除。"""
    md = (
        "正文段落。\n"
        "<!-- Start of picture text -->\n"
        "安洁科技 沪深300<br>23%<br>-31%<br>2024/1/2\n"
        "<!-- End of picture text -->\n"
        "后续段落。"
    )
    cleaned = conversion.clean_markdown(md)
    assert "沪深300" not in cleaned and "23%" not in cleaned
    assert "正文段落。" in cleaned and "后续段落。" in cleaned


def test_clean_markdown_strips_template_fields_and_mark() -> None:
    """Word 模板隐藏域 [Table_*] 与 pymupdf4llm 的 <mark> 高亮标记剥除，正文保留。"""
    md = "评级 [Table_Rating] <mark>安洁科技（002635）</mark> 盈利预测 [Table_EPS] 稳步提升。"
    cleaned = conversion.clean_markdown(md)
    assert "[Table_" not in cleaned and "<mark>" not in cleaned
    assert "安洁科技（002635）" in cleaned and "盈利预测" in cleaned and "稳步提升。" in cleaned


def test_clean_markdown_keeps_real_numbers_in_context() -> None:
    md = "2024年营收12.5亿元，同比增长30%。"
    assert conversion.clean_markdown(md) == md


def test_clean_markdown_keeps_short_table_rows() -> None:
    md = "| 指标 | 2024E |"
    assert conversion.clean_markdown(md) == md


# ---------- 前置闸门（真实夹具 PDF） ----------

def test_preflight_normal_pdf_passthrough() -> None:
    data = _pdf("dongwu-002635-anjie-20241231.pdf")
    assert conversion.preflight_pdf(data, "a.pdf") is data  # 未加密：原样返回


def test_preflight_encrypted_empty_password_decrypted() -> None:
    data = _pdf("edge-encrypted.pdf")
    out = conversion.preflight_pdf(data, "a.pdf")
    assert out is not data
    import io

    from pypdf import PdfReader

    r = PdfReader(io.BytesIO(out))
    assert not r.is_encrypted
    assert len(r.pages) == 3


def test_preflight_scanned_rejected() -> None:
    with pytest.raises(ConversionError) as e:
        conversion.preflight_pdf(_pdf("edge-scanned-image-only.pdf"), "a.pdf")
    assert e.value.error_code == "scanned"


def test_preflight_encrypted_scanned_rejected() -> None:
    """回归（code-review Spec 轴）：解密重写不得绕过扫描闸门。"""
    import io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf("edge-scanned-image-only.pdf"))))
    writer.encrypt("")  # 空密码加密的扫描版
    buf = io.BytesIO()
    writer.write(buf)

    with pytest.raises(ConversionError) as e:
        conversion.preflight_pdf(buf.getvalue(), "a.pdf")
    assert e.value.error_code == "scanned"


def test_preflight_encrypted_nonempty_password_rejected(monkeypatch) -> None:
    """空密码解不开的加密件要明确拒收，而不是走到 markitdown 出空串。"""

    class _FakeDecrypt:
        def __getattr__(self, name):  # PasswordType.NOT_DECRYPTED 等
            return 0

    monkeypatch.setattr("pypdf.PdfReader.decrypt", lambda self, pw: 0)
    with pytest.raises(ConversionError) as e:
        conversion.preflight_pdf(_pdf("edge-encrypted.pdf"), "a.pdf")
    assert e.value.error_code == "encrypted"


def test_preflight_scanned_gate_threshold(monkeypatch) -> None:
    """低于每页字符闸门的文本层（平均口径）判为扫描版。"""

    class _Page:
        def __init__(self, n: int) -> None:
            self.n = n

        def extract_text(self) -> str:
            return "字" * self.n

    class _FakeReader:
        is_encrypted = False
        pages = [_Page(10), _Page(0), _Page(5)]  # 平均 5 字/页 < 30

    monkeypatch.setattr(conversion, "PdfReader", lambda src: _FakeReader())
    with pytest.raises(ConversionError) as e:
        conversion.preflight_pdf(b"%PDF-fake", "a.pdf")
    assert e.value.error_code == "scanned"


# ---------- 引擎转换 ----------

@pytest.mark.skipif(not _has_samples(), reason="research/markitdown-samples 本地资产不在仓库，相关测试跳过")
def test_convert_to_markdown_matches_reference() -> None:
    """pymupdf4llm 输出与参考 md 逐字节一致（同版本确定性；参考件由锁定版本引擎生成）。"""
    from pathlib import Path

    ref = (
        Path(__file__).resolve().parents[2]
        / "research"
        / "markitdown-samples"
        / "md"
        / "dongwu-002635-anjie-20241231.pymupdf.md"
    ).read_text(encoding="utf-8")
    out = conversion.convert_to_markdown(
        _pdf("dongwu-002635-anjie-20241231.pdf"), "dongwu-002635-anjie-20241231.pdf"
    )
    assert out == ref


@pytest.mark.skipif(not _has_samples(), reason="research/markitdown-samples 本地资产不在仓库，相关测试跳过")
def test_cleaned_markdown_renders_valuation_table() -> None:
    """端到端质量闸门（ADR-0002）：东吴首页盈利预测表清洗后仍保留表头与分隔行。"""
    out = conversion.convert_to_markdown(
        _pdf("dongwu-002635-anjie-20241231.pdf"), "dongwu-002635-anjie-20241231.pdf"
    )
    cleaned = conversion.clean_markdown(out)
    assert "|盈利预测与估值|" in cleaned
    assert "|---|" in cleaned.replace(" ", "")
    assert "[Table_" not in cleaned


def test_convert_to_markdown_encrypted_input_passes_through_preflight() -> None:
    """加密件经 preflight 解密重写后可直接转换（端到端组合，不静默出空串）。"""
    data = conversion.preflight_pdf(_pdf("edge-encrypted.pdf"), "a.pdf")
    out = conversion.convert_to_markdown(data, "a.pdf")
    assert "安洁科技" in out and len(out) > 1000


def test_convert_to_markdown_unsupported_extension_rejected() -> None:
    with pytest.raises(ConversionError) as e:
        conversion.convert_to_markdown(b"hello", "a.txt")
    assert e.value.error_code == "unsupported_format"
