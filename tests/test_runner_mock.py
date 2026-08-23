import json
from pathlib import Path

import pytest

from harness.adapters import MockAdapter
from harness.registry import DeviceRegistry, RegistryError
from harness.runner import TestError, TestRunner

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def registry(tmp_path):
    reg = DeviceRegistry(adapters={MockAdapter.name: MockAdapter()})
    reg.load_device(EXAMPLES / "devices" / "stm32_motor.yaml")
    return reg


def test_device_loads_and_negotiates(registry):
    assert "stm32_motor" in registry.devices


def test_unknown_capability_rejected(tmp_path):
    reg = DeviceRegistry(adapters={MockAdapter.name: MockAdapter()})
    bad = tmp_path / "bad.yaml"
    good = (EXAMPLES / "devices" / "stm32_motor.yaml").read_text(encoding="utf-8")
    bad.write_text(good.replace("core.firmware", "vendor.acme.warp"), encoding="utf-8")
    with pytest.raises(RegistryError, match="no spec"):
        reg.load_device(bad)


def test_motor_start_passes(registry, tmp_path):
    runner = TestRunner(registry, tmp_path / "runs")
    script = runner.load_script(EXAMPLES / "tests" / "motor_start.yaml")
    report = runner.run(script)
    assert report["status"] == "PASS"
    assert report["steps"][3]["detail"]["measured"] > 1000
    assert report["artifacts"], "capture step must produce an artifact"
    assert (tmp_path / "runs" / report["run_id"] / "report.json").exists()


def test_assert_failure_reports_measured(registry, tmp_path):
    runner = TestRunner(registry, tmp_path / "runs")
    script = runner.load_script(EXAMPLES / "tests" / "motor_start.yaml")
    # impossible threshold: motor target is 1243 rpm
    for step in script["steps"]:
        if step["action"] == "assert":
            step["params"]["value"] = 5000
            step["params"]["within"] = "1s"
            step["params"]["settle"] = "0s"
    script["retry"] = 0
    report = runner.run(script)
    assert report["status"] == "FAIL"
    failed = [s for s in report["steps"] if s["status"] == "FAIL"][0]
    assert failed["detail"]["measured"] < 5000
    assert failed["detail"]["unit"] == "rpm"
    assert report["context"], "failure must carry UART context"


def test_destructive_requires_confirm(registry, tmp_path):
    runner = TestRunner(registry, tmp_path / "runs")
    script = runner.load_script(EXAMPLES / "tests" / "motor_start.yaml")
    script["steps"].insert(0, {"action": "power_cycle", "params": {"off": "1ms"}})
    with pytest.raises(TestError, match="confirmation"):
        runner.run(script)
    report = runner.run(script, confirm_token="confirm:motor_start")
    assert report["status"] == "PASS"
