"""SerialAdapter：纯串口适配器 —— Phase 1 的真实硬件链路（命令 + 遥测）。

【这是什么】
把 core.link（发命令/等回复）和 core.telemetry（读测量值）翻译成
真实的 pyserial 读写。设备通过 USB 串口（或 USB-CDC）连接。

【实现要点】
1. 遥测是流不是查询 —— 用后台读线程持续收行，进 ring log +
   queue 两份缓冲：log 给失败现场回看，queue 给 send 的 expect 匹配。
2. 端口解析支持三种方式（按优先级）：binding.port 显式指定 >
   match.serial_number > match.usb_vid/usb_pid 组合过滤。
3. 测试不需要真串口 —— transport_factory 可注入假传输层
   （见 tests/test_serial_adapter.py 的 FakeMotorTransport）。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque

from .adapters import ActionResult, Adapter, _apply_scale
from .session import DeviceLock

# pyserial 是常规依赖；但 import 失败时不要炸掉整个包
# （比如只跑 mock 测试的环境），真正开串口时才报错
try:
    import serial
    from serial.tools import list_ports

    HAVE_PYSERIAL = True
except ImportError:  # pragma: no cover - 环境缺依赖的兜底
    HAVE_PYSERIAL = False


def parse_duration(s: str) -> float:
    """把 "2s" / "200ms" 解析成秒。

    与 runner.parse_duration 同一规则，放这里是为了 serial_adapter
    能自洽地处理 binding 里的时长字段，不产生对 runner 的反向依赖。
    """
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|min)", s)
    if not m:
        raise ValueError(f"bad duration: {s!r}")
    return float(m.group(1)) * {"ms": 0.001, "s": 1.0, "min": 60.0}[m.group(2)]


class SerialTransport:
    """pyserial 端口封装：后台线程按行读 + 同步写。

    【为什么需要后台读线程】
    MCU 每几十毫秒就吐一行遥测。如果只在有人查询时才 read，
    两次查询之间的数据会堆在 OS 缓冲区里，缓冲一满就丢数据。
    所以连接建立后立刻起一个 daemon 线程死循环读，收到字节就
    按换行切分，每行塞进两个地方：
      - self.log   : deque(500) 环形日志，永远保留最近 500 行原始输出
      - self._queue: 阻塞队列，给 send/expect 这类"要等回复"的逻辑消费
    """

    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.log: deque[str] = deque(maxlen=500)
        self._queue: queue.Queue[str] = queue.Queue()
        self._ser = None            # 真实的 serial.Serial 实例
        self._alive = False
        self._reader: threading.Thread | None = None

    # ---------------------------------------------------------- 生命周期

    def open(self) -> None:
        if not HAVE_PYSERIAL:
            raise RuntimeError("pyserial not installed: pip install pyserial")
        # timeout=0.1：读线程每 100ms 醒一次检查 _alive，保证 close 能退出
        self._ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
        # 【ESP32 自动复位电路兼容】pyserial 开口默认把 DTR/RTS 都拉成
        # 激活态，而 ESP32 的 EN/IO0 由这两根线控制 —— 保持激活会让板子
        # 一直按住复位或进下载模式，表现为"打开成功但没有任何输出"。
        # 所以开口后立刻双双释放（esptool / mpremote 也是这么做的）。
        self._ser.dtr = False
        self._ser.rts = False
        time.sleep(0.05)
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._alive = False
        if self._reader is not None:
            self._reader.join(timeout=1.0)
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    # ------------------------------------------------------------- 读写

    def write(self, data: str) -> None:
        self._ser.write(data.encode("utf-8"))

    def read_line(self, timeout: float) -> str | None:
        """阻塞取一行（等 expect 回复用）；超时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> list[str]:
        """非阻塞取走当前所有积压行（遥测轮询用）。"""
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    def pulse_reset(self, line: str = "rts", hold: float = 0.05) -> None:
        """通过串口控制线复位 MCU。

        常见接法：RTS 或 DTR 接 NRST（低有效）——拉高保持一会儿再释放，
        芯片就复位重启。不同板子接法不同，所以由设备 binding 的
        reset_line 字段指定用哪根线（"rts" / "dtr"）。
        """
        if self._ser is None:
            return
        if line == "dtr":
            self._ser.dtr = True
            time.sleep(hold)
            self._ser.dtr = False
        else:
            self._ser.rts = True
            time.sleep(hold)
            self._ser.rts = False

    # ------------------------------------------------------------- 内部

    def _read_loop(self) -> None:
        """后台读循环：字节流 -> 按行切分 -> log + queue。"""
        buf = b""
        while self._alive:
            chunk = self._ser.read(256)
            if not chunk:
                continue
            buf += chunk
            # 一次 read 可能包含多行，循环切出来
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                self.log.append(line)
                self._queue.put(line)


