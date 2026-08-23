"""Device Registry：设备定义的加载、校验、能力协商。

【核心原则】加载时失败，而不是烧录时失败。
设备文件不合法、能力没有 spec、adapter 不支持某个能力 —— 全部在
load 阶段拒绝。绝不让一个坏定义活到 Agent 调 flash 的那一刻。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

from .adapters import Adapter
from .capabilities import builtin_capability_index, spec_satisfies

# schemas/ 目录在仓库根下（src/harness 的上两级），随源码分发
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _semver_tuple(v: str) -> tuple[int, int, int]:
    """"1.2.3" -> (1, 2, 3)，spec 新旧比较用。"""
    return tuple(int(x) for x in v.split("."))


class RegistryError(Exception):
    """加载/协商失败。message 直接给人或 LLM 看，不包多层。"""


class DeviceRegistry:
    def __init__(self, adapters: dict[str, Adapter]) -> None:
        self.adapters = adapters                        # adapter名 -> 实例（Phase 0 只有 mock）
        self.capabilities = builtin_capability_index()  # 能力名 -> 最新 spec
        self.devices: dict[str, dict] = {}              # 设备id -> 定义文档
        self.sources: dict[str, Path] = {}              # 设备id -> 来源文件（报错用）

    def load_device(self, path: Path) -> dict:
        """加载单个设备 YAML：解析 -> schema 校验 -> 查重 -> 能力协商 -> 入册。"""
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))

        # 第一步：结构校验。字段名打错、缺 unit、telemetry 格式非法都在这里拦下
        schema = json.loads((SCHEMA_DIR / "device.schema.json").read_text(encoding="utf-8"))
        try:
            jsonschema.validate(doc, schema)
        except jsonschema.ValidationError as e:
            raise RegistryError(f"{path.name}: invalid device definition: {e.message}") from e

        # 第二步：id 查重 —— id 是测试和 Agent 寻址的锚点，必须全局唯一
        device_id = doc["id"]
        if device_id in self.devices:
            raise RegistryError(f"duplicate device id '{device_id}' ({self.sources[device_id].name}, {path.name})")

        # 第三步：找到声明要用的 adapter，不存在直接拒绝
        adapter_name = doc["match"]["adapter"]
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            raise RegistryError(f"{path.name}: unknown adapter '{adapter_name}'")

        # 第四步：能力协商（见 _negotiate）
        self._negotiate(doc, adapter, path)

        self.devices[device_id] = doc
        self.sources[device_id] = path
        return doc

    def load_dir(self, directory: Path) -> list[dict]:
        """批量加载目录下所有 *.yaml（按文件名排序，保证行为可复现）。"""
        loaded = []
        for path in sorted(directory.glob("*.yaml")):
            loaded.append(self.load_device(path))
        return loaded

    # ------------------------------------------------ vendor 能力 spec 加载

    def load_capability_spec(self, path: Path) -> dict:
        """加载一份第三方能力契约（backlog B4）。

        vendor.* 能力的语义不再只能靠内置注册表 —— 第三方把 spec
        文件放进 <devices_dir>/capabilities/ 目录，这里校验入库。
        这样 LLM 看到的能力语义依然是"有据可查的契约"，而不是自由
        字符串（架构原则：能力注册表收口，方案 A）。

        规则：
          - 必须通过 capability.schema.json 校验（含 vendor 命名空间正则）
          - 同名多版本取语义化版本最高（与内置注册表同规则）
          - 不允许覆盖 core.*（内置契约不可被第三方篡改）
        """
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads((SCHEMA_DIR / "capability.schema.json").read_text(encoding="utf-8"))
        try:
            jsonschema.validate(doc, schema)
        except jsonschema.ValidationError as e:
            raise RegistryError(f"{path.name}: invalid capability spec: {e.message}") from e

        name = doc["name"]
        if name.startswith("core."):
            raise RegistryError(f"{path.name}: refusing to override builtin '{name}'")
        existing = self.capabilities.get(name)
        if existing and _semver_tuple(existing["version"]) >= _semver_tuple(doc["version"]):
            return existing   # 已有同版或更新：保留，不回退
        self.capabilities[name] = doc
        return doc

    def load_specs_dir(self, directory: Path) -> list[dict]:
        """批量加载目录下全部 *.yaml 能力 spec（按文件名序，可复现）。"""
        loaded = []
        for path in sorted(directory.glob("*.yaml")):
            loaded.append(self.load_capability_spec(path))
        return loaded

    def _negotiate(self, device: dict, adapter: Adapter, path: Path) -> None:
        """三方协商：设备要求的每个能力，必须有 spec，且 adapter 声明实现。

        这是"通用框架但每个都能跑"的保障机制 —— 设备声明
        core.firmware 但 adapter 只会串口？启动时就报错，而不是
        Agent 调 flash 时才发现不支持。
        """
        for cap in device["capabilities"]:
            name = cap["capability"]
            # 1) 契约必须存在：core.* 内置；vendor.* 需在
            #    <devices_dir>/capabilities/ 放 spec 文件（B4 已实现自动加载）
            spec = self.capabilities.get(name)
            if spec is None:
                raise RegistryError(
                    f"{path.name}: capability '{name}' has no spec. "
                    f"core.* is built in; vendor.* needs a spec yaml in the "
                    f"'capabilities/' subdirectory of the devices dir."
                )
            # 2) 版本范围检查：^1.0.0 = 主版本 1 且 >= 1.0.0
            req = cap.get("version", "^1.0.0")
            if not spec_satisfies(spec, req):
                raise RegistryError(f"{path.name}: spec {name}@{spec['version']} does not satisfy {req}")
            # 3) adapter 必须声明实现该能力
            if not adapter.supports(name, req):
                raise RegistryError(
                    f"{path.name}: adapter '{adapter.name}' does not implement {name}@{req}; device rejected"
                )

    def get(self, device_id: str) -> dict:
        """按 id 取设备定义；未知 id 时把已知列表告诉调用方（错误信息要能自我修复）。"""
        if device_id not in self.devices:
            known = ", ".join(sorted(self.devices)) or "<none loaded>"
            raise RegistryError(f"unknown device '{device_id}' (known: {known})")
        return self.devices[device_id]

    def adapter_for(self, device: dict) -> Adapter:
        """设备 -> 对应 adapter 实例。上层永远通过这里拿 adapter，不自己查表。"""
        return self.adapters[device["match"]["adapter"]]
