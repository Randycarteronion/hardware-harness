"""builder（B2）与 DeviceLock（B3）单测 —— 不碰真设备真工具链。

构建用临时目录里的假命令（python 一行脚本）模拟成功/失败/假成功
三种情况；锁测试覆盖互斥、释放、陈锁回收。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from harness.builder import build_flow
from harness.session import DeviceLock


# ------------------------------------------------------------------ builder

def _device(tmp_path: Path, command: str, artifact: str | None = "out.bin") -> dict:
    return {
        "id": "dev_build_test",
        "match": {"adapter": "stm32"},
        "capabilities": [{
            "capability": "core.firmware",
            "binding": {"tool": "stlink_cli", "build": {
                "command": command, "artifact": artifact,
                "working_dir": str(tmp_path)}},
        }],
        "telemetry": {"format": "line_json", "fields": {}},
    }


def test_build_success_produces_artifact(tmp_path):
    art = tmp_path / "out.bin"
    cmd = f'"{sys.executable}" -c "open(\'out.bin\',\'wb\').write(b\'FW\')"'
    result = build_flow(_device(tmp_path, cmd, "out.bin"))
    assert result["ok"] is True
    # artifact 返回解析后的绝对路径（以 working_dir 为基准）
    assert Path(result["artifact"]).resolve() == art.resolve()
    assert art.read_bytes() == b"FW"


def test_build_failure_returns_compiler_output(tmp_path):
    cmd = f'"{sys.executable}" -c "import sys; print(\'main.c(12): error: undeclared identifier rpm\'); sys.exit(2)"'
    result = build_flow(_device(tmp_path, cmd))
    assert result["ok"] is False
    assert "exit 2" in result["message"]
    assert "undeclared identifier" in result["output"]  # 编译错误原文必须带回


def test_build_exit0_but_no_artifact_is_failure(tmp_path):
    """工具链坑：脚本失败但退出码 0 —— 靠 artifact 存在性拦截。"""
    result = build_flow(_device(tmp_path, "echo pretending to build", "out.bin"))
    assert result["ok"] is False
    assert "artifact not found" in result["message"]


def test_device_without_build_config(tmp_path):
    device = {"id": "x", "match": {"adapter": "stm32"},
              "capabilities": [{"capability": "core.firmware",
                                "binding": {"tool": "stlink_cli"}}],
              "telemetry": {"format": "line_json", "fields": {}}}
    result = build_flow(device)
    assert result["ok"] is False
    assert "no build config" in result["message"]


# -------------------------------------------------------------------- locks

def test_lock_mutual_exclusion(tmp_path):
    """跨进程互斥：用一个真实存活的子进程模拟"别的进程占着设备"。

    注意不能用同进程的两个 DeviceLock 模拟 —— 锁是同 pid 可重入的
    （见 session.py 注释），同进程互斥本来就不该发生。
    """
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lockfile = tmp_path / "dev_a.lock"
        lockfile.write_text(json.dumps(
            {"pid": child.pid, "owner": "mcp-session", "t": time.time()}),
            encoding="utf-8")
        b = DeviceLock("dev_a", locks_dir=tmp_path)
        err = b.acquire(owner="another-cli")
        assert err is not None and err["code"] == "E_DEVICE_BUSY"
        assert "mcp-session" in err["hint"]      # 错误里能看到占用者是谁
    finally:
        child.kill()
        child.wait()


def test_lock_same_pid_reentrant(tmp_path):
    """同进程重入：MCP server 每次工具调用重建 adapter，不能自己锁死自己。"""
    a = DeviceLock("dev_r", locks_dir=tmp_path)
    b = DeviceLock("dev_r", locks_dir=tmp_path)
    assert a.acquire(owner="first-call") is None
    assert b.acquire(owner="second-call") is None   # 同 pid：直接共享
    a.release()


def test_lock_released_twice_is_safe(tmp_path):
    lock = DeviceLock("dev_b", locks_dir=tmp_path)
    lock.acquire()
    lock.release()
    lock.release()  # 幂等，不抛
    assert not (tmp_path / "dev_b.lock").exists()


def test_stale_lock_from_dead_process_is_reclaimed(tmp_path):
    """崩溃恢复：占用者进程已死 -> 陈锁自动清理，新持有者直接拿锁。"""
    stale = tmp_path / "dev_c.lock"
    # 写一个"占用者是早已不存在的进程"的陈锁
    dead_pid = os.getpid() + 100_000   # 当前 pid 偏移，几乎不可能存活
    stale.write_text(json.dumps({"pid": dead_pid, "owner": "crashed", "t": 0}),
                     encoding="utf-8")
    lock = DeviceLock("dev_c", locks_dir=tmp_path)
    assert lock.acquire(owner="recovery") is None   # 陈锁被清，抢锁成功
    lock.release()


def test_lock_context_manager(tmp_path):
    with DeviceLock("dev_d", locks_dir=tmp_path) as lock:
        assert lock._held
    assert not (tmp_path / "dev_d.lock").exists()
