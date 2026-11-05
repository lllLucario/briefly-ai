"""按键加的互斥锁：同一个键依次执行，没有人占用也没有人等待时就释放，不随会话数量一直增长。只在本进程内有效。"""
import threading


class _Hold:
    """一次加锁。with 进入时才计数，所以只取 locks[key] 不会留下记录。"""

    __slots__ = ("_locks", "_key")

    def __init__(self, locks: "KeyedLocks", key: str) -> None:
        self._locks, self._key = locks, key

    def __enter__(self) -> "_Hold":
        self._locks._acquire(self._key)
        return self

    def __exit__(self, *_) -> None:
        self._locks._release(self._key)


class KeyedLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, list] = {}  # 键 → [锁, 占用和等待的人数]

    def __getitem__(self, key: str) -> _Hold:
        return _Hold(self, key)

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)

    def _acquire(self, key: str) -> None:
        with self._guard:
            entry = self._entries.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1  # 等待之前先计数：持有者释放时看到还有人在等，就不会删掉这把锁
        entry[0].acquire()

    def _release(self, key: str) -> None:
        with self._guard:
            entry = self._entries[key]
            entry[0].release()
            entry[1] -= 1
            if entry[1] == 0:
                del self._entries[key]
