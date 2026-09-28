"""存储抽象：后续可换 S3/MinIO，先落本地卷实现。"""

from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None:
        """写入文件内容，key 为相对存储根的逻辑路径。"""
        ...

    def get(self, key: str) -> bytes:
        """读取文件内容；key 不存在时抛 FileNotFoundError。"""
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

    def delete(self, key: str) -> None:
        p = self._resolve(key)
        if p.is_file():
            p.unlink()
