"""Adapter 层（防腐层）。

【这是什么】
Adapter 把"能力动作"翻译成具体的传输/工具调用：
  core.firmware/flash  ->  stm32cubeprogrammer CLI / pyocd / esptool ...
  core.link/send       ->  Gateway 的串口写命令
上层（Test Runner、Agent、CLI）永远只说 capability/action，完全
不知道底下跑的是 st-flash 还是 esptool。

【铁律】
Gateway / 烧录器的原生协议不得泄漏到本模块之上。换烧录工具 =
写新 Adapter 或改 binding，测试剧本和 Agent 调用一个字都不用改。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass
class ActionResult:
    """所有 adapter 动作的统一返回信封。

    ok=False 时 error 必须带 code/message/hint/retryable —— hint 是
    给 LLM 的自我修复提示（比如"端口被占用，先 disconnect"）。
    """
    ok: bool
    data: dict = field(default_factory=dict)
    error: dict | None = None


class Adapter:
    """每个 Adapter 必须实现的接口 —— 只有两个入口，别加第三个。

    execute         : 一次性动作（reset / send / flash ...）
    read_telemetry  : 拉取最新遥测样本（返回"原始值已按 scale/offset 换算"的 dict）
    read_log        : 取设备最近输出（失败时作为报告里的 context 证据）
    """

    name: str

    def supports(self, capability: str, version_req: str) -> bool:
        """声明本 adapter 实现了哪些能力契约，Registry 加载设备时用来协商。"""
        raise NotImplementedError

    def execute(self, device: dict, capability: str, action: str, params: dict) -> ActionResult:
        raise NotImplementedError

    def read_telemetry(self, device: dict, keys: list[str] | None = None) -> dict:
        """返回 {field: 换算后的值}。keys=None 表示全部字段。"""
        raise NotImplementedError

    def read_log(self, device: dict, max_lines: int = 50) -> list[str]:
        raise NotImplementedError


def _apply_scale(fields: dict, raw: dict) -> dict:
    """按设备定义里的 telemetry.fields 做 原始值 -> 物理值 换算。

    物理值 = 原始值 * scale + offset
    例如 MCU 上报 rpm 原始值 1243、scale=1 → 1243 rpm；
    温度原始值 372、scale=0.1 → 37.2 degC。这样固件端只发整数，
    Agent 端拿到的永远是带正确量纲的数。
    """
    out = {}
    for key, value in raw.items():
        spec = fields.get(key, {})
        out[key] = value * spec.get("scale", 1) + spec.get("offset", 0)
    return out


class MockAdapter(Adapter):
    """模拟一台"说 line_json 遥测的电机控制 MCU"。Phase 0 专用。

    【为什么需要它】
    Phase 0 的目标是让 Build->Flash->Run->Observe->Test 全链路在
    没有真硬件、没有真 Gateway 的情况下跑通 —— 核心逻辑（Registry、
    Runner、报告、schema）全部先行验证，Phase 1 只替换 adapter。

    【物理模型怎么实现的】
    收到 'start' 命令时记下 t0；之后每次读遥测，rpm 按一阶惯性
    系统计算：rpm(t) = 目标转速 * (1 - e^(-t/τ))，目标 1243、
    τ=0.5s —— 模拟真实电机"逐渐加速到稳态"的惯性。
    温度按 rpm 比例从 25°C 漂移到 37.2°C。
    reset/stop 后 rpm 从当前值冻结（惯性衰减略去，够用）。
    """

    name = "mock"  # MockAdapter：无硬件全链路演示；真串口见 serial_adapter，烧录见 stm32_adapter

    def __init__(self) -> None:
        # 每个设备的模拟状态：是否在转、启动时刻 t0、输出日志、连接标志
        self._devices: dict[str, dict] = {}
        self._target_rpm = 1243.0   # 稳态转速（故意不是整千，测试才有区分度）
        self._target_temp = 37.2    # 稳态绕组温度 degC

    def supports(self, capability: str, version_req: str) -> bool:
        # Mock 声明实现全部五个 core.* 契约（build/verify 等只做记忆动作）
        return capability in {"core.lifecycle", "core.firmware", "core.link", "core.telemetry", "core.test"}

    def execute(self, device: dict, capability: str, action: str, params: dict) -> ActionResult:
        # setdefault：首次接触该设备时初始化模拟状态
        state = self._devices.setdefault(
            device["id"], {"running": False, "t0": None, "log": [], "connected": False, "rpm0": 0.0}
        )

        # ---- 生命周期动作：大部分只改状态机，reset 顺带把电机停掉 ----
        if capability == "core.lifecycle":
            if action in ("connect", "reset", "discover"):
                if action == "reset":
                    state.update(running=False, t0=None, rpm0=0.0)
                    state["log"].append("[mock] device reset")
                else:
                    state["connected"] = True
                return ActionResult(True, {})
            if action == "disconnect":
                state["connected"] = False
                return ActionResult(True, {})
            if action == "power_cycle":
                state.update(running=False, t0=None, rpm0=0.0)
                state["log"].append("[mock] power cycled")
                return ActionResult(True, {})

        # ---- 固件动作：flash 只记日志 + 停机（真实现会先做 identity_check 再调烧录器）----
        if capability == "core.firmware":
            if action == "flash":
                state.update(running=False, t0=None, rpm0=0.0)
                state["log"].append(f"[mock] flashed {params.get('image', '?')} (identity_check passed)")
                return ActionResult(True, {"flashed": params.get("image")})

        # ---- 通道动作：解析 ASCII 行命令 start/stop，未知命令返回 NAK ----
        if capability == "core.link" and action == "send":
            data = params.get("data", "")
            state["log"].append(f"> {data.strip()}")
            if data.strip() == "start":
                # 记录启动时刻，遥测从这一刻开始按惯性曲线爬升
                state.update(running=True, t0=time.monotonic())
                state["log"].append("OK motor started")
                return ActionResult(True, {"reply": "OK motor started", "matched": True})
            if data.strip() == "stop":
                # 停机时把当前转速冻结到 rpm0，读数不会瞬间归零
                state.update(running=False, rpm0=self._raw_rpm(state))
                state["log"].append("OK motor stopped")
                return ActionResult(True, {"reply": "OK motor stopped", "matched": True})
            state["log"].append("ERR unknown command")
            # 错误信封示范：code 稳定可编程匹配，retryable 告诉 Agent 值不值得重试
            return ActionResult(False, error={"code": "E_DEVICE_NAK", "message": "device rejected command", "retryable": True})

        return ActionResult(False, error={"code": "E_UNSUPPORTED_ACTION",
                                          "message": f"{self.name} cannot execute {capability}/{action}",
                                          "retryable": False})

    def _raw_rpm(self, state: dict) -> float:
        """一阶惯性模型计算当前 rpm（未换算物理单位，原始值即 rpm）。"""
        if not state["running"] or state["t0"] is None:
            return state.get("rpm0", 0.0)
        t = time.monotonic() - state["t0"]
        return self._target_rpm * (1 - math.exp(-t / 0.5))

    def _raw_temp(self, state: dict) -> float:
        """温度模型：与转速成比例地从 25°C 升到 37.2°C（热惯性的简化）。"""
        rpm = self._raw_rpm(state)
        return 25.0 + (self._target_temp - 25.0) * min(rpm / self._target_rpm, 1.0)

    def read_telemetry(self, device: dict, keys: list[str] | None = None) -> dict:
        """取最新样本：算原始值 -> 按 scale/offset 换算 -> 追加一行模拟 UART 输出。"""
        state = self._devices.get(device["id"], {})
        raw = {"rpm": round(self._raw_rpm(state), 1), "temp_c": round(self._raw_temp(state), 2)}
        if keys:
            raw = {k: v for k, v in raw.items() if k in keys}
        fields = device.get("telemetry", {}).get("fields", {})
        scaled = _apply_scale(fields, raw)
        # 模拟固件的 line_json 输出，同时进日志（capture/assert 时可回看）
        line = " ".join(f'{k}={v}' for k, v in scaled.items())
        state.setdefault("log", []).append(line)
        return scaled

    def read_log(self, device: dict, max_lines: int = 50) -> list[str]:
        """返回最近 N 行设备输出 —— 测试失败时的现场证据。"""
        state = self._devices.get(device["id"], {})
        log = state.get("log", [])
        return log[-max_lines:]
