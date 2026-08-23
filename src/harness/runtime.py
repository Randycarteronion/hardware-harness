"""共享装配层（Runtime Assembly）—— CLI 与 MCP 两个壳共用的唯一实现。

【为什么要有这层】
架构原则："CLI 和 MCP server 只是同一 core 的两个壳，不存在第二套
逻辑"。所有"组装 Registry + 执行动作"的公共流程都收在这里，
cli.py 和 mcp_server.py 只做各自的输入/输出格式适配：

  cli.py       人/CI 壳：参数 -> runtime -> 表格/JSON + 退出码
  mcp_server   Agent 壳：工具调用 -> runtime -> 结构化 JSON（永不抛裸异常）

改设备装配、改错误处理策略，只动这一个文件。
"""

from __future__ import annotations

from pathlib import Path

from .adapters import MockAdapter
from .esp32_adapter import Esp32Adapter
from .registry import DeviceRegistry, RegistryError
from .runner import TestError, TestRunner
from .serial_adapter import SerialAdapter
from .stm32_adapter import Stm32Adapter

# 设备目录默认锚定在仓库内的 examples/devices（相对 __file__ 解析，
# 不依赖 CWD —— MCP server 由 Agent 拉起时 CWD 不可控）
REPO_ROOT = Path(__file__).resolve().parents[2]


def default_devices_dir() -> Path:
    """默认设备目录：环境变量 HARNESS_DEVICES_DIR > 仓库 examples/devices。"""
    import os
    env = os.environ.get("HARNESS_DEVICES_DIR")
    if env:
        return Path(env)
    return REPO_ROOT / "examples" / "devices"


def build_registry(devices_dir: Path | None = None) -> DeviceRegistry:
    """组装 Registry：注册全部 adapter + 加载设备定义。

    【装配点集中原则】以后加新 adapter（CAN、独立 JTAG 探针…）只改
    这里，CLI / MCP / Runner 一行不动。
    """
    registry = DeviceRegistry(adapters={
        MockAdapter.name: MockAdapter(),        # 无硬件演示
        SerialAdapter.name: SerialAdapter(),    # 真串口：命令 + 遥测
        Stm32Adapter.name: Stm32Adapter(),      # STM32：探针烧录 + 串口
        Esp32Adapter.name: Esp32Adapter(),      # ESP32：esptool 烧录 + 串口
    })
    devices_dir = devices_dir or default_devices_dir()
    if devices_dir.is_dir():
        # 约定优先：<devices_dir>/capabilities/ 里的 vendor.* spec 自动入库
        # （B4：第三方能力契约的加载点，设备协商时就能引用）
        specs_dir = devices_dir / "capabilities"
        if specs_dir.is_dir():
            registry.load_specs_dir(specs_dir)
        registry.load_dir(devices_dir)
    return registry


def flash_flow(registry: DeviceRegistry, device_id: str, image: str,
               confirm_token: str | None) -> dict:
    """烧录公共流程：三道闸 -> adapter 执行 -> 统一 dict 结果。

    返回 {ok, message, hint?} —— 两个壳各自决定怎么呈现
    （CLI 打印 + 退出码；MCP 原样返回给 Agent）。
    """
    device = registry.get(device_id)   # 未知设备 -> RegistryError（带已知列表）
    adapter = registry.adapter_for(device)

    # 闸 3（CLI/MCP 层 token）；闸 1（精确串口/探针匹配）和
    # 闸 2（身份核验）在 adapter 内部执行
    if confirm_token != "confirm:flash":
        return {
            "ok": False,
            "message": "refused: flash is destructive",
            "hint": f"verify the right board with the user, then re-call with "
                    f"confirm_token='confirm:flash' (target: {device_id})",
        }

    result = adapter.execute(device, "core.firmware", "flash",
                             {"image": image, "verify": True})
    if not result.ok:
        err = result.error or {}
        return {"ok": False, "message": err.get("message", "?"),
                "hint": err.get("hint", "")}
    return {"ok": True, "message": f"flashed {Path(image).name} -> {device_id} "
                                   f"(identity verified, reset issued)",
            "detail": result.data.get("output", "")[-400:]}


def run_test_flow(registry: DeviceRegistry, test_path: Path,
                  confirm_token: str | None, runs_dir: Path) -> dict:
    """跑测试剧本的公共流程。

    返回值两种：
      成功执行（无论 PASS/FAIL）-> {"ok": True, "report": <完整报告 dict>}
      执行出错（加载失败/缺 token）-> {"ok": False, "message", "hint"}
    注意：PASS/FAIL 是"测试结果"，不是"执行错误"——Agent 靠
    report["status"] 判断，ok=False 只用于"根本没跑起来"。
    """
    runner = TestRunner(registry, runs_dir)
    try:
        script = runner.load_script(test_path)   # 结构错误在这里拦
        report = runner.run(script, confirm_token=confirm_token)
        return {"ok": True, "report": report}
    except (RegistryError, TestError) as e:
        return {"ok": False, "message": str(e),
                "hint": getattr(e, "hint", "") or ""}


__all__ = ["build_registry", "flash_flow", "run_test_flow", "default_devices_dir"]
