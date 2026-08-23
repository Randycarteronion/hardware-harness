"""Stm32Adapter：组合适配器 = 调试探针（烧录/复位） + 串口（命令/遥测）。

【架构意义】
一块真实 STM32 开发场景要用两条物理链路：
  - SWD 调试探针（ST-Link）：flash / 硬件复位 / 读芯片身份
  - USB 串口（或板载 VCP）：发命令、收 line_json 遥测
本适配器把两者拧在一起对外呈现为"一个设备的全部能力"，
这正是 Adapter 的本职 —— 上层不需要知道物理上是两条线。

【双工具路径：binding.tool 选择烧录后端】
  tool: pyocd（默认）      —— pyocd CLI，要求 ST-Link 固件 >= V2J24
  tool: stlink_cli         —— STM32 ST-LINK Utility 自带的 ST-LINK_CLI.exe，
                              原生兼容老固件的山寨 ST-Link
为什么需要两条路（真板教训，2026-08-23）：山寨 ST-Link 常见固件
V2J16M4，pyocd 直接 ProbeError 拒连；升级探针固件有变砖风险且要
人点 GUI。而用户机器上现成的 ST-LINK_CLI v1.4.0 对老固件工作正常，
于是把"工具选择"下沉为设备 binding 的一个字段 —— 这正是当初
capability/binding 分离设计的用途：换工具不改测试剧本。

【防烧错板】
  1. 烧录前 identity_check：pyocd 路径核验探针序列号；stlink_cli
     路径核验 Device ID（如 F103 = 0x410）+ flash 容量
  2. CLI 层 confirmation token
不满足任一即硬失败。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .adapters import ActionResult, Adapter
from .serial_adapter import SerialAdapter

# pyocd 很重（带一堆依赖），做成可选安装：pip install 'hardware-harness[stm32]'
try:
    from pyocd.core.helpers import ConnectHelper

    HAVE_PYOCD = True
except ImportError:
    HAVE_PYOCD = False


class StlinkCliTool:
    """ST-LINK_CLI.exe 封装（老固件 ST-Link 的零风险烧录通道）。

    v1.4.0 实测注意：
      - 不支持 -ListStlink（列出探针）—— 多探针场景无法选择，单探针没问题
      - 退出码不可靠（有些失败也返回 0），成败要靠输出文本判断
    """

    # 默认安装路径按顺序探测；binding.exe 可显式指定
    DEFAULT_EXES = [
        r"C:\Program Files (x86)\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe",
        r"C:\Program Files\STMicroelectronics\STM32 ST-LINK Utility\ST-LINK Utility\ST-LINK_CLI.exe",
    ]

    def __init__(self, exe: str | None = None):
        if exe:
            self.exe = exe
        else:
            self.exe = next((p for p in self.DEFAULT_EXES if Path(p).exists()), None)

    def available(self) -> bool:
        return self.exe is not None

    def run(self, args: list[str], timeout_s: int = 180) -> str:
        """跑一条 ST-LINK_CLI 命令，返回完整输出（stdout+stderr 合并）。

        抛异常 = 进程级失败（超时/找不到 exe）；业务失败由调用方解析输出。
        """
        if not self.available():
            raise RuntimeError("ST-LINK_CLI.exe not found; install STM32 ST-LINK Utility "
                               "or set binding.exe in the device definition")
        proc = subprocess.run([self.exe, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s)
        return (proc.stdout or "") + (proc.stderr or "")

    # ------------------------------------------------------------- 身份

    def identity(self) -> dict:
        """连一次 SWD，解析 Device ID 和 flash 容量。

        输出形如：
          Connected via SWD.
          Device ID:0x410
          Device flash Size : 64 Kbytes
          Device family :STM32F10x Medium-density
        """
        out = self.run(["-c", "SWD"])
        if "Connected via" not in out:
            raise RuntimeError(f"cannot connect via SWD: {out.strip()[-200:]}")
        dev_id = re.search(r"Device ID\s*:\s*(0x[0-9A-Fa-f]+)", out)
        flash = re.search(r"flash Size\s*:\s*(\d+)\s*Kbytes", out)
        return {
            "device_id": dev_id.group(1) if dev_id else None,
            "flash_kb": int(flash.group(1)) if flash else None,
        }

    def flash(self, image: str, offset: str = "0x08000000") -> str:
        """编程 + 校验 + 复位。ST-LINK_CLI 参数串联一次完成。"""
        return self.run(["-c", "SWD", "-P", image, offset, "-V", "-Rst"])

    def reset(self) -> str:
        """硬件复位（-HardRst 拉 NRST，比 -Rst 软复位更彻底）。"""
        return self.run(["-c", "SWD", "-HardRst"])

    def read_mem8(self, address: int, num_bytes: int) -> bytes:
        """读一片 flash/RAM 内存（v1.4.0 用 -r8，不支持 -Dump）。

        【经验功能化：L13 的回读验证】烧录后想物理确认"芯片里真的
        是这个固件"，靠的就是这个命令。输出形如：
          0x08000000 : 00  50  00  20  41  00  00  08 ...
        按行解析地址 + 16 进制字节流，返回原始 bytes。
        """
        out = self.run(["-c", "SWD", "-r8", hex(address), str(num_bytes)])
        data = bytearray()
        for m in re.finditer(r"^0x[0-9A-Fa-f]{8}\s*:\s*((?:[0-9A-Fa-f]{2}\s*)+)$",
                             out, re.MULTILINE):
            data.extend(int(b, 16) for b in m.group(1).split())
        if len(data) < num_bytes:
            raise RuntimeError(f"-r8 readback incomplete: got {len(data)}/{num_bytes} bytes")
        return bytes(data)


class Stm32Adapter(Adapter):
    """实现全部五个 core.* 契约：firmware/reset 走探针，其余委托串口。"""

    name = "stm32"

    def __init__(self, serial_adapter: SerialAdapter | None = None):
        # 串口子适配器可注入（测试用）；默认自建一个真实串口的
        self._serial = serial_adapter or SerialAdapter()
        self._cli_tool = StlinkCliTool()

    # ------------------------------------------------------------ 契约

    def supports(self, capability: str, version_req: str) -> bool:
        # firmware/reset 只有本适配器能做；link/telemetry 串口也能做 —— 都声明支持
        return capability in {"core.lifecycle", "core.firmware", "core.link", "core.telemetry", "core.test"}

    # ------------------------------------------------------------ 分发

    def execute(self, device: dict, capability: str, action: str, params: dict) -> ActionResult:
        if capability == "core.firmware":
            if action == "flash":
                return self._flash(device, params)
            if action == "build":
                # 构建是纯宿主机操作（不碰设备），两个 adapter 共用 builder
                return self._build(device)
            return ActionResult(False, error={"code": "E_UNSUPPORTED_ACTION",
                                              "message": f"stm32 adapter firmware/{action} not implemented yet",
                                              "retryable": False})
        if capability == "core.lifecycle" and action == "reset":
            return self._reset(device)
        # 其余动作（connect/discover/send/...）全部走串口子适配器
        return self._serial.execute(device, capability, action, params)

    # ---------------------------------------------------------- 遥测委托

    def read_telemetry(self, device: dict, keys: list[str] | None = None) -> dict:
        return self._serial.read_telemetry(device, keys)

    def read_log(self, device: dict, max_lines: int = 50) -> list[str]:
        return self._serial.read_log(device, max_lines)

    # -------------------------------------------------------- 工具选择

    def _binding(self, device: dict) -> dict:
        for cap in device.get("capabilities", []):
            if cap["capability"] == "core.firmware":
                return cap.get("binding", {})
        return {}

    def _use_cli(self, device: dict) -> bool:
        """binding.tool == 'stlink_cli' 时走 ST-LINK_CLI（老固件路径）。"""
        return self._binding(device).get("tool") == "stlink_cli"

    def _probe_serial(self, device: dict) -> str | None:
        """pyocd 路径的目标探针序列号。"""
        for cap in device.get("capabilities", []):
            if cap["capability"] == "core.firmware":
                sn = cap.get("binding", {}).get("probe_serial")
                if sn:
                    return sn
        return device.get("match", {}).get("serial_number")

    # -------------------------------------------------------- 身份核验

    def _identity_check(self, device: dict) -> ActionResult | None:
        """烧录前的身份核验，按工具路径分派。

        返回 None = 通过；返回 ActionResult = 失败（调用方直接短路）。
        stlink_cli 路径的核验依据是 Device ID + flash 容量 —— 比 pyocd
        路径的探针序列号弱（同型号芯片 ID 相同），但配合"这台机器只
        插了这一个探针"的实际约束已经够用；v1.4.0 的 CLI 列不出探针
        序列号，是已知限制（见 docs/lessons.md L12）。
        """
        want = device.get("identity_check") or {}
        try:
            if self._use_cli(device):
                got = self._cli_tool.identity()
                for key in ("device_id",):
                    if want.get(key) and (got.get(key) or "").lower() != str(want[key]).lower():
                        return ActionResult(False, error={
                            "code": "E_IDENTITY_MISMATCH",
                            "message": f"{key} mismatch: yaml expects {want[key]}, chip answered {got.get(key)}",
                            "hint": "wrong board on the probe, or fix identity_check in the device definition",
                            "retryable": False,
                        })
                if want.get("flash_kb") and got.get("flash_kb") != int(str(want["flash_kb"])):
                    return ActionResult(False, error={
                        "code": "E_IDENTITY_MISMATCH",
                        "message": f"flash size mismatch: yaml expects {want['flash_kb']}KB, "
                                   f"chip answered {got.get('flash_kb')}KB",
                        "hint": "wrong chip variant (e.g. C8 vs CB); fix identity_check.flash_kb",
                        "retryable": False,
                    })
                return None

            # ---- pyocd 路径：核验探针序列号 ----
            if not HAVE_PYOCD:
                return ActionResult(False, error={
                    "code": "E_PYOCD_MISSING",
                    "message": "pyocd is not installed",
                    "hint": "pip install 'hardware-harness[stm32]', or switch the device to "
                            "tool: stlink_cli (works with old ST-Link firmware)",
                    "retryable": False,
                })
            want_sn = self._probe_serial(device)
            if not want_sn:
                return ActionResult(False, error={
                    "code": "E_NO_PROBE_SPECIFIED",
                    "message": "device defines no probe_serial; refusing to flash blind",
                    "hint": "run 'harness discover' to list probes, then set binding.probe_serial",
                    "retryable": False,
                })
            probes = ConnectHelper.get_all_connected_probes()
            if not [p for p in probes if p.unique_id == want_sn]:
                available = ", ".join(p.unique_id for p in probes) or "<none>"
                return ActionResult(False, error={
                    "code": "E_IDENTITY_MISMATCH",
                    "message": f"probe '{want_sn}' not connected",
                    "hint": f"connected probes: {available}",
                    "retryable": False,
                })
            return None
        except RuntimeError as e:  # ST-Link_CLI 进程级失败（连不上等）
            return ActionResult(False, error={
                "code": "E_IDENTITY_UNREADABLE",
                "message": str(e),
                "hint": "check SWD wiring (SWDIO/SWCLK/GND/3V3) and that the probe's LED is on",
                "retryable": True,
            })

    # -------------------------------------------------------- 烧录/复位

    def _build(self, device: dict) -> ActionResult:
        """委托 builder 执行设备声明的构建命令（见 builder.py）。"""
        from .builder import build_flow   # 延迟 import，避免装配期开销
        result = build_flow(device)
        if not result["ok"]:
            return ActionResult(False, error={
                "code": "E_BUILD_FAILED",
                "message": result["message"],
                "hint": result.get("hint", ""),
                "retryable": True,
            })
        return ActionResult(True, {k: v for k, v in result.items() if k != "ok"})

    def _flash(self, device: dict, params: dict) -> ActionResult:
        # 闸 2：身份核验（闸 1 = 精确探针/串口匹配，闸 3 = CLI token）
        guard = self._identity_check(device)
        if guard is not None:
            return guard

        image = params.get("image", "")
        try:
            if self._use_cli(device):
                out = self._cli_tool.flash(image, self._binding(device).get("offset", "0x08000000"))
                # v1.4.0 退出码不可靠，按输出关键词判成败
                if "Error" in out or "error" in out or "failed" in out.lower():
                    return ActionResult(False, error={
                        "code": "E_FLASH_FAILED",
                        "message": f"ST-LINK_CLI failed: {out.strip()[-400:]}",
                        "hint": "check image path/offset; SWD wiring; target may need "
                                "connect-under-reset if app disables SWD pins",
                        "retryable": False,
                    })
                # ---- 向量表健全性检查（经验功能化）----
                # 烧录"成功"≠固件能启动：这次开发就在向量表上踩过两个坑
                # （--first 放错位置、入口符号未定义），-V 校验只保证
                # "写进去的字节对"，不保证"字节排列是可启动的镜像"。
                # 所以烧完回读 8 字节做结构检查：
                #   SP 落在 SRAM 区（0x20000000 起 1MB 内）
                #   PC 是 Thumb 地址（奇数）且落在 flash 范围内
                # 不满足 = 镜像布局错误，直接判失败，省掉一次"烧完不跑
                # 然后人肉排查"的循环。
                guard = self._vector_sanity(device)
                if guard is not None:
                    return guard
                return ActionResult(True, {"output": out.strip()[-400:]})

            # ---- pyocd 路径 ----
            probe = self._probe_serial(device)
            cmd = [sys.executable, "-m", "pyocd", "flash",
                   "--probe", probe, "--nowait", image]
            return self._run(cmd, timeout_s=300)
        except subprocess.TimeoutExpired:
            return ActionResult(False, error={
                "code": "E_FLASH_TIMEOUT",
                "message": "flash tool timed out",
                "hint": "check probe connection; a hung target may need power cycle",
                "retryable": True,
            })
        except RuntimeError as e:
            return ActionResult(False, error={"code": "E_FLASH_FAILED", "message": str(e),
                                              "retryable": False})

    def _reset(self, device: dict) -> ActionResult:
        try:
            if self._use_cli(device):
                out = self._cli_tool.reset()
                if "Connected via" not in out:
                    return ActionResult(False, error={"code": "E_RESET_FAILED",
                                                      "message": out.strip()[-300:], "retryable": True})
                return ActionResult(True, {})
        except RuntimeError as e:
            return ActionResult(False, error={"code": "E_RESET_FAILED", "message": str(e),
                                              "retryable": True})
        # 没有探针序列号就退回串口控制线复位
        if not HAVE_PYOCD or not self._probe_serial(device):
            return self._serial.execute(device, "core.lifecycle", "reset", {})
        cmd = [sys.executable, "-m", "pyocd", "reset", "--probe", self._probe_serial(device), "--nowait"]
        return self._run(cmd, timeout_s=60)

    def _vector_sanity(self, device: dict) -> ActionResult | None:
        """烧录后回读向量表做结构检查（返回 None = 通过）。

        【为什么需要】 -V 只校验字节写对，不校验镜像布局。本次开发
        实际踩过的坑：armlink --first 没生效时向量表不在 0x08000000，
        烧写校验照样 OK，但板子起不来 —— 这类错误在烧录瞬间就该
        被拦下，而不是等人盯着不闪的 LED 猜。

        检查规则（对 STM32 通用）：
          SP（向量[0]）：0x20000000 <= SP < 0x20100000 且 4 字节对齐
          PC（向量[1]）：Thumb 位 = 1（奇数），且落在
                         [flash 基址, flash 基址 + flash_kb*1024) 内
        flash_kb 从 identity_check 声明取（连芯片读也行，但多一次
        SWD 连接；声明值在身份核验时已经和真芯片比对过了）。
        """
        import struct as _struct

        try:
            head = self._cli_tool.read_mem8(0x08000000, 8)
        except RuntimeError:
            # 回读失败不阻断烧录结果（-V 已经过了）—— 健全性检查是
            # 加分项，工具链读不动时降级为跳过
            return None
        sp, pc = _struct.unpack("<II", head)
        flash_base = int(self._binding(device).get("offset", "0x08000000"), 16)
        flash_kb = int(str((device.get("identity_check") or {}).get("flash_kb", 0)) or 0)
        problems = []
        if not (0x20000000 <= sp < 0x20100000 and sp % 4 == 0):
            problems.append(f"SP {hex(sp)} not in aligned SRAM range")
        if not (pc & 1):
            problems.append(f"reset vector {hex(pc)} missing Thumb bit")
        elif flash_kb and not (flash_base <= pc < flash_base + flash_kb * 1024):
            problems.append(f"reset vector {hex(pc)} outside flash window")
        if problems:
            return ActionResult(False, error={
                "code": "E_BAD_IMAGE_LAYOUT",
                "message": "flash OK but vector table is not bootable: " + "; ".join(problems),
                "hint": "image was linked without the vector table at the flash base "
                        "(check --first/--ro-base in the build script)",
                "retryable": False,
            })
        return None

    def _run(self, cmd: list[str], timeout_s: int) -> ActionResult:
        """跑 pyocd CLI，退出码非 0 转结构化错误。"""
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return ActionResult(False, error={
                "code": "E_FLASH_TIMEOUT", "message": f"pyocd timed out after {timeout_s}s",
                "hint": "check probe connection; a hung target may need power cycle",
                "retryable": True,
            })
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            return ActionResult(False, error={
                "code": "E_FLASH_FAILED",
                "message": f"pyocd failed (exit {proc.returncode}): {tail}",
                "hint": "common causes: probe firmware too old (V2J16 clones: use tool: stlink_cli), "
                        "probe occupied by IDE/Cube Programmer, wrong image path",
                "retryable": False,
            })
        return ActionResult(True, {"output": (proc.stdout or "").strip()[-500:]})
