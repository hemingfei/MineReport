"""存储抽象：后续可换 S3/MinIO，先落本地卷实现。"""

from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None:
        """写入文件内容，key 为相对存储根的逻辑路径。"""
        ...

    def get(self, key: str) -> bytes:
        """读取文件内容；key 不存在时抛 FileNotFoundError。"""
        ...

    def exists(self, key: str) -> bool:
        """key 是否已存在（分阶段重试的产物探测）。"""
        ...

    def delete(self, key: str) -> None:
        """删除文件；key 不存在时静默（幂等）。"""
        ...


class LocalStorage:
    """本地卷实现：storage_root 下的按 key 落盘，目录穿透防护。"""

    def __init__(self, root: str) -> None:
        import pathlib

        self._root = pathlib.Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> "pathlib.Path":
        import pathlib

        p = (self._root / key).resolve()
        if not p.is_relative_to(self._root):
            raise ValueError(f"key escapes storage root: {key!r}")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._resolve(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        p = self._resolve(key)
        if not p.is_file():
            raise FileNotFoundError(key)
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        p = self._resolve(key)
        if p.is_file():
            p.unlink()


_storage: Storage | None = None


def get_storage() -> Storage:
    """进程级单例：API 与 worker 共用同一存储根。测试经 set_storage 重绑 tmp 目录。"""
    global _storage
    if _storage is None:
        from .config import get_settings

        _storage = LocalStorage(get_settings().storage_root)
    return _storage


def set_storage(storage: Storage | None) -> None:
    global _storage
    _storage = storage
