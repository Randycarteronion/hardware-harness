"""固件构建封装 —— core.firmware.build 动作的实现（backlog B2）。

【为什么需要它】
Agent 自主闭环的最后一环：改代码（Agent 自己）-> 编译（本模块）->
烧录（adapter flash）-> 验证（TestRunner）。之前 build 是手工脚本，
Agent 只能"假设固件已编译"，现在它自己能编。

【设计】
构建命令属于"设备怎么造固件"的知识 —— 按架构归设备定义的
firmware binding：

    capabilities:
      - capability: core.firmware
        binding:
          tool: stlink_cli
          build:
            command: "bash firmware/stm32/f103_motor_demo/build.sh"
            artifact: "firmware/stm32/f103_motor_demo/fw.bin"   # 构建产物
            working_dir: "."                                     # 可选

规则：
  - command 用 shell 执行（设备定义是本地开发者的私有配置，
    等同于自己写 Makefile；不接受来自 Agent 的任意命令）
  - 构建成功 = 退出码 0 且 artifact 声明了就真的存在
    （防止"编译失败但返回 0"的工具链坑）
  - 完整输出截尾 2000 字符返回 —— Agent 排编译错误够用

【放独立模块的原因】
runtime.py 要 import 全部 adapter，adapter 又要委托 build —— 
放 runtime 会循环依赖。本模块零 harness 内依赖，谁都能 import。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def firmware_binding(device: dict) -> dict:
    """取设备定义里 core.firmware 能力的 binding（构建配置在里面）。"""
    for cap in device.get("capabilities", []):
        if cap["capability"] == "core.firmware":
            return cap.get("binding", {})
    return {}


def build_flow(device: dict, timeout_s: int = 600) -> dict:
    """执行设备声明的构建命令。返回 {ok, message, hint?, artifact?, output?}。

    无论成败都带 output 尾部 —— 编译错误对 Agent 是第一手排错材料。
    """
    build = firmware_binding(device).get("build")
    if not build or "command" not in build:
        return {
            "ok": False,
            "message": f"device '{device['id']}' declares no build config",
            "hint": "add binding.build.command (+artifact) to the core.firmware "
                    "capability in the device definition",
        }

    command: str = build["command"]
    artifact: str | None = build.get("artifact")
    working_dir: str | None = build.get("working_dir")

    # artifact 相对路径以 working_dir 为基准解析（否则构建在 working_dir
    # 产出、检查却在 harness 的 cwd 找 —— 真板验证时踩过的路径错位）
    artifact_path: Path | None = None
    if artifact:
        artifact_path = Path(artifact)
        if not artifact_path.is_absolute() and working_dir:
            artifact_path = Path(working_dir) / artifact_path

    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",   # 工具链可能输出 GBK（L14）
            timeout=timeout_s,
            cwd=working_dir or None,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"build timed out after {timeout_s}s",
                "hint": "check for a hanging build step (linker lock, disk full)"}

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = output[-2000:]

    if proc.returncode != 0:
        return {"ok": False,
                "message": f"build command failed (exit {proc.returncode})",
                "hint": "read 'output' for the compiler error; fix the source "
                        "code and build again",
                "output": tail}

    # 退出码 0 也要验产物存在 —— 有的脚本失败时忘了 set -e
    if artifact_path and not artifact_path.exists():
        return {"ok": False,
                "message": f"build returned 0 but artifact not found: {artifact_path}",
                "hint": "check the build script's output path vs binding.build.artifact",
                "output": tail}

    return {"ok": True,
            "message": "build succeeded",
            "artifact": str(artifact_path) if artifact_path else None,
            "output": tail}
