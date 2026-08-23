"""SerialAdapter 集成测试 —— 不插真板，用假传输层验证串口链路。

【怎么做到无硬件测串口】
SerialAdapter 的构造函数接受 transport_factory 注入。这里注入
FakeMotorTransport：接口和真串口一致（open/close/write/read_line/
drain/log），内部用一个后台线程模拟 MCU —— 收到 start 后每 50ms
主动吐一行 JSON 遥测（和真实固件行为相同：数据是异步推过来的，
不是查询才有）。物理模型复用 MockAdapter 的一阶惯性曲线。

这验证的是 SerialAdapter 真正干活的代码路径：后台收行、行解析、
scale 换算、expect 正则匹配、reset 清样本 —— 不是 mock 自己测自己。
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from harness.registry import DeviceRegistry
from harness.runner import TestRunner
from harness.serial_adapter import SerialAdapter

DEVICE_YAML = """
schema_version: 1
id: fake_motor
name: 假串口电机板
description: 测试用
match:
  adapter: serial
capabilities:
  - capability: core.lifecycle
    binding: {reset_line: rts}
  - capability: core.link
    binding: {baudrate: 115200, port: FAKE}
  - capability: core.telemetry
    binding: {source: uart0, format: line_json}
telemetry:
  format: line_json
  fields:
    rpm: {type: uint16, unit: rpm}
    temp_c: {type: float32, scale: 0.1, unit: degC}
