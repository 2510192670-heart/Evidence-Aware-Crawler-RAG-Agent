"""同一数据目录只允许一个 API 实例，避免第二个进程误做启动恢复。"""
import os


class InstanceLock:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.handle = (root / 'agent.lock').open('a+b')
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError('同一数据目录已有 API 实例；请仅启动一个 worker。') from None

    def close(self):
        self.handle.close()
