"""StlinkCliTool 单测 —— 解析逻辑，不碰真探针。

ST-LINK_CLI v1.4.0 实测输出特征：
  - 中文 Windows 下输出 GBK 编码（run() 已用 errors='replace' 兜底）
  - 退出码不可靠，成败靠输出文本判断
  - 不支持 -ListStlink / -Dump（读内存用 -r8）
"""

from __future__ import annotations

import pytest

from harness.adapters import ActionResult
from harness.stm32_adapter import StlinkCliTool, Stm32Adapter

# 真板（蓝pill F103C8 + 山寨 ST-Link 老固件）抓下来的真实输出
REAL_CONNECT_OUTPUT = """STM32 ST-LINK CLI v1.4.0
STM32 ST-LINK Command Line Interface

Connected via SWD.
Device ID:0x410
Device flash Size : 64 Kbytes
Device family :STM32F10x Medium-density
"""

# 真板 -r8 回读输出（烧录后物理验证时抓的）
REAL_READBACK_OUTPUT = """0x08000000 : 00  50  00  20  41  00  00  08  DD  00  00  08  DD  00  00  08
0x08000010 : DD  00  00  08  DD  00  00  08  DD  00  00  08  00  00  00  00"""

DEVICE = {
    "id": "bp_test",
    "match": {"adapter": "stm32"},
    "identity_check": {"device_id": "0x410", "flash_kb": "64"},
    "capabilities": [
        {"capability": "core.firmware", "binding": {"tool": "stlink_cli"}},
        {"capability": "core.link", "binding": {"baudrate: 115200"}},
    ],
    "telemetry": {"format": "line_json", "fields": {}},
}


class FakeSerial:
    def execute(self, *a, **k):
        return ActionResult(True, {})

    def read_telemetry(self, *a, **k):
        return {}

    def read_log(self, *a, **k):
        return []


@pytest.fixture
def adapter(monkeypatch):
    ad = Stm32Adapter(serial_adapter=FakeSerial())
    monkeypatch.setattr(ad._cli_tool, "available", lambda: True)
    return ad


def test_identity_parses_real_output(adapter, monkeypatch):
    monkeypatch.setattr(adapter._cli_tool, "run", lambda args, timeout_s=180: REAL_CONNECT_OUTPUT)
    ident = adapter._cli_tool.identity()
    assert ident == {"device_id": "0x410", "flash_kb": 64}


def test_correct_identity_passes(adapter, monkeypatch):
    monkeypatch.setattr(adapter._cli_tool, "run", lambda args, timeout_s=180: REAL_CONNECT_OUTPUT)
    assert adapter._identity_check(DEVICE) is None


def test_wrong_device_id_rejected(adapter, monkeypatch):
    monkeypatch.setattr(adapter._cli_tool, "run", lambda args, timeout_s=180: REAL_CONNECT_OUTPUT)
    dev = {**DEVICE, "identity_check": {"device_id": "0x411", "flash_kb": "64"}}
    result = adapter._identity_check(dev)
    assert result is not None
    assert result.error["code"] == "E_IDENTITY_MISMATCH"
    assert "0x410" in result.error["message"]


def test_wrong_flash_size_rejected(adapter, monkeypatch):
    """C8(64KB) 与 CB(128KB) 同为 0x410，靠容量区分 —— 核验不被绕过。"""
    monkeypatch.setattr(adapter._cli_tool, "run", lambda args, timeout_s=180: REAL_CONNECT_OUTPUT)
    dev = {**DEVICE, "identity_check": {"device_id": "0x410", "flash_kb": "128"}}
    result = adapter._identity_check(dev)
    assert result.error["code"] == "E_IDENTITY_MISMATCH"


def test_read_mem8_parses_real_output(adapter, monkeypatch):
    """-r8 回读解析：真板输出格式 -> bytes（L13 回读验证的基础）。"""
    monkeypatch.setattr(adapter._cli_tool, "run", lambda args, timeout_s=180: REAL_READBACK_OUTPUT)
    data = adapter._cli_tool.read_mem8(0x08000000, 32)
    assert data[:8] == bytes.fromhex("00500020 41000008".replace(" ", ""))
    assert len(data) == 32


def test_vector_sanity_passes_on_real_vectors(adapter, monkeypatch):
    """向量表健全性：真板烧录后的 SP/PC 必须通过。"""
    monkeypatch.setattr(adapter._cli_tool, "read_mem8",
                        lambda addr, n: bytes.fromhex("00500020 41000008".replace(" ", "")))
    assert adapter._vector_sanity(DEVICE) is None


def test_vector_sanity_catches_bad_layout(adapter, monkeypatch):
    """镜像布局错误必须在烧录瞬间被拦下（--first 失效、入口非 Thumb）。

    这正是开发时真踩过的坑：-V 字节校验通过但向量表没在基址上，
    板子不跑 —— 该检查把这类错误变成结构化失败。
    """
    # SP 落在 flash 区 + PC 偶数（缺 Thumb 位）= 双重坏
    monkeypatch.setattr(adapter._cli_tool, "read_mem8",
                        lambda addr, n: bytes.fromhex("00300008 40000008".replace(" ", "")))
    result = adapter._vector_sanity(DEVICE)
    assert result is not None
    assert result.error["code"] == "E_BAD_IMAGE_LAYOUT"
    assert "SP" in result.error["message"] and "Thumb" in result.error["message"]


def test_flash_success_and_failure_by_output(adapter, monkeypatch):
    """v1.4.0 退出码不可靠 —— 成败必须按输出关键词判断。"""
    calls = []

    def fake_run(args, timeout_s=180):
        calls.append(args)
        if "-P" in args:
            return "Flash memory programmed in 0s and 172ms.\nVerification...OK\nMCU Reset."
        return REAL_CONNECT_OUTPUT

    monkeypatch.setattr(adapter._cli_tool, "run", fake_run)
    r = adapter.execute(DEVICE, "core.firmware", "flash", {"image": "fw.bin"})
    assert r.ok

    def failing_run(args, timeout_s=180):
        if "-P" in args:
            return "... Error: cannot write memory ..."
        return REAL_CONNECT_OUTPUT

    monkeypatch.setattr(adapter._cli_tool, "run", failing_run)
    r = adapter.execute(DEVICE, "core.firmware", "flash", {"image": "fw.bin"})
    assert not r.ok and r.error["code"] == "E_FLASH_FAILED"
