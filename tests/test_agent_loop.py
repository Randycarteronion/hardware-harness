"""内置 Agent 循环测试 —— 进程内假 LLM 服务器驱动真工具，全程离线。

【验证什么】
Agent 循环的正确性不依赖某个具体模型：这里起一个真 HTTP 服务器
（OpenAI /chat/completions 协议），按剧本返回 tool_calls / 文本，
验证：工具真的被执行（mock 设备的遥测进入对话）、ASK_CONFIRM
人工批准流、步数上限、工具表与 MCP 同步。
真 Qwen3-8B 的实测见 docs/模型接入.md 的"实测记录"。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harness.agent import TOOLS_SPEC, run_agent
from harness.llm import LLMConfig


class ScriptedLLM:
    """按剧本逐条返回响应的假 OpenAI 服务器。

    script: 每项是一次请求的响应 —— dict 带 tool_calls，或 str 为纯文本
    收到的请求（messages 等）存入 received 供断言。
    """

    def __init__(self, script: list):
        self.script = script
        self.received: list[dict] = []
        self.server = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "ScriptedLLM":
        self.thread.start()
        return self

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def stop(self) -> None:
        self.server.shutdown()

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802（http.server 约定）
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.received.append(body)
                reply = outer.script[len(outer.received) - 1]
                if isinstance(reply, str):
                    message = {"role": "assistant", "content": reply}
                else:
                    message = {"role": "assistant", "content": "", "tool_calls": [reply]}
                out = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):  # 静音访问日志
                pass

        return Handler


def _tool_call(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "args": json.dumps(args)}}


def _cfg(port: int) -> LLMConfig:
    return LLMConfig(provider="local", base_url=f"http://127.0.0.1:{port}/v1",
                     model="fake-model")


def test_agent_executes_tools_and_finishes(tmp_path):
    """两步工具 + 收尾：mock 设备遥测真实进入对话历史。"""
    llm = ScriptedLLM([
        _tool_call("hardware_list_devices", {}),
        _tool_call("hardware_monitor", {"device_id": "stm32_motor", "duration_s": 1}),
        "任务完成：设备清单已核对，遥测正常。",
    ]).start()
    try:
        result = run_agent("看看有什么设备", config=_cfg(llm.port),
                           confirm_fn=None, runs_dir=tmp_path, verbose=False)
    finally:
        llm.stop()
    assert result["ok"] and result["status"] == "DONE"
    assert result["tool_calls"] == 2
    # 第二轮请求里应包含第一轮工具的执行结果（list 的设备 id）
    tool_msgs = [m for m in llm.received[1]["messages"] if m["role"] == "tool"]
    assert any("stm32_motor" in m["content"] for m in tool_msgs)
    # 转写落盘
    assert (tmp_path / result["transcript"].split("\\")[-2] / "transcript.md").exists() or \
           Path(result["transcript"]).exists()


def test_agent_confirm_flow_approved(tmp_path):
    """ASK_CONFIRM 批准流：LLM 申请烧录 -> 人类批准 -> 注入批准消息。"""
    llm = ScriptedLLM([
        _tool_call("hardware_flash", {"device_id": "stm32_motor", "image": "x.bin"}),  # 无 token 被拒
        "ASK_CONFIRM: 需要烧录新固件以修复转速",                       # 申请
        _tool_call("hardware_flash", {"device_id": "stm32_motor", "image": "x.bin",
                                      "confirm_token": "confirm:flash"}),  # 带 token 重试
        "烧录完成，任务结束。",
    ]).start()
    approvals = []

    def confirm(desc):
        approvals.append(desc)
        return True

    try:
        result = run_agent("重刷固件", config=_cfg(llm.port), confirm_fn=confirm,
                           runs_dir=tmp_path, verbose=False)
    finally:
        llm.stop()
    assert result["ok"]
    assert approvals, "确认回调必须被调用"
    # 第三轮对话历史里应有人类批准消息
    assert any(m.get("content", "").startswith("HUMAN_APPROVED")
               for m in llm.received[2]["messages"])


def test_agent_confirm_flow_rejected_stops_flashing(tmp_path):
    """拒绝流：人类说不 -> LLM 收到拒绝消息，不再被允许烧录。"""
    llm = ScriptedLLM([
        "ASK_CONFIRM: 想烧录",
        "好的，人类拒绝了。我汇报现状并结束：未执行任何烧录。",
    ]).start()
    try:
        result = run_agent("烧一下", config=_cfg(llm.port),
                           confirm_fn=lambda d: False, runs_dir=tmp_path, verbose=False)
    finally:
        llm.stop()
    assert result["ok"]
    assert any(m.get("content", "").startswith("HUMAN_REJECTED")
               for m in llm.received[1]["messages"])


def test_agent_max_steps_guard(tmp_path):
    """步数上限：无限要工具的模型到顶即停，转写仍归档。"""
    loop_call = _tool_call("hardware_list_devices", {})
    llm = ScriptedLLM([loop_call] * 20).start()
    try:
        result = run_agent("无限循环", config=_cfg(llm.port), max_steps=3,
                           runs_dir=tmp_path, verbose=False)
    finally:
        llm.stop()
    assert result["ok"] is False and result["status"] == "MAX_STEPS"
    assert result["steps"] == 3


def test_tools_spec_synced_with_mcp():
    """工具表同步检查：agent 的 schema 名字必须与 MCP 暴露的完全一致。

    改了 mcp_server 的工具而忘了改 agent.py 的 TOOLS_SPEC，
    这里立刻红 —— 双表漂移是最容易犯的维护错误。"""
    from harness.mcp_server import TOOL_FUNCS
    spec_names = {t["function"]["name"] for t in TOOLS_SPEC}
    assert spec_names == set(TOOL_FUNCS), \
        f"工具表漂移: agent={spec_names} vs mcp={set(TOOL_FUNCS)}"


from pathlib import Path  # noqa: E402 （放底部避免打断测试布局阅读）
