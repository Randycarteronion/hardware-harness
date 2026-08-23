"""内置 Agent —— LLM 大脑驱动 8 个硬件工具的自主循环。

【这是什么】
`harness agent "目标"` 的实现：把自然语言目标交给 LLM（本地
llama.cpp 或 DeepSeek API，见 llm.py），LLM 通过标准 tool-calling
协议调用硬件工具（hardware_list_devices / flash / run_test ...），
工具结果回灌给 LLM，直到任务完成或步数耗尽。

【与 MCP server 的关系】
MCP 是"外脑接入"（DSH/Claude Code 当 Agent Host）；
本模块是"自带大脑"（Harness 自己跑 Agent 循环）。
两者复用同一份工具实现（mcp_server.TOOL_FUNCS）—— 行为永远一致，
不存在第二套工具逻辑（架构原则：一套实现，N 个壳）。

【安全模型（与三道闸完全兼容）】
- read-only/safe 工具（monitor/run_test/deploy/build）：Agent 自主
- destructive（flash）：工具层会拒绝无 token 的调用并返回指引；
  系统提示词要求 LLM 此时输出 "ASK_CONFIRM: <理由>" 暂停循环，
  由 confirm_fn（CLI 里是终端 y/n，测试里是回调）决定是否放行；
  放行后以用户消息注入批准，LLM 才能带 token 重试。
  LLM 永远拿不到"绕过闸门"的能力 —— 它只能请求，不能自批。

【循环上限】
max_steps 防止小模型（本地 8B）陷入复读机循环 —— 到顶即终止，
转写照常归档（run 的 artifact 哲学：过程数据比结论珍贵）。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from .llm import LLMClient, LLMConfig, LLMError
from .mcp_server import TOOL_FUNCS

# ---------------------------------------------------------------------------
# 工具 schema —— OpenAI tools 格式。
# 描述文案与 mcp_server.py 的 docstring 语义一致；名字集合有同步测试
# （tests/test_agent_loop.py::test_tools_spec_synced_with_mcp），改工具
# 时测试会拽着你更新这里。
# 嵌套字典全部走 _fn 构造器 —— 手写五层嵌套必漏括号（血泪教训）。
# ---------------------------------------------------------------------------


def _fn(name: str, desc: str, params: dict) -> dict:
    """构造一个 OpenAI function 工具条目。"""
    return {"type": "function",
            "function": {"name": name, "description": desc, "parameters": params}}


_P = {"type": "object", "properties": {}}   # 无参数模板
_S = {"type": "string"}                     # 字符串参数简写

TOOLS_SPEC = [
    _fn("hardware_list_devices",
        "列出可用硬件设备（id/名称/能力/安全等级）。操作设备前先调它。", _P),
    _fn("hardware_get_info",
        "读设备完整定义：命令格式、遥测字段与单位、安全注意事项。",
        {"type": "object", "required": ["device_id"],
         "properties": {"device_id": _S}}),
    _fn("hardware_discover",
        "枚举本机串口与调试探针。无 VID 的串口是蓝牙虚拟口，不是设备。", _P),
    _fn("hardware_build",
        "执行设备声明的固件构建。编译错误原文在 output 里，读它改代码。",
        {"type": "object", "required": ["device_id"],
         "properties": {"device_id": _S}}),
    _fn("hardware_deploy",
        "部署 Python 应用脚本到 MicroPython 设备（安全，不碰固件），部署后自动生效。",
        {"type": "object", "required": ["device_id", "script"],
         "properties": {"device_id": _S, "script": _S, "target": _S}}),
    _fn("hardware_flash",
        "[DESTRUCTIVE] 烧录固件，需 confirm_token='confirm:flash'（向人类申请，不可自批）。",
        {"type": "object", "required": ["device_id", "image"],
         "properties": {"device_id": _S, "image": _S, "confirm_token": _S}}),
    _fn("hardware_monitor",
        "采集一段时间的遥测（read-only）。返回带单位的样本和串口日志尾部。",
        {"type": "object", "required": ["device_id"],
         "properties": {"device_id": _S, "duration_s": {"type": "integer"}}}),
    _fn("hardware_run_test",
        "执行测试剧本，返回结构化报告。status=='PASS' 才算过；FAIL 看 detail.measured。",
        {"type": "object", "required": ["test_path"],
         "properties": {"test_path": _S, "confirm_token": _S}}),
]

SYSTEM_PROMPT = """你是 Hardware Harness 的硬件测试 Agent，操作真实嵌入式设备。

工作准则：
1. 动手前先 hardware_list_devices 了解设备，再 hardware_get_info 读字段和单位
2. 遥测数值永远带单位理解（rpm / degC），断言比较时注意
3. 测试判据：run_test 返回 report.status == 'PASS'；FAIL 时读 FAIL 步骤的
   detail.measured（实测值）判断差多少，再决定下一步
4. 烧录（flash）是危险操作：无 token 会被拒绝。需要烧录时，回复以
   "ASK_CONFIRM: <为什么需要烧录>" 开头并停止调用工具，等人类批准。
   严禁编造 confirm_token —— 它只能来自人类的批准消息
