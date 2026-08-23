"""Session Manager —— 设备独占锁（backlog B3，架构文档承诺的模块）。

【解决什么问题】
一台物理设备同一时刻只能有一个主人。没有锁会发生：
  - 两个 Agent 会话同时 run-test，互相发命令、复位对方正在跑的测试
  - CLI 的 monitor 占着串口，MCP 的 flash 端口打不开（OS 会挡串口，
    但"串口 + 探针"的组合操作 OS 挡不住 —— 探针操作可以和别人的
    串口会话打架）

【实现：跨进程文件锁】
锁文件落在 ~/.hardware-harness/locks/<device_id>.lock，内容 JSON：
  {"pid": 占用者进程, "owner": 谁（cli/mcp/run-test）, "t": 获取时刻}

  - 抢锁用 O_CREAT|O_EXCL 原子创建（两个进程同时抢，只有一个成功）
  - 撞锁时检查占用者进程是否还活着：活着 -> 报 E_DEVICE_BUSY（带
    owner 信息，Agent 能理解"谁在用"）；死了 -> 陈锁，自动清掉重抢
    （进程崩溃后不留死锁的关键设计）
  - 崩溃场景靠陈锁自动回收兜底；正常路径 disconnect 时释放

【Windows 进程存活检查的坑】
不能用 os.kill(pid, 0)：Windows 上 os.kill 非 0 信号会直接
TerminateProcess —— 检查动作本身会杀死对方。必须用 ctypes 的
OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) 查询。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    """跨平台查进程是否存活。Windows 走 OpenProcess（见模块注释）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False  # 打不开：进程不存在或无权限（按不存在处理）
            try:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        # POSIX：信号 0 只探测不发送，标准做法
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError):
        return False


class DeviceLock:
    """单台设备的独占锁。acquire 失败不抛异常，返回错误 dict ——
    调用方（adapter）好把它转成带 hint 的 ActionResult。"""

    def __init__(self, device_id: str,
                 locks_dir: Path | None = None):
        self.device_id = device_id
        self.locks_dir = locks_dir or Path.home() / ".hardware-harness" / "locks"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.locks_dir / f"{device_id}.lock"
        self._held = False

    def acquire(self, owner: str = "unknown") -> dict | None:
        """抢锁。成功返回 None（占用中标记 _held），失败返回错误 dict。

        【同进程可重入】锁文件里 pid == 当前进程时直接视为"已是我的"：
        MCP server 每次工具调用重建 Registry/Adapter，若同进程互斥，
        第二次工具调用就会自己锁死自己。代价：进程内第一个
        disconnect 会放下整把锁（进程内并发由串口 OS 层互斥兜底，
        真正要防的是跨进程，这里语义正确）。
        """
        if self._held:
            return None  # 同一实例重复 acquire 幂等

        payload = json.dumps({"pid": os.getpid(), "owner": owner,
                              "t": time.time()}).encode()

        for _ in range(3):  # 陈锁清理后重试，最多 3 次（并发清理竞态兜底）
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                stale = self._read_owner()
                if stale and stale["pid"] == os.getpid():
                    self._held = True  # 同进程重入：这台设备本来就归我
                    return None
                if stale and _pid_alive(stale["pid"]):
                    return {
                        "code": "E_DEVICE_BUSY",
                        "message": f"device '{self.device_id}' is in use",
                        "hint": f"held by {stale['owner']} (pid {stale['pid']}, "
                                f"since {time.strftime('%H:%M:%S', time.localtime(stale['t']))}); "
                                f"wait for it to finish or stop that process",
                    }
                # 占用者已死：清陈锁重抢（两个进程同时清可能都失败，靠循环重试）
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            self._held = True
            return None
        return {"code": "E_LOCK_RACE",
                "message": "lock contention while cleaning stale lock; retry",
                "hint": "just retry the operation"}

    def release(self) -> None:
        """释放锁。不是自己的锁不动（防误删别人的）。"""
        if not self._held:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._held = False

    def _read_owner(self) -> dict | None:
        """读锁文件内容；损坏的锁文件按陈锁处理（返回 None 触发清理）。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "pid" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def __enter__(self):
        err = self.acquire()
        if err:
            raise RuntimeError(f"{err['code']}: {err['message']}")
        return self

    def __exit__(self, *exc):
        self.release()
