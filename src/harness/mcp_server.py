"""Hardware Harness MCP Server —— 把硬件能力交给 AI Agent（Phase 2）。

【这是什么】
MCP（Model Context Protocol）是 Agent 工具接入的事实标准。本模块把
Hardware Harness 的核心操作暴露为 6 个 MCP 工具，任何支持 MCP 的
Agent Host（DSH / Claude Code / Codex / ZCode…）零改动接入：

  Agent 说"把电机改到 1000 RPM 并验证"
    -> 改代码、编译（Agent 自己的能力）
    -> hardware_flash(...)          烧到真板（要向用户要 confirm_token）
    -> hardware_run_test(...)       跑测试剧本
    -> 读报告里的 measured/unit      不达标就继续改

【设计要点】
1. 与 CLI 共用 runtime.py —— 一套实现两个壳，本文件只做协议适配
2. 永不向 Agent 抛裸异常：所有错误转 {ok:False, message, hint}
   —— hint 是给 LLM 的自我修复指令
3. destructive 闸门在 MCP 上同样生效：flash 必须带 confirm_token，
   Agent 拿不到 token 就必须去问人类 —— 这是安全模型的核心
4. 工具 docstring 就是 LLM 看到的工具说明，写得越准 Agent 用得越对

【启动】stdio transport（Agent 拉起子进程的标准方式）：
  harness-mcp          # 或 python -m harness.mcp_server
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .runtime import build_registry, flash_flow, run_test_flow

mcp = FastMCP("hardware-harness")

# ---------------------------------------------------------------------------
# 工具函数注册表 —— 一份实现，两个消费方：
#   1. MCP server（Agent Host 经 stdio 调用，FastMCP 装饰器负责协议）
#   2. 内置 Agent（harness agent 命令，agent.py 直接调 Python 函数）
# 用 _tool() 代替裸 @mcp.tool()：函数体保持纯 Python（返回 JSON 字符串），
# 注册进 TOOL_FUNCS 供内置 Agent 复用 —— 协议壳和大脑永远看到同一套行为。
# ---------------------------------------------------------------------------
TOOL_FUNCS: dict[str, callable] = {}


def _tool(fn):
    TOOL_FUNCS[fn.__name__] = fn
    return mcp.tool()(fn)


# ---------------------------------------------------------------- helpers

def _e(message: str, hint: str = "") -> str:
    """错误统一信封：结构化 JSON 字符串（LLM 友好）。"""
    return json.dumps({"ok": False, "message": message, "hint": hint}, ensure_ascii=False)


def _ok(data: dict) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ tools

@_tool
def hardware_list_devices() -> str:
    """列出可用的硬件设备（Device）。返回每个设备的 id、名称、adapter、
    能力清单（capabilities）和安全等级。操作具体设备前先调这个。
    设备能力决定它能做什么：core.firmware=可烧录固件，core.link=可发
    串口命令，core.telemetry=可读带单位的遥测（rpm/degC 等）。"""
    try:
        registry = build_registry()
    except Exception as e:  # 设备文件损坏等装配错误
        return _e(f"registry load failed: {e}", "check device yaml files in the devices dir")
    devices = []
    for dev in registry.devices.values():
        devices.append({
            "id": dev["id"],
            "name": dev["name"],
            "adapter": dev["match"]["adapter"],
            "capabilities": [c["capability"] for c in dev["capabilities"]],
            "safety": dev.get("safety", {}).get("class", ""),
        })
    return _ok({"devices": devices})


@_tool
def hardware_get_info(device_id: str) -> str:
    """读取一台设备的完整定义：描述（怎么和它交互）、遥测字段与单位、
    命令格式、串口参数、安全注意事项。给设备发命令或断言遥测之前
    必须先调这个，了解字段名和单位（比如 temp_c 是 degC、raw x10）。"""
    try:
        registry = build_registry()
        device = registry.get(device_id)
    except Exception as e:
        return _e(str(e), "call hardware_list_devices first to see valid ids")
    return _ok({"device": device})


@_tool
def hardware_discover() -> str:
    """枚举本机物理硬件资源：串口列表（含 VID/PID/序列号）和 SWD 调试
    探针列表。用于设备接线核验和填写设备定义。没有 VID 的串口是
    蓝牙虚拟口，不是真实设备。"""
    registry = build_registry()
    serial_adapter = registry.adapters.get("serial")
    result = serial_adapter.execute({"id": "_", "capabilities": []},
                                    "core.lifecycle", "discover", {})
    out = {"serial_ports": result.data.get("ports", [])}

    # 探针枚举用线程 + 超时兜底（Windows libusb 偶发挂死，L9）
    probes = None
    try:
        from .stm32_adapter import HAVE_PYOCD
        if HAVE_PYOCD:
            import concurrent.futures
            from pyocd.core.helpers import ConnectHelper
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    probes = pool.submit(ConnectHelper.get_all_connected_probes).result(timeout=3)
                except concurrent.futures.TimeoutError:
                    probes = None
    except ImportError:
        pass
    out["debug_probes"] = (
        [{"unique_id": p.unique_id, "description": str(p.description or "")} for p in probes]
        if probes is not None else "unavailable (pyocd missing or libusb timeout)"
    )
    return _ok(out)


@_tool
def hardware_build(device_id: str) -> str:
    """执行设备声明的固件构建（binding.build.command，如 make/CMake/
    build.sh）。返回构建输出尾部（编译错误原文在里面，直接读它改代码）
    和 artifact 固件路径。构建成功后通常接 hardware_flash 烧录。
    注意：构建在宿主机跑，不碰设备，安全（无需 token）。"""
    from .builder import build_flow
    try:
        registry = build_registry()
        device = registry.get(device_id)
    except Exception as e:
        return _e(str(e), "call hardware_list_devices first")
    result = build_flow(device)
    # 输出截尾 1500 字符 —— 编译错误信息对 Agent 最有价值，不能全丢
    result["output"] = result.get("output", "")[-1500:]
    return _ok(result) if result["ok"] else _e(result["message"], result.get("hint", ""))


@_tool
def hardware_deploy(device_id: str, script: str, target: str = "main.py") -> str:
    """部署应用脚本到 MicroPython 设备（写设备文件系统，不动固件，安全）。
    部署后自动复位，脚本立即生效（target=main.py 时开机自启）。
    典型用途：改了设备端 Python 逻辑后快速迭代 —— deploy + monitor，
    比完整 flash 固件快得多。"""
    if not Path(script).exists():
        return _e(f"script not found: {script}", "pass an existing .py path")
    try:
        registry = build_registry()
        device = registry.get(device_id)
        adapter = registry.adapter_for(device)
    except Exception as e:
        return _e(str(e), "call hardware_list_devices first")
    result = adapter.execute(device, "core.firmware", "install",
                             {"script": script, "target": target})
    if not result.ok:
        err = result.error or {}
        return _e(err.get("message", "?"), err.get("hint", ""))
    return _ok(result.data)


@_tool
def hardware_flash(device_id: str, image: str, confirm_token: str = "") -> str:
    """[DESTRUCTIVE] 烧录固件到真实设备。会覆盖设备 flash 中的现有程序！

    安全闸门（三道，缺一不可）：
    1. confirm_token 必须是 'confirm:flash' —— 拿不到就向用户说明
       目标设备并请求确认，绝不能编造 token
    2. 设备身份核验（adapter 内部自动执行，如 MAC/DeviceID 比对）
    3. 烧录后自动校验 + 向量检查 + 复位

    image 是 .bin 文件路径（相对当前目录或绝对路径）。
    成功后设备复位并立即运行新固件。"""
    if not Path(image).exists():
        return _e(f"image not found: {image}", "check the build output path")
    try:
        registry = build_registry()
        result = flash_flow(registry, device_id, image, confirm_token or None)
    except Exception as e:
        return _e(str(e), "call hardware_list_devices to check the device id")
    return _ok(result) if result["ok"] else _e(result["message"], result.get("hint", ""))


@_tool
def hardware_monitor(device_id: str, duration_s: int = 3) -> str:
    """连接设备并采集一段时间的遥测（read-only，安全）。返回采样数组
    （已按 scale 换算、带单位语义）和原始串口日志尾部。用于观察设备
    当前状态、验证固件是否在正常输出。"""
    try:
        registry = build_registry()
        device = registry.get(device_id)
        adapter = registry.adapter_for(device)
    except Exception as e:
        return _e(str(e), "call hardware_list_devices first")

    r = adapter.execute(device, "core.lifecycle", "connect", {})
    if not r.ok:
        err = r.error or {}
        return _e(f"connect failed: {err.get('message', '?')}", err.get("hint", ""))
    try:
        samples = []
        end = time.monotonic() + max(min(duration_s, 30), 0.5)  # 钳 0.5~30s
        while time.monotonic() < end:
            s = adapter.read_telemetry(device)
            if s:
                samples.append(s)
            time.sleep(0.2)
        return _ok({
            "samples": samples[-20:],               # 尾部 20 个足够判断趋势
            "log_tail": adapter.read_log(device, 10),
        })
    finally:
        adapter.execute(device, "core.lifecycle", "disconnect", {})


@_tool
def hardware_run_test(test_path: str, confirm_token: str = "") -> str:
    """在真实设备上执行硬件测试剧本（YAML），返回结构化报告：
    每步结果、断言实测值与单位、失败时的串口现场、artifact 路径。

    判断标准：返回的 report.status == 'PASS' 才算通过；'FAIL' 表示
    断言不满足（看 steps 里 FAIL 步骤的 detail.measured 实测值来定位
    差多少）；ok=false 表示剧本没跑起来（按 hint 修复）。

    剧本含 flash/power_cycle 等危险步骤时必须传 confirm_token
    'confirm:<剧本名>'（先向用户确认）。剧本格式见 schemas/test.schema.json，
    7 种原语：reset/send/wait/assert/capture/power_cycle/run。"""
    path = Path(test_path)
    if not path.exists():
        return _e(f"test script not found: {test_path}",
                  "path relative to cwd, or absolute; see examples/tests/")
    try:
        registry = build_registry()
        result = run_test_flow(registry, path, confirm_token or None, Path("runs"))
    except Exception as e:
        return _e(str(e), "check the test script and devices dir")
    if not result["ok"]:
        return _e(result["message"], result.get("hint", ""))
    report = result["report"]
    # context 可能很长，给 Agent 截尾部（完整版在落盘的 report.json 里）
    report["context"] = report.get("context", [])[-15:]
    return _ok({"report": report})


# ------------------------------------------------------------------- main

def main() -> None:
    """stdio 入口：Agent Host 以子进程方式拉起本函数。"""
    mcp.run()


if __name__ == "__main__":
    main()