5. 设备可能没插：连接类错误先看 hint（"插了吗"），如实汇报，不要瞎重试
6. 任务完成或无法推进时，输出最终总结（中文，含关键数值），不再调用工具
7. 每一步都要基于工具的真实返回值说话，不要臆造数据"""


class AgentResult(dict):
    """Agent 一次运行的产出（dict 语法糖）：final/状态/步数/转写路径。"""


def run_agent(goal: str, config: LLMConfig | None = None,
              max_steps: int = 12,
              confirm_fn=None,
              runs_dir: Path = Path("runs"),
              verbose: bool = True) -> AgentResult:
    """Agent 主循环。

    参数：
      goal        自然语言目标
      config      LLM 配置（None 用 load_config 默认链）
      max_steps   最多 LLM 轮次（防小模型死循环）
      confirm_fn  危险操作批准回调 fn(description)->bool；
                  None = 无人批准（ASK_CONFIRM 一律拒绝，测试/CI 用）
      verbose     打印每步工具调用（CLI 观察用）
    """
    from .llm import load_config
    config = config or load_config()
    client = LLMClient(config)

    # 转写归档：runs/agent_<时间>_<随机>/transcript.md（过程即 artifact）
    run_id = f"agent_{dt.datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    transcript_dir = runs_dir / run_id
    transcript_dir.mkdir(parents=True, exist_ok=True)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": goal},
    ]
    tool_calls_total = 0
    final_text = ""
    status = "MAX_STEPS"
    step = 0

    def _log(line: str) -> None:
        if verbose:
            print(line, flush=True)

    try:
        for step in range(1, max_steps + 1):
            message = client.chat(messages, tools=TOOLS_SPEC)
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []

            # ---- 人工确认门：LLM 请求烧录类批准 ----
            if content.startswith("ASK_CONFIRM:"):
                _log(f"[agent] 请求批准: {content[len('ASK_CONFIRM:'):].strip()}")
                approved = bool(confirm_fn and confirm_fn(content))
                reply = ("HUMAN_APPROVED: 人类已批准该烧录。本次可携带 "
                         "confirm_token='confirm:flash' 重试。"
                         if approved else
                         "HUMAN_REJECTED: 人类拒绝了该操作。不要烧录，"
                         "汇报现状并结束。")
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": reply})
                continue

            if not calls:
                # 没有工具调用 = LLM 认为任务结束（正常出口）
                final_text = content
                status = "DONE"
                break

            # 把 assistant 的工具调用意图记入历史（协议要求原样回填）
            messages.append({"role": "assistant",
                             "content": content,
                             "tool_calls": calls})
            for call in calls:
                fn_name = call["function"]["name"]
                try:
                    fn_args = json.loads(call["function"].get("args") or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                _log(f"[step {step}] {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:120]})")
                # 执行 = 直接调 mcp_server 注册的同一份 Python 函数
                fn = TOOL_FUNCS.get(fn_name)
                result_text = (fn(**fn_args) if fn
                               else json.dumps({"ok": False,
                                                "message": f"unknown tool {fn_name}"}))
                tool_calls_total += 1
                # 长结果截断进历史：LLM 上下文有限（本地 8B 尤甚），
                # 保留头尾 —— 头部有 ok/状态，尾部有 hint 和实测值
                if len(result_text) > 4000:
                    result_text = result_text[:800] + "\n...<truncated>...\n" + result_text[-2600:]
                messages.append({"role": "tool",
                                 "tool_call_id": call.get("id", fn_name),
                                 "content": result_text})
    except LLMError as e:
        _write_transcript(transcript_dir, goal, messages, f"LLM_ERROR: {e}")
        return AgentResult(ok=False, status="LLM_ERROR", final=str(e),
                           steps=step, tool_calls=tool_calls_total,
                           transcript=str(transcript_dir / "transcript.md"))

    _write_transcript(transcript_dir, goal, messages,
                      final_text or f"<{status}: 无最终答复>")
    _log(f"[agent] {status}，共 {tool_calls_total} 次工具调用，"
         f"转写: {transcript_dir / 'transcript.md'}")
    return AgentResult(ok=(status == "DONE"), status=status, final=final_text,
                       steps=step, tool_calls=tool_calls_total,
                       transcript=str(transcript_dir / "transcript.md"))


def _write_transcript(directory: Path, goal: str, messages: list[dict], outcome: str) -> None:
    """把整段对话渲染成 Markdown 存档（人可读，排错第一现场）。"""
    lines = [f"# Agent Run — {directory.name}",
             f"- 目标: {goal}",
             f"- 结局: {outcome}", "",
             "|#|角色|内容|", "|---|---|---|"]
    for i, m in enumerate(messages):
        role = m["role"]
        content = m.get("content") or ""
        if m.get("tool_calls"):
            names = ", ".join(c["function"]["name"] for c in m["tool_calls"])
            content = (content + f" ⚙call[{names}]").strip()
        content = content.replace("\n", "<br>").replace("|", "\\|")[:2000]
        lines.append(f"|{i}|{role}|{content}|")
    (directory / "transcript.md").write_text("\n".join(lines), encoding="utf-8")
