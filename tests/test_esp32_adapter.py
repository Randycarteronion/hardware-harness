"""Esp32Adapter 单测 —— MAC 身份核验逻辑，不碰真板不真烧。

esptool 调用全部打桩（monkeypatch），验证的是三件事：
  1. identity_check.mac 缺失 -> 拒绝盲烧
  2. MAC 不匹配 -> E_IDENTITY_MISMATCH 硬失败
  3. MAC 匹配 -> 正常走到 write_flash 命令
"""

from __future__ import annotations

import pytest

from harness.adapters import ActionResult
from harness.esp32_adapter import Esp32Adapter

DEVICE = {
    "id": "esp_test",
    "match": {"adapter": "esp32", "serial_number": "SN123"},
    "identity_check": {"mac": "AA:BB:CC:DD:EE:FF"},
    "capabilities": [
        {"capability": "core.firmware", "binding": {"chip": "esp32s3"}},
        {"capability": "core.link", "binding": {"port": "COMTEST"}},
    ],
    "telemetry": {"format": "line_json", "fields": {}},
}


@pytest.fixture
def adapter():
    return Esp32Adapter(serial_adapter=_NoopSerial())


class _NoopSerial:
    """串口子适配器桩：本测试不涉及串口路径。"""

    def execute(self, *a, **k):
        return ActionResult(True, {})

    def read_telemetry(self, *a, **k):
        return {}

    def read_log(self, *a, **k):
        return []

    def resolve_port(self, device):
        return "COMTEST"


def test_no_mac_refuses_blind_flash(adapter):
    dev = {**DEVICE, "identity_check": {}}
    result = adapter.execute(dev, "core.firmware", "flash", {"image": "x.bin"})
    assert not result.ok
    assert result.error["code"] == "E_NO_IDENTITY_SPECIFIED"


def test_mac_mismatch_hard_fails(adapter, monkeypatch):
    # 桩：esptool 读回来的 MAC 和 yaml 里写的不一样
    monkeypatch.setattr(adapter, "_read_mac", lambda device: "11:22:33:44:55:66")
    result = adapter.execute(DEVICE, "core.firmware", "flash", {"image": "x.bin"})
    assert not result.ok
    assert result.error["code"] == "E_IDENTITY_MISMATCH"
    assert "11:22:33:44:55:66" in result.error["message"]


def test_mac_match_proceeds_to_write_flash(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_read_mac", lambda device: "AA:BB:CC:DD:EE:FF")
    calls = []

    def fake_esptool(args, timeout_s=300):
        calls.append(args)
        import types
        return types.SimpleNamespace(returncode=0, stdout="Flash OK", stderr="")

    monkeypatch.setattr(adapter, "_esptool", fake_esptool)
    result = adapter.execute(DEVICE, "core.firmware", "flash", {"image": "fw.bin"})
    assert result.ok
    assert len(calls) == 1
    cmd = calls[0]
    # 命令形态核对：芯片/烧完硬复位/偏移/镜像
    assert "--chip" in cmd and "esp32s3" in cmd
    assert "--after" in cmd and "hard_reset" in cmd
    assert "write_flash" in cmd and "fw.bin" in cmd


def test_read_mac_parses_esptool_output(adapter, monkeypatch):
    import types
    fake = types.SimpleNamespace(
        returncode=0,
        stdout="Chip is ESP32-S3\nMAC: 68:ee:8f:4f:03:b4\nDone.",
        stderr="")
    monkeypatch.setattr(adapter, "_esptool", lambda args, timeout_s=60: fake)
    assert adapter._read_mac(DEVICE) == "68:EE:8F:4F:03:B4"
