"""MCP server 端到端测试 —— 真 stdio 子进程 + 真客户端调用。

【验证什么】
不是测工具函数本身（那是单测的事），而是测"Agent 视角的完整链路"：
子进程拉起 -> MCP 握手 -> tools/list -> 工具调用 -> 结构化 JSON 返回。
这个测试过了，Claude Code / DSH / Codex 接进来就是这个行为。

安全闸门也在 MCP 层面验证：flash 不带 token 必须被拒，
且错误信息要引导 Agent "向用户要确认"而不是硬闯。

【技术说明】MCP 客户端是 anyio 异步 API，测试用 anyio 自带的
pytest 插件跑（anyio 是 mcp 的依赖，无需额外安装）。
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO / "examples" / "tests"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_server"],
        cwd=str(REPO),
    )


@asynccontextmanager
async def _session():
    """拉起 server 子进程并完成 MCP 握手，yield 一个可用会话。"""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def _call(session, tool: str, args: dict) -> dict:
    """调工具并解析返回的 JSON 信封（Agent 视角的一致行为）。"""
    result = await session.call_tool(tool, args)
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_tools_listed():
    """Agent 拉起后能看到 6 个工具。"""
    async with _session() as s:
        tools = (await s.list_tools()).tools
        names = {t.name for t in tools}
        assert names == {
            "hardware_list_devices", "hardware_get_info", "hardware_discover",
            "hardware_build", "hardware_deploy", "hardware_flash",
            "hardware_monitor", "hardware_run_test",
        }


@pytest.mark.anyio
async def test_list_devices_returns_all():
    """设备清单：mock/stm32/esp32 全部就位，含能力与安全等级。"""
    async with _session() as s:
        data = await _call(s, "hardware_list_devices", {})
        assert data["ok"] is True
        ids = {d["id"] for d in data["devices"]}
        assert {"stm32_motor", "bluepill_f103", "esp32s3_dev"} <= ids
        bp = next(d for d in data["devices"] if d["id"] == "bluepill_f103")
        assert "core.firmware" in bp["capabilities"]


@pytest.mark.anyio
async def test_flash_gate_blocks_without_token():
    """MCP 层闸门：无 token 必须拒绝，且 hint 引导 Agent 找人类要确认。"""
    fake_bin = REPO / "firmware" / "stm32" / "f103_motor_demo" / "fw.bin"
    if not fake_bin.exists():  # 没编译固件也能测闸门——随便找个真文件
        fake_bin = REPO / "README.md"
    async with _session() as s:
        data = await _call(s, "hardware_flash", {
            "device_id": "bluepill_f103", "image": str(fake_bin)})
        assert data["ok"] is False
        assert "confirm:flash" in data["hint"]
        assert "refused" in data["message"]


@pytest.mark.anyio
async def test_run_test_mock_passes():
    """Agent 全链路：通过 MCP 跑 mock 测试剧本，报告结构完整。"""
    async with _session() as s:
        data = await _call(s, "hardware_run_test", {
            "test_path": str(TESTS_DIR / "motor_start.yaml")})
        assert data["ok"] is True
        report = data["report"]
        assert report["status"] == "PASS"
        assert report["steps"][3]["detail"]["unit"] == "rpm"
        assert report["steps"][3]["detail"]["measured"] > 1000


@pytest.mark.anyio
async def test_run_test_missing_file_hint():
    """错误路径：不存在的剧本 -> ok=false + hint（LLM 自我修复依据）。"""
    async with _session() as s:
        data = await _call(s, "hardware_run_test", {
            "test_path": "no_such_test.yaml"})
        assert data["ok"] is False
        assert "not found" in data["message"]
