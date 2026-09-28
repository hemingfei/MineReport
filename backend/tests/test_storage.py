"""存储抽象（本地卷实现）的行为测试：put/get/delete 往返、幂等删除、目录穿越防护。"""

from pathlib import Path

import pytest

from app.storage import LocalStorage


def test_put_get_roundtrip(tmp_path: Path) -> None:
    s = LocalStorage(str(tmp_path))
    s.put("reports/1/file.pdf", b"%PDF-1.4 fake")
    assert s.get("reports/1/file.pdf") == b"%PDF-1.4 fake"


def test_get_missing_raises(tmp_path: Path) -> None:
    s = LocalStorage(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        s.get("no/such/file.bin")


def test_delete_is_idempotent(tmp_path: Path) -> None:
    s = LocalStorage(str(tmp_path))
    s.put("a/b.txt", b"x")
    s.delete("a/b.txt")
    s.delete("a/b.txt")  # 不存在时静默
    with pytest.raises(FileNotFoundError):
        s.get("a/b.txt")


def test_key_escape_rejected(tmp_path: Path) -> None:
    s = LocalStorage(str(tmp_path))
    for bad in ("../escape.txt", "a/../../escape.txt"):
        with pytest.raises(ValueError):
            s.put(bad, b"x")