"""

# 带 boot_time 的变体：验证复位后自动等待（经验 L2 的回归测试）
DEVICE_YAML_BOOT = DEVICE_YAML.replace(
    "binding: {baudrate: 115200, port: FAKE}",
    "binding: {baudrate: 115200, port: FAKE, boot_time: 300ms}")


class FakeMotorTransport:
    """模拟 MCU 的串口行为：异步推行 + 命令响应。

    物理模型与 MockAdapter 相同：start 后 rpm 一阶惯性爬向 1243
    （tau=0.5s）；温度原始值从 250 爬向 372（yaml scale=0.1 → 25~37.2 degC）。
    """

    def __init__(self):
        self.log: deque[str] = deque(maxlen=500)
        self._queue: queue.Queue[str] = queue.Queue()
        self._alive = False
        self._running = False
        self._t0: float | None = None
        self._rpm0 = 0.0
        self._thread: threading.Thread | None = None

    # ---- 传输层接口（与 SerialTransport 一致）----
    def open(self) -> None:
        self._alive = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._alive = False
        self._running = False

    def write(self, data: str) -> None:
        # 收到命令立刻产生响应行（真实 MCU 在命令解析线程里回复）
        self.log.append(f"> {data.strip()}")
        cmd = data.strip()
        if cmd == "start":
            self._running = True
            self._t0 = time.monotonic()
            self._push("OK motor started")
        elif cmd == "stop":
            self._rpm0 = self._rpm()
            self._running = False
            self._push("OK motor stopped")
        else:
            self._push("ERR unknown command")

    def read_line(self, timeout: float) -> str | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    def pulse_reset(self, line: str = "rts", hold: float = 0.05) -> None:
        # 复位：停机 + 清状态（Adapter 侧还会清最新样本）
        self._running = False
        self._t0 = None
        self._rpm0 = 0.0
        self.log.append("[fake] reset")

    # ---- 模拟物理 ----
    def _rpm(self) -> float:
        if not self._running or self._t0 is None:
            return self._rpm0
        t = time.monotonic() - self._t0
        return 1243.0 * (1 - math.exp(-t / 0.5))

    def _raw_temp(self) -> float:
        frac = min(self._rpm() / 1243.0, 1.0)
        return 250.0 + (372.0 - 250.0) * frac  # 原始值，*0.1 后为 degC

    def _push(self, line: str) -> None:
        self.log.append(line)
        self._queue.put(line)

    def _loop(self) -> None:
        """后台遥测线程：running 时每 50ms 主动吐一行 JSON（模拟固件定时器）。"""
        while self._alive:
            time.sleep(0.05)
            if not self._running:
                continue
            if self._queue.qsize() > 100:  # 没人消费时别无限堆积
                continue
            self._push(json.dumps({"rpm": round(self._rpm(), 1),
                                   "temp_c": round(self._raw_temp(), 1)}))


@pytest.fixture
def registry(tmp_path):
    adapter = SerialAdapter(transport_factory=lambda device: FakeMotorTransport())
    reg = DeviceRegistry(adapters={"serial": adapter})
    device_file = tmp_path / "fake_motor.yaml"
    device_file.write_text(DEVICE_YAML, encoding="utf-8")
    reg.load_device(device_file)
    return reg


def _script(steps=None):
    """构造与 examples/tests/motor_start.yaml 等价的剧本（直接 dict，不走文件）。"""
    return {
        "schema_version": 1,
        "name": "serial_motor_start",
        "device": "fake_motor",
        "timeout": "30s",
        "retry": 0,
        "on_failure": "abort",
        "steps": steps or [
            {"action": "reset"},
            {"action": "send", "params": {"channel": "uart0", "data": "start\n", "expect": "^OK"}},
            {"action": "wait", "params": {"duration": "2s"}},
            {"action": "assert", "params": {"metric": "rpm", "op": "gt", "value": 1000,
                                             "within": "5s", "settle": "200ms"}},
            {"action": "capture", "params": {"source": "uart0", "duration": "500ms", "artifact": "uart_log"}},
        ],
    }


def test_send_expect_and_scale(registry):
    """命令期望回复 + 定点遥测换算：temp_c 原始 3xx * scale 0.1 -> 3x.xx degC。"""
    device = registry.get("fake_motor")
    adapter = registry.adapter_for(device)
    assert adapter.execute(device, "core.lifecycle", "connect", {}).ok

    result = adapter.execute(device, "core.link", "send",
                             {"channel": "uart0", "data": "start\n", "expect": "^OK"})
    assert result.ok and result.data["matched"] is True

    time.sleep(1.0)  # 让转速爬一会儿（1s 时约 86% 稳态）
    sample = adapter.read_telemetry(device)
    assert sample["rpm"] > 800                       # 一阶惯性：1s ≈ 1065 rpm
    assert 30 < sample["temp_c"] < 40                # 原始 ~3xx 经 scale=0.1
    adapter.execute(device, "core.lifecycle", "disconnect", {})


def test_full_script_over_serial(registry, tmp_path):
    """全链路：TestRunner 走串口 adapter 完整跑剧本（含 capture artifact）。"""
    runner = TestRunner(registry, tmp_path / "runs")
    report = runner.run(_script())
    assert report["status"] == "PASS"
    assert report["steps"][3]["detail"]["measured"] > 1000
    assert report["steps"][3]["detail"]["unit"] == "rpm"
    assert report["artifacts"], "capture 步骤必须产出 artifact"
    art_path = Path(report["artifacts"][0]["path"])
    assert art_path.exists() and art_path.stat().st_size > 0
    # artifact 是原始遥测行，逐行可解析
    first_line = json.loads(art_path.read_text(encoding="utf-8").splitlines()[0])
    assert "rpm" in first_line


def test_boot_time_waits_after_reset(tmp_path):
    """经验 L2：声明了 boot_time 的设备，reset 后要等够时长再返回。"""
    adapter = SerialAdapter(transport_factory=lambda device: FakeMotorTransport())
    reg = DeviceRegistry(adapters={"serial": adapter})
    f = tmp_path / "boot.yaml"
    f.write_text(DEVICE_YAML_BOOT, encoding="utf-8")
    reg.load_device(f)
    device = reg.get("fake_motor")
    adapter.execute(device, "core.lifecycle", "connect", {})

    t0 = time.monotonic()
    adapter.execute(device, "core.lifecycle", "reset", {})
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.3, f"boot_time 未生效: reset 仅耗时 {elapsed:.3f}s"
    adapter.execute(device, "core.lifecycle", "disconnect", {})


def test_noise_lines_ignored(registry):
    """串口噪声行（启动 banner 等非 JSON）不炸解析、不污染遥测。"""
    device = registry.get("fake_motor")
    adapter = registry.adapter_for(device)
    adapter.execute(device, "core.lifecycle", "connect", {})
    tr = adapter._tr["fake_motor"]
    tr._push("STM32 Bootloader v2.3 (c) ST")   # 模拟上电 banner 噪声
    tr._push("not json at all !!!")
    sample = adapter.read_telemetry(device)
    assert sample == {}                          # 噪声行被静默跳过，最新样本仍为空
