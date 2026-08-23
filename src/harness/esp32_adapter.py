"""Esp32Adapter：组合适配器 = esptool 烧录 + 串口 命令/遥测。

【架构意义】
这是"新增设备家族只需新增 adapter"承诺的第一次兑现：core /
schema / runner / CLI 一行不改，ESP32 就接进来了。烧录走 esptool
命令行（ESP 家族的事实标准工具），命令与遥测完全复用 SerialAdapter。

【与 Stm32Adapter 的对应关系】
  Stm32Adapter: pyocd + ST-Link 探针（SWD）    —— 烧录/复位/身份
  Esp32Adapter : esptool + 串口引导程序（ROM）  —— 烧录/复位/身份
ESP32 不需要调试探针：芯片内置 ROM 引导程序，靠 DTR/RTS 自动
进下载模式，所以全部操作都走那一根 USB 串口线。

【防烧错板（两道闸 + CLI token）】
  1. match.serial_number 精确选中 CH343 串口（就是这块板的线）
  2. 烧录前 read_mac 核验 MAC 地址 —— 和设备 yaml 的 identity_check
     .mac 逐字符比对，不匹配硬失败

【真板趟坑经验（ESP32-S3 N16R8 + CH343，2026-08-23 实测）】
  - CH343 烧录波特率 921600 稳定可用（1.75MB 固件 16.6s 烧完）
  - 换固件家族（如摄像头固件 → MicroPython）必须先 erase_flash：
    旧固件的数据区残留会让新固件的文件系统误判损坏
  - PSRAM 变体要选对：N16R8 是八线（octal），标准 GENERIC_S3 按
    四线（quad）初始化会报 "PSRAM chip is not connected" —— 该错误
    非致命（ESP-IDF 放弃 PSRAM 继续启动），不用 PSRAM 的应用可忽略
  - MicroPython 的 sys.stdout 没有 flush()，打印用 print() 即可
  - 国内网络下 Arduino 工具链（394MB，GitHub 源）约 60KB/s：
    原型验证优先用 MicroPython 官方预编译固件（micropython.org
    直连很快），C 工具链留给正式项目夜里慢慢下
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from .adapters import ActionResult, Adapter
from .serial_adapter import SerialAdapter


class Esp32Adapter(Adapter):
    """实现全部五个 core.* 契约：firmware 走 esptool，其余委托串口。"""

    name = "esp32"

    def __init__(self, serial_adapter: SerialAdapter | None = None):
        # 串口子适配器可注入（测试用）；默认自建
        self._serial = serial_adapter or SerialAdapter()

    # ------------------------------------------------------------ 契约

    def supports(self, capability: str, version_req: str) -> bool:
        return capability in {"core.lifecycle", "core.firmware", "core.link", "core.telemetry", "core.test"}

    # ------------------------------------------------------------ 分发

    def execute(self, device: dict, capability: str, action: str, params: dict) -> ActionResult:
        if capability == "core.firmware":
            if action == "flash":
                return self._flash(device, params)
            if action == "build":
                # 构建是纯宿主机操作，与 stm32 路径共用同一个 builder
                from .builder import build_flow
                result = build_flow(device)
                if not result["ok"]:
                    return ActionResult(False, error={
                        "code": "E_BUILD_FAILED",
                        "message": result["message"],
                        "hint": result.get("hint", ""),
                        "retryable": True,
                    })
                return ActionResult(True, {k: v for k, v in result.items() if k != "ok"})
            if action == "install":
                return self._install(device, params)
            if action == "verify":
                return self._verify(device)
            return ActionResult(False, error={"code": "E_UNSUPPORTED_ACTION",
                                              "message": f"esp32 adapter firmware/{action} not implemented yet",
                                              "retryable": False})
        # ESP32 复位 = 串口 RTS 脉冲（esptool 的 hard reset 也是这么做的），
        # 直接委托串口子适配器；connect/send/telemetry 同理
        return self._serial.execute(device, capability, action, params)

    # ---------------------------------------------------------- 遥测委托

    def read_telemetry(self, device: dict, keys: list[str] | None = None) -> dict:
        return self._serial.read_telemetry(device, keys)

    def read_log(self, device: dict, max_lines: int = 50) -> list[str]:
        return self._serial.read_log(device, max_lines)

    # ----------------------------------------------------------- esptool

    def _binding(self, device: dict) -> dict:
        """firmware 能力的 binding（chip 型号 / 烧录波特率 / 偏移）。"""
        for cap in device.get("capabilities", []):
            if cap["capability"] == "core.firmware":
                return cap.get("binding", {})
        return {}

    def _port(self, device: dict) -> str:
        """解析串口号：binding.port 显式 > match.serial_number 匹配。"""
        port = self._binding(device).get("port")
        if port:
            return port
        return self._serial.resolve_port(device)  # 可能抛 KeyError，见 resolve_port 注释

    def _esptool(self, args: list[str], timeout_s: int = 300) -> subprocess.CompletedProcess:
        """跑一条 esptool 命令。用系统安装的 esptool（pip 装的即有 CLI）。

        注意：烧大固件在 921600 波特率下可能要一两分钟，timeout 给足。
        """
        # encoding/errors：工具可能输出 GBK 中文（中文 Windows），必须
        # errors='replace' 否则 Python 3.14 UTF-8 模式下读线程会崩（L14）
        return subprocess.run([sys.executable, "-m", "esptool", *args],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s)

    def _read_mac(self, device: dict) -> str | None:
        """读芯片 MAC —— 身份核验的依据。

        输出里抓 "MAC: 68:ee:8f:..." 行。失败返回 None（调用方决定怎么报错）。
        注意 read_mac 本身会复位芯片（要先同步波特率再重启），属于可接受
        的副作用 —— 烧录前反正要复位。
        """
        try:
            proc = self._esptool(["--port", self._port(device),
                                  "--chip", self._binding(device).get("chip", "esp32s3"),
                                  "read_mac"], timeout_s=60)
        except (subprocess.TimeoutExpired, KeyError, RuntimeError) as e:
            return None
        m = re.search(r"MAC:\s*([0-9A-Fa-f:]{17})", proc.stdout or "")
        return m.group(1).upper() if m else None

    def _flash(self, device: dict, params: dict) -> ActionResult:
        # ---- 闸 2：MAC 身份核验（闸 1 = 精确串口匹配，闸 3 = CLI token）----
        want = (device.get("identity_check") or {}).get("mac", "").upper()
        if not want:
            return ActionResult(False, error={
                "code": "E_NO_IDENTITY_SPECIFIED",
                "message": "device defines no identity_check.mac; refusing to flash blind",
                "hint": "run 'esptool --port <PORT> read_mac' and put the MAC into the "
                        "device definition's identity_check section",
                "retryable": False,
            })
        got = self._read_mac(device)
        if got is None:
            return ActionResult(False, error={
                "code": "E_IDENTITY_UNREADABLE",
                "message": "could not read MAC from device",
                "hint": "check the serial port (harness discover), cable, and that no other "
                        "program holds the port; the chip must auto-enter bootloader via DTR/RTS",
                "retryable": True,
            })
        if got != want:
            return ActionResult(False, error={
                "code": "E_IDENTITY_MISMATCH",
                "message": f"MAC mismatch: device yaml expects {want}, board answered {got}",
                "hint": "wrong board connected, or fix identity_check.mac in the device definition",
                "retryable": False,
            })

        # ---- 身份对上了，烧录 ----
        binding = self._binding(device)
        image = params.get("image", "")
        # --before/--after：进下载模式用默认 DTR/RTS 序列，烧完硬复位跑新固件
        cmd = ["--port", self._port(device),
               "--chip", binding.get("chip", "esp32s3"),
               "--baud", str(binding.get("flash_baud", 921600)),
               "--before", "default_reset", "--after", "hard_reset",
               "write_flash", binding.get("offset", "0x0"), image]
        try:
            proc = self._esptool(cmd, timeout_s=600)
        except subprocess.TimeoutExpired:
            return ActionResult(False, error={
                "code": "E_FLASH_TIMEOUT",
                "message": "esptool timed out",
                "hint": "check cable; try a lower flash_baud (some CH343 boards dislike 921600)",
                "retryable": True,
            })
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            return ActionResult(False, error={
                "code": "E_FLASH_FAILED",
                "message": f"esptool failed (exit {proc.returncode}): {tail}",
                "hint": "common causes: wrong image/offset, port held by IDE serial monitor, "
                        "board needs BOOT held for manual download mode. If switching firmware "
                        "families (e.g. Arduino app -> MicroPython), run "
                        "'esptool --port <PORT> erase_flash' first to clear stale data regions",
                "retryable": False,
            })
        return ActionResult(True, {"output": (proc.stdout or "").strip()[-500:], "mac": want})

    def _run_mpremote(self, args: list[str], timeout_s: int = 90) -> subprocess.CompletedProcess:
        """跑一条 mpremote 命令（MicroPython 官方工具，纯 Python）。

        encoding/errors：GBK 兜底同 L14。
        """
        return subprocess.run([sys.executable, "-m", "mpremote", *args],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout_s)

    def _install(self, device: dict, params: dict) -> ActionResult:
        """部署应用脚本到 MicroPython 设备（backlog B7，firmware/install）。

        流程：mpremote cp 脚本到 :<target>（默认 main.py，开机自启），
        然后 reset 让它立即跑起来。

        【经验 L7 固化】mpremote 打开串口的瞬间 DTR/RTS 电平变化会
        触发一次板子复位，第一次 cp 可能撞上"板子还在启动"而报
        "could not enter raw repl"——不是故障，等 1.5s 重试一次即可。
        这个重试是实测得出的必需品，不是防御性编程。
        """
        script = params.get("script", "")
        if not script or not Path(script).exists():
            return ActionResult(False, error={
                "code": "E_SCRIPT_NOT_FOUND",
                "message": f"script not found: {script}",
                "hint": "pass an existing .py path (e.g. firmware/esp32/micropython/main.py)",
                "retryable": False,
            })
        target = params.get("target", "main.py")
        try:
            port = self._port(device)
        except RuntimeError as e:
            return ActionResult(False, error={
                "code": "E_PORT_OPEN_FAILED", "message": str(e),
                "hint": "is the device plugged in? run 'harness discover'",
                "retryable": True,
            })

        cp_args = ["connect", port, "cp", str(script), f":{target}"]
        last_err = ""
        for attempt in (1, 2):   # L7：首连可能撞上端口打开引发的板子复位
            try:
                proc = self._run_mpremote(cp_args)
            except subprocess.TimeoutExpired:
                return ActionResult(False, error={"code": "E_DEPLOY_TIMEOUT",
                                                  "message": "mpremote cp timed out",
                                                  "hint": "check cable; board may need manual reset",
                                                  "retryable": True})
            if proc.returncode == 0 and "could not enter raw repl" not in (proc.stdout or ""):
                break
            last_err = ((proc.stdout or "") + (proc.stderr or ""))[-300:]
            time.sleep(1.5)       # 等板子完成复位启动
        else:
            return ActionResult(False, error={
                "code": "E_DEPLOY_FAILED",
                "message": f"mpremote cp failed: {last_err}",
                "hint": "common causes: port held by monitor/IDE, board in REPL "
                        "with main.py crashed (press reset), wrong port",
                "retryable": True,
            })

        # 部署完成 → 复位让脚本立即生效（main.py 开机自启）
        try:
            self._run_mpremote(["connect", port, "reset"], timeout_s=30)
        except subprocess.TimeoutExpired:
            pass  # 复位命令偶尔不回包，脚本已经上去了，不算失败
        return ActionResult(True, {"deployed": str(script), "target": target,
                                   "port": port})

    def _verify(self, device: dict) -> ActionResult:
        """读回 flash 与镜像比对（firmware/verify，read-only）。

        用 esptool read_flash 读 offset 起整片，与设备 yaml 声明的
        artifact（binding.build.artifact 或 params 无 → 用 flash 时
        记录的镜像）比对。MVP 简化：读 build artifact 的长度比对。
        """
        from .builder import firmware_binding
        artifact = firmware_binding(device).get("build", {}).get("artifact")
        if not artifact or not Path(artifact).exists():
            return ActionResult(False, error={
                "code": "E_NO_IMAGE_TO_VERIFY",
                "message": "no build artifact declared/found to verify against",
                "hint": "run 'harness build' first, or declare binding.build.artifact",
                "retryable": False,
            })
        image = Path(artifact).read_bytes()
        offset = int(firmware_binding(device).get("offset", "0x0"), 16)
        out = Path("runs") / "verify_read.bin"
        out.parent.mkdir(exist_ok=True)
        try:
            proc = self._esptool(["--port", self._port(device),
                                  "--chip", firmware_binding(device).get("chip", "esp32s3"),
                                  "read_flash", hex(offset), str(len(image)), str(out)],
                                 timeout_s=180)
        except (subprocess.TimeoutExpired, RuntimeError) as e:
            return ActionResult(False, error={"code": "E_VERIFY_FAILED",
                                              "message": f"read_flash failed: {e}",
                                              "retryable": True})
        if proc.returncode != 0:
            return ActionResult(False, error={"code": "E_VERIFY_FAILED",
                                              "message": (proc.stderr or "")[-300:],
                                              "retryable": False})
        readback = out.read_bytes()
        if readback == image:
            return ActionResult(True, {"verified": len(image), "artifact": artifact})
        # 找第一个差异字节（长度不等直接报 -1）
        diff = -1
        if len(readback) == len(image):
            for i, (a, b) in enumerate(zip(image, readback)):
                if a != b:
                    diff = i
                    break
        return ActionResult(False, error={
            "code": "E_VERIFY_MISMATCH",
            "message": f"flash content differs from {artifact} (first diff at byte {diff})",
            "hint": "flash the image again, or rebuild (artifact may be stale)",
            "retryable": False,
        })
