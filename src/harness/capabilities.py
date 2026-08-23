"""内置 core.* 能力注册表（Capability Registry）。

【这是什么】
每个条目是一份"版本化契约"：它规定某个能力的动作集合、参数结构、
风险等级和状态前置条件。设备文件引用这些契约，Adapter 声明自己能
实现哪些契约 —— 三方（设备 / Adapter / Harness 核心）靠它对齐语义。

【为什么这么做】
如果能力只是字符串（"flash"、"uart"），每个 Adapter 各自解释参数
和错误语义，三个月后必然语义漂移。所以契约必须集中收口：
  - core.*     ：内置，全球语义一致（LLM 看到的能力含义固定）
  - vendor.*   ：第三方自定义，必须随包提供 spec 文件，
                 通过 schemas/capability.schema.json 校验后才能加载
"""

from __future__ import annotations

CAPABILITY_SPECS: list[dict] = [
    # ---- 设备生命周期：发现 / 连接 / 复位 / 断电重启 ----
    # reset = 让 MCU 回到已知启动状态（safe，可反复执行）
    # power_cycle = 物理断电再上电（destructive：会丢运行状态，需要确认）
    {
        "schema_version": 1,
        "name": "core.lifecycle",
        "version": "1.0.0",
        "description": (
            "Device lifecycle control: discovery, connection, reset and power. "
            "reset puts the MCU into a known boot state; power_cycle removes "
            "and restores physical power (destructive for running sessions)."
        ),
        "actions": [
            # risk 分级：read-only 不改设备状态；safe 可逆；destructive 需 confirmation token
            # requires = 状态前置条件，执行前 Harness 检查设备会话状态
            {"name": "discover", "risk": "read-only", "requires": []},
            {"name": "connect", "risk": "safe", "requires": []},
            {"name": "disconnect", "risk": "safe", "requires": ["connected"]},
            {"name": "reset", "risk": "safe", "requires": ["connected"]},
            {"name": "power_cycle", "risk": "destructive", "requires": ["connected"],
             "params": {"type": "object", "properties": {"off": {"type": "string"}}}},
        ],
    },
    # ---- 固件管理：编译 / 烧录 / 校验 / 回滚 / 应用脚本部署 ----
    # flash 是最危险的操作：烧错板子可能变砖，所以契约里标 destructive，
    # 且设备文件里的 identity_check（如 chip_id）会在烧录前强制比对。
    # install（v1.1.0 新增）：部署应用脚本（如 MicroPython main.py）——
    # 只写文件系统不动固件本体，风险远低于 flash，定为 safe。
    {
        "schema_version": 1,
        "name": "core.firmware",
        "version": "1.1.0",
        "description": (
            "Firmware management. flash writes an image after identity_check "
            "verification; verify checks the flashed image; rollback restores "
            "the previous slot if the target supports dual banks; install "
            "deploys an application script (e.g. a MicroPython main.py) over "
            "serial without touching the firmware image."
        ),
        "actions": [
            {"name": "build", "risk": "safe", "requires": []},
            {"name": "flash", "risk": "destructive", "requires": ["connected"],
             "params": {"type": "object", "required": ["image"],
                        "properties": {"image": {"type": "string"}, "verify": {"type": "boolean"}}}},
            {"name": "verify", "risk": "read-only", "requires": ["connected"]},
            {"name": "rollback", "risk": "destructive", "requires": ["connected"]},
            {"name": "install", "risk": "safe", "requires": ["connected"],
             "params": {"type": "object", "required": ["script"],
                        "properties": {"script": {"type": "string"},
                                       "target": {"type": "string", "default": "main.py"}}}},
        ],
    },
    # ---- 字节/行通道（UART/SPI/I2C 统一抽象成 link）----
    # 故意不按物理总线拆分能力：对 Agent 来说"往通道发一条命令、
    # 等一个回复"语义相同，底层是 UART 还是 I2C 由 binding 决定
    {
        "schema_version": 1,
        "name": "core.link",
        "version": "1.0.0",
        "description": (
            "Point-to-point byte/line channel (UART/SPI/I2C abstracted away). "
            "send writes data and optionally awaits a regex-matched reply; "
            "monitor subscribes to the stream for a bounded duration."
        ),
        "actions": [
            {"name": "send", "risk": "safe", "requires": ["connected"],
             "params": {"type": "object", "required": ["channel", "data"],
                        "properties": {"channel": {"type": "string"}, "data": {"type": "string"},
                                       "expect": {"type": "string"}, "expect_within": {"type": "string"}}}},
            {"name": "receive", "risk": "read-only", "requires": ["connected"]},
            {"name": "subscribe", "risk": "read-only", "requires": ["connected"]},
            {"name": "monitor", "risk": "read-only", "requires": ["connected"],
             "params": {"type": "object", "required": ["duration"],
                        "properties": {"duration": {"type": "string"}}}},
        ],
    },
    # ---- 结构化遥测：带单位、带 scale 的测量流 ----
    # 单位是一等公民（rpm / degC），LLM 永远不碰裸数字
    {
        "schema_version": 1,
        "name": "core.telemetry",
        "version": "1.0.0",
        "description": (
            "Structured measurement stream with units. Fields and decoding are "
            "declared in the device definition; the agent never handles bare numbers."
        ),
        "actions": [
            {"name": "read", "risk": "read-only", "requires": ["connected"],
             "params": {"type": "object", "properties": {"keys": {"type": "array"}, "window": {"type": "string"}}}},
            {"name": "subscribe", "risk": "read-only", "requires": ["connected"]},
            {"name": "capture_log", "risk": "read-only", "requires": ["connected"]},
        ],
    },
    # ---- 测试执行：解释 7 个步骤原语 ----
    # 风险等级"继承"：测试剧本里含 flash/power_cycle，整剧本即 destructive
    {
        "schema_version": 1,
        "name": "core.test",
        "version": "1.0.0",
        "description": (
            "Test execution. The runner interprets the 7 step primitives and "
            "inherits the highest risk level of the contained steps."
        ),
        "actions": [
            {"name": "execute", "risk": "safe", "requires": ["connected"]},
            {"name": "assert", "risk": "read-only", "requires": ["connected"]},
            {"name": "report", "risk": "read-only", "requires": []},
        ],
    },
]


def builtin_capability_index() -> dict[str, dict]:
    """name -> 最新版 spec。

    内置注册表可能出现同名多版本（将来升级 core.link 到 1.1.0 时
    旧 spec 仍保留做协商），这里取语义化版本号最大的那条。
    """
    index: dict[str, dict] = {}
    for spec in CAPABILITY_SPECS:
        name = spec["name"]
        if name not in index or _semver(spec["version"]) > _semver(index[name]["version"]):
            index[name] = spec
    return index


def _semver(v: str) -> tuple[int, int, int]:
    """把 "1.2.3" 拆成可比较的元组 (1, 2, 3)。"""
    major, minor, patch = v.split(".")
    return int(major), int(minor), int(patch)


def spec_satisfies(spec: dict, requirement: str) -> bool:
    """检查 spec 是否满足设备声明的要求范围。

    采用 caret 语义（npm 风格）：^1.2.0 表示
    "主版本必须是 1，且 >= 1.2.0"——次版本升级向后兼容可用，
    主版本升级意味着契约破坏性变更，必须显式改要求。
    不带 ^ 则要求精确匹配。
    """
    req = requirement.lstrip("^")
    want = _semver(req)
    have = _semver(spec["version"])
    if requirement.startswith("^"):
        return have[0] == want[0] and have >= want
    return have == want
