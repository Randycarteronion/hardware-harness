"""LLM 客户端层 —— 一个 OpenAI 兼容实现，两个 Provider（本地/API）。

【设计背景】
用户要求同时支持：
  1. 本地模型：llama.cpp 的 llama-server（C:/llama/...，CUDA 版）
     —— 它暴露 OpenAI 兼容的 /v1/chat/completions
  2. API 模型：DeepSeek 官方 API —— 同样是 OpenAI 兼容协议
两个 Provider 协议同构，所以只需要 ONE 客户端 + 一份配置：

  provider=local    -> base_url http://127.0.0.1:8080/v1（llama-server）
  provider=deepseek -> base_url https://api.deepseek.com

【配置解析优先级】
  环境变量 HARNESS_AGENT_* > 项目根 agent.toml > 内置默认（local）。
  API key 只从环境变量读（DEEPSEEK_API_KEY），绝不写进文件 —— 密钥
  不落盘是底线。

【Qwen3 兼容处理】
Qwen3 系列在思考模式下会把推理过程包在 <think>...</think> 里，
思维链会干扰后续解析 ——_strip_think() 统一剥掉（llama.cpp 本地
常见；DeepSeek reasoner 模型的思维链在单独字段，不经过这里）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[2]

# 默认值与 C:/llama 实际安装对齐（用户本机实测路径）；
# 都能被 agent.toml / 环境变量覆盖
DEFAULTS = {
    "base_url": "http://127.0.0.1:8080/v1",
    "model": "qwen3-8b",
    "llama_server_exe": r"C:\llama\llama-b10545-bin-win-cuda-12.4-x64\llama-server.exe",
    "llama_model": r"C:\llama\QwenQwen3-8B-GGUF\Qwen3-8B-Q4_K_M.gguf",
    "port": 8080,
}


@dataclass
class LLMConfig:
    """一份配置描述"大脑在哪"。

    provider 字段只影响文档性提示（两个 provider 协议一致）；
    真正的切换是 base_url + model。
    """
    provider: str = "local"          # local | deepseek
    base_url: str = DEFAULTS["base_url"]
    model: str = DEFAULTS["model"]
    api_key: str = ""                # local 不需要；deepseek 从环境变量读
    temperature: float = 0.3         # 硬件操作要稳：低温少发散
    max_tokens: int = 2048
    # 仅 local 用：agent-serve 拉起 llama-server 的参数（见 cli.py）
    llama_server_exe: str = DEFAULTS["llama_server_exe"]
    llama_model: str = DEFAULTS["llama_model"]
    port: int = DEFAULTS["port"]
    # 【实测 L23】本机 RTX 3050 Laptop（4GB）在 WDDM 下实际可分配显存
    # 不足 1.5GB（显示栈+桌面应用占用，nvidia-smi 的 free 不可信），
    # 连 10 层卸载都 OOM —— 默认 0（纯 CPU，约 4~10 tok/s，可用）。
    # ≥8GB 显存的台式卡改 99 全卸载提速。
    llama_ngl: int = 0
    llama_ctx: int = 8192
    extra: dict = field(default_factory=dict)


def load_config(overrides: dict | None = None) -> LLMConfig:
    """配置解析：环境变量 > agent.toml > 默认值。

    agent.toml 放项目根（[agent] 段），示例：
        [agent]
        provider = "local"           # 或 "deepseek"
        model = "qwen3-8b"
        # base_url = "https://api.deepseek.com"
    """
    cfg = LLMConfig()

    # ---- agent.toml ----
    toml_path = REPO_ROOT / "agent.toml"
    if toml_path.exists():
        try:
            import tomllib
            data = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("agent", {})
            for key in ("provider", "base_url", "model", "temperature"):
                if key in data:
                    setattr(cfg, key, data[key])
        except Exception:
            pass   # 配置文件坏了就用默认值 —— 大脑的启动不能被配置卡死

    # ---- 环境变量（优先级最高，方便 CI/临时切换）----
    env_map = {
        "provider": "HARNESS_AGENT_PROVIDER",
        "base_url": "HARNESS_AGENT_BASE_URL",
        "model": "HARNESS_AGENT_MODEL",
    }
    for attr, env in env_map.items():
        if os.environ.get(env):
            setattr(cfg, attr, os.environ[env])

    # ---- api key 只走环境变量 ----
    cfg.api_key = os.environ.get("DEEPSEEK_API_KEY", "")

    # provider 语义补全：选 deepseek 但没给 base_url 时自动指官方端点
    if cfg.provider == "deepseek" and "HARNESS_AGENT_BASE_URL" not in os.environ:
        cfg.base_url = "https://api.deepseek.com"
        if not cfg.api_key:
            cfg.api_key = "<<missing DEEPSEEK_API_KEY>>"

    for key, value in (overrides or {}).items():
        setattr(cfg, key, value)
    return cfg


class LLMError(Exception):
    """LLM 调用失败。message 里带排查 hint（Agent/人都能读）。"""


def _strip_think(text: str) -> str:
    """剥掉 Qwen3 的 <think>...</think> 思维链（含未闭合的残段）。"""
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # 流式/截断时可能只剩半截 <think> 开头：从标签起全部丢弃
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


class LLMClient:
    """极简 OpenAI 兼容 chat 客户端（urllib 实现，零新依赖）。

    只实现 Agent 循环需要的东西：一次带 tools 的非流式 chat。
    为什么要自己写而不用 openai SDK：依赖重、版本漂移快；这里
    协议面就一个端点，百行内自己实现更稳。
    """

    def __init__(self, config: LLMConfig):
        self.cfg = config

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """一次对话。返回解析后的 choices[0].message（dict）。

        失败抛 LLMError —— 常见三类都给出可行动 hint：
          连不上（llama-server 没起）/ 401（key 错）/ 400（模板不支持 tools）
        """
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        req = urllib.request.Request(
            f"{self.cfg.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.cfg.api_key:
            req.add_header("Authorization", f"Bearer {self.cfg.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code == 401:
                raise LLMError(f"401 unauthorized: {detail}. "
                               f"检查 DEEPSEEK_API_KEY 是否设置/有效") from e
            if e.code == 400 and "tool" in detail.lower():
                raise LLMError(
                    f"400 tool-calling rejected: {detail}. "
                    f"llama-server 需要以 --jinja 启动才支持 tools —— "
                    f"用 'harness agent-serve' 拉起即已带正确参数") from e
            raise LLMError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(
                f"cannot reach {self.cfg.base_url}: {e.reason}. "
                f"本地模型先用 'harness agent-serve' 启动 llama-server") from e

        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected response shape: {json.dumps(body)[:300]}") from e

        # Qwen3 思维链剥离：content 里的 <think> 不该进对话历史
        if isinstance(message.get("content"), str):
            message["content"] = _strip_think(message["content"])
        return message

    def health(self) -> bool:
        """探活（agent 命令启动前先看大脑在不在线，报错才有的放矢）。"""
        try:
            with urllib.request.urlopen(f"{self.cfg.base_url.rstrip('/')}/models",
                                        timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False
