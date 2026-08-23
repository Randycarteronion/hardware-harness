"""B7 部署动作单测 —— mpremote 全打桩，不碰真板。

验证四件事：
  1. 命令构造正确（connect <port> cp <script> :<target>）
  2. 【经验 L7】首次 cp 撞上"板子被端口打开复位"时自动重试成功
  3. 脚本不存在 -> 结构化错误
  4. 两次都失败 -> E_DEPLOY_FAILED 带排查 hint
"""

from __future__ import annotations

import subprocess
import types

import pytest

from harness.adapters import ActionResult
from harness.esp32_adapter import Esp32Adapter

DEVICE = {
    "id": "esp_deploy_test",
    "match": {"adapter": "esp32", "serial_number": "SN1"},
    "identity_check": {"mac": "AA:BB:CC:DD:EE:FF"},
    "capabilities": [
        {"capability": "core.firmware",
         "binding": {"tool": "esptool", "chip": "esp32s3", "port": "COMTEST"}},
        {"capability": "core.link", "binding": {"port": "COMTEST"}},
    ],
    "telemetry": {"format": "line_json", "fields": {}},
}


class _FakeSerial:
    def execute(self, *a, **k):
        return ActionResult(True, {})

    def read_telemetry(self, *a, **k):
        return {}

    def read_log(self, *a, **k):
        return []

    def resolve_port(self, device):
        return "COMTEST"


@pytest.fixture
def adapter():
    return Esp32Adapter(serial_adapter=_FakeSerial())


def _proc(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def test_deploy_success_first_try(adapter, monkeypatch, tmp_path):
    script = tmp_path / "main.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(adapter, "_run_mpremote",
                        lambda args, timeout_s=90: (calls.append(args), _proc(0, "copied"))[1])
    r = adapter.execute(DEVICE, "core.firmware", "install", {"script": str(script)})
    assert r.ok
    assert r.data["target"] == "main.py" and r.data["port"] == "COMTEST"
    cp = calls[0]
    assert cp[:3] == ["connect", "COMTEST", "cp"] and cp[3] == str(script) and cp[4] == ":main.py"
    assert calls[1][:3] == ["connect", "COMTEST", "reset"]   # 部署后自动复位


def test_deploy_retries_after_port_open_reset(adapter, monkeypatch, tmp_path):
    """L7：首次 cp 撞上 raw-repl 失败 -> 等 1.5s 重试成功。"""
    script = tmp_path / "main.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    attempts = []

    def fake(args, timeout_s=90):
        attempts.append(list(args))
        if len(attempts) == 1 and "cp" in args:
            return _proc(1, "Traceback ... could not enter raw repl")
        return _proc(0, "copied")

    sleeps = []
    monkeypatch.setattr("harness.esp32_adapter.time.sleep", sleeps.append)
    monkeypatch.setattr(adapter, "_run_mpremote", fake)
    r = adapter.execute(DEVICE, "core.firmware", "install", {"script": str(script)})
    assert r.ok
    assert len([a for a in attempts if "cp" in a]) == 2   # cp 重试了一次
    assert sleeps and sleeps[0] >= 1.0                      # 重试前等了


def test_deploy_missing_script(adapter):
    r = adapter.execute(DEVICE, "core.firmware", "install", {"script": "nope.py"})
    assert not r.ok and r.error["code"] == "E_SCRIPT_NOT_FOUND"


def test_deploy_persistent_failure(adapter, monkeypatch, tmp_path):
    script = tmp_path / "main.py"
    script.write_text("x", encoding="utf-8")
    monkeypatch.setattr(adapter, "_run_mpremote",
                        lambda args, timeout_s=90: _proc(1, "error: port busy"))
    monkeypatch.setattr("harness.esp32_adapter.time.sleep", lambda s: None)
    r = adapter.execute(DEVICE, "core.firmware", "install", {"script": str(script)})
    assert not r.ok and r.error["code"] == "E_DEPLOY_FAILED"
    assert "monitor/IDE" in r.error["hint"]