class SerialAdapter(Adapter):
    """实现 core.lifecycle(部分) / core.link / core.telemetry 三个契约。

    不实现 core.firmware —— 烧录需要调试探针，由 Stm32Adapter 负责。
    """

    name = "serial"

    def __init__(self, transport_factory=None, locks_dir=None):
        # transport_factory: (device) -> 传输层对象。生产环境默认开真串口；
        # 测试注入假对象（同接口：open/close/write/read_line/drain/log）
        # locks_dir：锁目录注入点（默认 ~/.hardware-harness/locks，测试隔离用）
        self._factory = transport_factory or self._open_real
        self._tr: dict[str, SerialTransport] = {}   # device_id -> 传输层
        self._latest: dict[str, dict] = {}          # device_id -> {字段: 原始值}
        # 设备独占锁（B3）：connect 抢 / disconnect 放；跨进程生效。
        # owner 标记谁在用，撞锁的错误信息里会带（Agent 能理解"谁占着"）
        self._locks: dict[str, DeviceLock] = {}
        self._locks_dir = locks_dir
        self.lock_owner = "cli"

    # ------------------------------------------------------------ 契约

    def supports(self, capability: str, version_req: str) -> bool:
        return capability in {"core.lifecycle", "core.link", "core.telemetry"}

    # ------------------------------------------------------------ 动作

    def execute(self, device: dict, capability: str, action: str, params: dict) -> ActionResult:
        if capability == "core.lifecycle":
            return self._lifecycle(device, action, params)
        if capability == "core.link":
            return self._send(device, params)
        return ActionResult(False, error={
            "code": "E_UNSUPPORTED_ACTION",
            "message": f"serial adapter cannot execute {capability}/{action}",
            "hint": "flashing requires the stm32 adapter (ST-Link + pyocd)",
            "retryable": False,
        })

    def _lifecycle(self, device: dict, action: str, params: dict) -> ActionResult:
        if action == "connect":
            # 幂等：已连接就直接成功
            if device["id"] in self._tr:
                return ActionResult(True, {})
            # ---- 先抢设备锁（跨进程独占，B3）----
            # 抢不到 = 别的进程在用这台设备，端口都不用碰
            lock = DeviceLock(device["id"], locks_dir=self._locks_dir)
            err = lock.acquire(owner=self.lock_owner)
            if err:
                return ActionResult(False, error={**err, "retryable": True})
            try:
                tr = self._factory(device)
                tr.open()
            except Exception as e:  # 端口不存在/被占用等，统一转成结构化错误
                lock.release()  # 开口失败必须放锁，否则留死锁
                # 【经验 L17】"解析不到串口"的第一大原因是设备根本没插
                # （换板子时拔了）——hint 必须先提这个，再提被占用
                return ActionResult(False, error={
                    "code": "E_PORT_OPEN_FAILED",
                    "message": f"cannot open serial port: {e}",
                    "hint": f"is the device plugged in? (run 'harness discover' to list "
                            f"ports); also close other programs holding the port "
                            f"(serial monitor, IDE)",
                    "retryable": True,
                })
            self._tr[device["id"]] = tr
            self._latest[device["id"]] = {}
            self._locks[device["id"]] = lock
            return ActionResult(True, {})
        if action == "disconnect":
            tr = self._tr.pop(device["id"], None)
            if tr is not None:
                tr.close()
            # 放锁（没连过也幂等）
            lock = self._locks.pop(device["id"], None)
            if lock is not None:
                lock.release()
            return ActionResult(True, {})
        if action == "reset":
            tr = self._tr.get(device["id"])
            if tr is None:
                return ActionResult(False, error={
                    "code": "E_NOT_CONNECTED", "message": "device not connected",
                    "hint": "connect first", "retryable": True})
            # 复位后清空最新样本 —— 旧数据不代表复位后的状态
            tr.pulse_reset(line=self._binding(device, "core.link").get("reset_line", "rts"))
            self._latest[device["id"]] = {}
            # 【经验固化：复位后必须等固件启动】真板教训（ESP32-S3 实测）：
            # ROM 引导 + 固件初始化约需 300~500ms（MicroPython 要 1s），
            # 复位完立刻 send 命令会丢。等待时长因板而异，所以做成设备
            # 声明：binding.boot_time（如 "1s"），不声明则不等待。
            boot_time = self._binding(device, "core.link").get("boot_time")
            if boot_time:
                time.sleep(parse_duration(boot_time))
            return ActionResult(True, {})
        if action == "discover":
            # 列出所有串口，供填 device yaml 的 match 段参考
            if not HAVE_PYSERIAL:
                return ActionResult(False, error={"code": "E_PYSERIAL_MISSING",
                                                  "message": "pyserial not installed", "retryable": False})
            return ActionResult(True, {"ports": [
                {"port": p.device, "vid": f"{p.vid:04X}" if p.vid else None,
                 "pid": f"{p.pid:04X}" if p.pid else None,
                 "serial_number": p.serial_number, "description": p.description}
                for p in list_ports.comports()]})
        return ActionResult(False, error={"code": "E_UNSUPPORTED_ACTION",
                                          "message": f"serial adapter cannot do lifecycle/{action}",
                                          "retryable": False})

    def _send(self, device: dict, params: dict) -> ActionResult:
        tr = self._tr.get(device["id"])
        if tr is None:
            return ActionResult(False, error={"code": "E_NOT_CONNECTED",
                                              "message": "device not connected",
                                              "hint": "connect first", "retryable": True})
        data = params.get("data", "")
        # 统一补换行：设备固件按行解析命令（binding.newline 可关掉）
        if self._binding(device, "core.link").get("newline", True) and not data.endswith("\n"):
            data += "\n"
        tr.log.append(f"> {data.strip()}")
        tr.write(data)

        expect = params.get("expect")
        if not expect:
            return ActionResult(True, {"matched": None, "reply": ""})

        # 等 expect 正则命中：在 expect_within 窗口内逐行读
        deadline = time.monotonic() + 2.0  # 上层 runner 已控制总超时，这里兜底 2s
        reply_lines: list[str] = []
        while time.monotonic() < deadline:
            line = tr.read_line(timeout=0.1)
            if line is None:
                continue
            reply_lines.append(line)
            self._ingest(device, [line])  # 等待期间的遥测行也别丢
            if re.search(expect, line):
                return ActionResult(True, {"matched": True, "reply": line})
        return ActionResult(True, {"matched": False,
                                   "reply": "\n".join(reply_lines[-5:]) or "<no output>"})

    # ---------------------------------------------------------- 遥测

    def read_telemetry(self, device: dict, keys: list[str] | None = None) -> dict:
        """取最新样本：先 drain 积压行解析进 _latest，再按 scale/offset 换算。

        _latest 存"原始值"，换算在读取时做 —— 设备文件的 scale 改了
        不用重连，立刻生效。
        """
        tr = self._tr.get(device["id"])
        if tr is not None:
            self._ingest(device, tr.drain())
        raw = self._latest.get(device["id"], {})
        if keys:
            raw = {k: v for k, v in raw.items() if k in keys}
        fields = device.get("telemetry", {}).get("fields", {})
        return _apply_scale(fields, raw)

    def read_log(self, device: dict, max_lines: int = 50) -> list[str]:
        tr = self._tr.get(device["id"])
        if tr is None:
            return []
        return list(tr.log)[-max_lines:]

    # ---------------------------------------------------------- 内部

    def _ingest(self, device: dict, lines: list[str]) -> None:
        """把原始行解析成 {字段: 原始值} 存进 _latest。

        支持设备文件声明的两种格式：
          line_json : 每行一个 JSON 对象（推荐，固件用桩头文件即可输出）
          regex     : 按正则命名分组提取（对付改不了的遗留固件）
        解析失败的行静默跳过 —— 串口上可能有启动 banner 等噪声行。

        【B8 实测坑】regex 分组提取出来的是字符串，而 scale/offset
        换算要做算术 —— 必须先转数值（能转则转，转不了当噪声丢弃），
        否则 read_telemetry 直接 TypeError。line_json 路径无此问题
        （JSON 解析天然分型）。
        """
        telemetry = device.get("telemetry", {})
        fmt = telemetry.get("format", "line_json")
        latest = self._latest.setdefault(device["id"], {})
        for line in lines:
            try:
                if fmt == "line_json":
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        latest.update(obj)
                else:
                    m = re.search(telemetry["pattern"], line)
                    if m:
                        for key, value in m.groupdict().items():
                            try:
                                latest[key] = float(value) if "." in value else int(value)
                            except (TypeError, ValueError):
                                pass   # 非数值字段（罕见）：跳过，别污染数值流
            except (json.JSONDecodeError, KeyError):
                continue  # 噪声行：不是遥测就跳过

    def _binding(self, device: dict, capability: str) -> dict:
        """取设备定义里某个能力的 binding 段（串口参数在这里）。"""
        for cap in device.get("capabilities", []):
            if cap["capability"] == capability:
                return cap.get("binding", {})
        return {}

    def resolve_port(self, device: dict) -> str:
        """按设备定义解析串口号（不打开端口）。

        供两类调用方复用：本类 _open_real，以及 Esp32Adapter（esptool
        也需要 --port 参数）。解析不到时抛 RuntimeError，错误信息里
        带排查建议。

        【经验固化：Windows 蓝牙虚拟串口污染】本机实测 discover 会列出
        "蓝牙链接上的标准串行 (COMx)"，它们没有 VID/PID。所以设备匹配
        永远优先用 match.serial_number / usb_vid+pid（真实 USB 设备才有），
        绝不要按"第一个 COM 口"猜 —— 会撞上蓝牙口。
        """
        binding = self._binding(device, "core.link")
        port = binding.get("port")
        if port:
            return port
        match = device.get("match", {})
        want_sn = match.get("serial_number")
        want_vid = match.get("usb_vid")
        want_pid = match.get("usb_pid")
        if want_sn or (want_vid and want_pid):
            from serial.tools import list_ports as _lp
            for p in _lp.comports():
                if want_sn and p.serial_number == want_sn:
                    return p.device
                if (want_vid and want_pid and p.vid is not None
                        and f"{p.vid:04X}" == want_vid and f"{p.pid:04X}" == want_pid):
                    return p.device
        raise RuntimeError(
            f"cannot resolve serial port for '{device['id']}': "
            f"set match.serial_number / usb_vid+pid, or binding.port explicitly")
    def _open_real(self, device: dict) -> SerialTransport:
        """生产环境默认工厂：解析端口号并打开真实串口。"""
        if not HAVE_PYSERIAL:
            raise RuntimeError("pyserial not installed")
        binding = self._binding(device, "core.link")
        return SerialTransport(self.resolve_port(device), baudrate=int(binding.get("baudrate", 115200)))
