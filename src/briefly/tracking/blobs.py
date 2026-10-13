"""原始文件版本库：按 SHA-256 内容寻址，相同内容只存一份。

写入先落临时文件再改名，进程中断不会留下半截文件（只会留下可清理的临时文件）。
目录建好后收紧权限（POSIX 下仅当前用户可读写；Windows 下沿用用户目录的 ACL）。
"""
import hashlib
import os
from pathlib import Path

from .. import config


class StoreFull(Exception):
    """版本库总量达到上限。"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore:
    def __init__(self, root: str | Path | None = None, max_bytes: int | None = None) -> None:
        self.root = Path(root or config.TRACKING_DIR) / "blobs"
        self.max_bytes = max_bytes if max_bytes is not None else int(config.TRACKING_MAX_STORE_MB * 1024 * 1024)
        self._used: int | None = None  # 第一次需要时统计，之后增量维护

    def _ensure_root(self) -> None:
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.root, 0o700)
                os.chmod(self.root.parent, 0o700)
            except OSError:
                pass

    def path(self, digest: str) -> Path:
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise ValueError(f"非法的内容哈希: {digest!r}")  # 防止拼出版本库以外的路径
        return self.root / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def put(self, data: bytes, digest: str | None = None) -> str:
        """存入并返回哈希；已存在时不重复写。超过总量上限时抛 StoreFull。"""
        digest = digest or sha256_bytes(data)
        target = self.path(digest)
        if target.is_file():
            return digest
        if self.max_bytes and self.used_bytes() + len(data) > self.max_bytes:
            raise StoreFull(f"版本库已达上限 {config.TRACKING_MAX_STORE_MB:g}MB，未保存新版本；可运行 briefly track prune 清理")
        self._ensure_root()
        target.parent.mkdir(exist_ok=True)
        tmp = target.with_name(f"{digest}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        if self._used is not None:
            self._used += len(data)
        return digest

    def get(self, digest: str) -> bytes:
        return self.path(digest).read_bytes()

    def delete(self, digest: str) -> None:
        self.path(digest).unlink(missing_ok=True)
        self._used = None

    def used_bytes(self) -> int:
        if self._used is None:
            files = self.root.glob("*/*") if self.root.exists() else []
            self._used = sum(p.stat().st_size for p in files if p.is_file())
        return self._used

    def all_digests(self) -> set[str]:
        if not self.root.exists():
            return set()
        return {p.name for p in self.root.glob("*/*") if p.is_file() and not p.name.endswith(".tmp")}

    def clean_temp(self) -> int:
        """删除中断写入留下的临时文件。"""
        count = 0
        if self.root.exists():
            for p in self.root.glob("*/*.tmp"):
                p.unlink(missing_ok=True)
                count += 1
        return count
