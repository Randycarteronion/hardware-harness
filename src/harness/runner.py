"""Test Runner：解释执行 7 个步骤原语，产出结构化报告。

【设计立场】
Test Runner 只做"确定性重放"，不做逻辑：
  原语只有 7 个 —— reset / power_cycle / send / wait / assert / capture / run
  没有脚本语言、没有 if/for、没有变量。
需要逻辑的时候让 Agent 自己写逻辑 —— 那是 LLM 的强项。
Runner 的职责是"同一段剧本，今天和下个月跑出一样的结果"。

【产出】
每跑一次生成 runs/<run_id>/：
  report.json   —— 通过 report.schema.json 校验的结构化报告
  *.jsonl       —— capture 步骤的原始数据 artifact
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from pathlib import Path

import jsonschema
import yaml

from .adapters import Adapter
from .registry import DeviceRegistry, RegistryError

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

# 哪些 (能力, 动作) 组合属于 destructive —— 剧本里出现任何一个，
# 整个剧本就要求 confirmation token 才能执行
_DESTRUCTIVE_ACTIONS = {("core.firmware", "flash"), ("core.firmware", "rollback"), ("core.lifecycle", "power_cycle")}


def parse_duration(s: str) -> float:
    """把 "2s" / "200ms" / "1.5min" 解析成秒（float）。

    schema 里用字符串写时长是为了人类可读（YAML 里裸 2 会被当成数字，
    单位不明），进到代码里统一换成秒。
    """
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|min)", s)
    if not m:
        raise ValueError(f"bad duration: {s!r}")
    value, unit = float(m.group(1)), m.group(2)
    return value * {"ms": 0.001, "s": 1.0, "min": 60.0}[unit]


class TestFailure(Exception):
    """断言失败 = 测试本身有效但结果不满足（报告记 FAIL，不是程序 bug）。"""

    def __init__(self, detail: dict):
        super().__init__(detail.get("message", "assert failed"))
        self.detail = detail  # 直接进报告的 detail 字段：metric/op/expected/measured/unit/samples


class TestError(Exception):
    """执行错误 = 剧本外的故障（超时、设备 NAK、参数错误）。

    携带 hint —— 给 Agent 的自我修复提示，比如"端口被占用，先 disconnect"。
    """

    def __init__(self, message: str, hint: str = "", retryable: bool = False):
        super().__init__(message)
        self.code = "E_TEST_ERROR"
        self.hint = hint
        self.retryable = retryable


class TestRunner:
    def __init__(self, registry: DeviceRegistry, runs_dir: Path):
        self.registry = registry
        self.runs_dir = runs_dir  # 报告和 artifact 的落盘根目录

    # ------------------------------------------------------------------ api

    def load_script(self, path: Path) -> dict:
        """加载测试剧本 YAML 并按 test.schema.json 校验。

        步骤拼错（如 actoin）、assert 缺 value 之类的错误在这里拦截，
        不会跑到一半才炸。
        """
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads((SCHEMA_DIR / "test.schema.json").read_text(encoding="utf-8"))
        try:
            jsonschema.validate(doc, schema)
        except jsonschema.ValidationError as e:
            raise RegistryError(f"{path.name}: invalid test script: {e.message}") from e
        doc["_path"] = path
        return doc

    def script_risk(self, script: dict) -> str:
        """计算整个剧本的风险等级：取所有步骤的最高级。

        destructive > safe > read-only。含 flash/power_cycle 的剧本
        整体按 destructive 处理 —— 一次确认覆盖剧本内全部危险步骤，
        避免每步都打断 Agent。
        """
        order = {"read-only": 0, "safe": 1, "destructive": 2}
        worst = "read-only"
        for step in script["steps"]:
            if step["action"] in ("flash", "power_cycle", "rollback"):
                worst = "destructive"
            elif step["action"] in ("send", "reset"):
                worst = max(worst, "safe", key=lambda x: order[x])
        return worst

    def run(self, script: dict, confirm_token: str | None = None) -> dict:
        """执行剧本主入口。返回值 = 已落盘并通过 schema 校验的报告。

        流程：生成 run_id -> destructive 门禁 -> 重试循环 -> 逐步执行
        -> 汇总遥测/上下文/artifact -> 校验并写 report.json。
        """
        # run_id = 时间戳 + 随机短码：时间戳保证可排序，随机码防同秒冲突
        run_id = f"{dt.datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        device = self.registry.get(script["device"])   # 未知设备在这里就抛错
        adapter = self.registry.adapter_for(device)

        # ---- destructive 门禁 ----
        # token 格式固定为 confirm:<剧本名>：防"AI 乱来"（必须显式拿到
        # token 才能跑），但挡不住"人对错板子"——那是 identity_check 的职责
        if self.script_risk(script) == "destructive" and confirm_token != f"confirm:{script['name']}":
            raise TestError(
                "destructive test requires confirmation",
                hint=f"re-run with --confirm confirm:{script['name']} after verifying target device {device['id']}",
                retryable=True,
            )

        started = time.monotonic()
        report = {
            "schema_version": 1,
            "run_id": run_id,
            "test": script["name"],
            "device": device["id"],
            "status": "PASS",
            "started_at": dt.datetime.now(dt.UTC).isoformat(),
            "duration_ms": 0.0,
            "steps": [],
            "summary": {},
            "artifacts": [],
        }
        on_failure = script.get("on_failure", "abort")
        timeout = parse_duration(script.get("timeout", "60s"))
        artifacts: list[dict] = []

        # ---- 重试循环：1 次原始执行 + retry 次重试 ----
        # 只重试 FAIL（断言不满足，可能是硬件抖动）；ERROR（超时/NAK）不重试
        for attempt in range(1 + script.get("retry", 0)):
            report["steps"] = []
            report["status"] = "PASS"
            self._reset_device(adapter, device)  # 每次尝试都从已知状态开始，保证可复现
            try:
                for index, step in enumerate(script["steps"]):
                    # 每步之前检查总墙钟超时，防止设备挂死后无限等
                    if time.monotonic() - started > timeout:
                        raise TestError(f"test timeout after {script.get('timeout', '60s')}",
                                        hint="increase 'timeout' or check whether the device hung")
                    t0 = time.monotonic()
                    record = {"index": index, "action": step["action"], "status": "PASS", "duration_ms": 0.0}
                    try:
                        detail = self._exec_step(step, adapter, device, run_dir, artifacts)
                        record["detail"] = detail or {}
                    except TestFailure as f:
                        # 断言失败：记 FAIL；abort 模式下立即终止（continue 模式继续跑后续步骤）
                        record.update(status="FAIL", detail=f.detail)
                        report["status"] = "FAIL"
                        if on_failure == "abort":
                            report["steps"].append(record)
                            raise
                    except TestError as e:
                        # 执行错误：一律终止，不区分 abort/continue
                        record.update(status="ERROR", detail={"message": str(e), "hint": e.hint})
                        report["status"] = "ERROR"
                        report["steps"].append(record)
                        raise
                    record["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
                    report["steps"].append(record)
                break  # 全部步骤走完（PASS，或 FAIL+continue 且无 ERROR）—— 跳出重试循环
            except (TestFailure, TestError) as exc:
                # FAIL 且还有重试额度 -> 回到循环开头重来
                if attempt < script.get("retry", 0) and not isinstance(exc, TestError):
                    continue
                if isinstance(exc, TestError):
                    report["error"] = {"code": exc.code, "message": str(exc), "hint": exc.hint, "retryable": exc.retryable}
                if report["status"] == "PASS":
                    report["status"] = "FAIL"

        # ---- 收尾：最终遥测快照 + 失败现场 + 释放设备 + 落盘 ----
        report["summary"] = adapter.read_telemetry(device)          # 结束时刻的物理量快照
        report["context"] = adapter.read_log(device)[-50:]          # 最近 50 行设备输出（证据）
        report["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        report["artifacts"] = artifacts

        # 释放设备（断开 + 放锁）：不释放的话设备锁挂到进程退出，
        # MCP 场景下后续工具调用会被自己锁死（真板验证时踩过）
        adapter.execute(device, "core.lifecycle", "disconnect", {})

        # 报告自身也过 schema —— 它是 Agent 消费频率最高的 JSON，不能有结构意外
        schema = json.loads((SCHEMA_DIR / "report.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(report, schema)
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    # --------------------------------------------------------------- steps

    def _reset_device(self, adapter: Adapter, device: dict) -> None:
        """每次执行前的标准开场：连接 + 复位，让设备进入已知初始状态。"""
        adapter.execute(device, "core.lifecycle", "connect", {})
        adapter.execute(device, "core.lifecycle", "reset", {})

    def _exec_step(self, step: dict, adapter: Adapter, device: dict,
                   run_dir: Path, artifacts: list) -> dict:
        """单个步骤的分发器。返回进报告的 detail；失败抛 TestFailure/TestError。"""
        action, params = step["action"], step.get("params", {})

        if action == "reset":
            # 复位：让 MCU 回到启动状态
            result = adapter.execute(device, "core.lifecycle", "reset", {})
            return self._checked(result)

        if action == "power_cycle":
            # 断电重启：比 reset 更彻底（外设状态也清掉），destructive
            result = adapter.execute(device, "core.lifecycle", "power_cycle", params)
            return self._checked(result)

        if action == "send":
            # 发命令 + 可选期待回复：expect 是正则，reply 不匹配按 FAIL 处理
            result = adapter.execute(device, "core.link", "send", params)
            self._checked(result)
            if "expect" in params and not result.data.get("matched"):
                raise TestFailure({"message": f"reply did not match {params['expect']!r}",
                                   "reply": result.data.get("reply", "")})
            return {"reply": result.data.get("reply", "")}

        if action == "wait":
            # 纯等待：给硬件状态建立的时间（电机加速、传感器稳定...）
            time.sleep(parse_duration(params["duration"]))
            return {"slept": params["duration"]}

        if action == "assert":
            # 断言：详见 _assert —— 带时间窗和稳定期，不是瞬时采样
            return self._assert(params, adapter, device)

        if action == "capture":
            # 采集：以固定节奏轮询遥测 duration 秒，样本逐行写 jsonl artifact。
            # 轮询间隔 = min(0.2s, duration/25)：短采集采样密，长采集不刷爆文件
            duration = parse_duration(params["duration"])
            t0 = time.monotonic()
            lines: list[str] = []
            while time.monotonic() - t0 < duration:
                sample = adapter.read_telemetry(device)
                lines.append(json.dumps(sample, ensure_ascii=False))
                time.sleep(min(0.2, max(duration / 25, 0.02)))
            name = params.get("artifact", "capture")
            art = run_dir / f"{name}.jsonl"
            art.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # artifact 登记进报告：Agent 之后按路径取原始数据
            entry = {"name": name, "path": str(art), "mime_type": "application/x-ndjson", "bytes": art.stat().st_size}
            artifacts.append(entry)
            return {"lines": len(lines), "artifact": entry["path"]}

        if action == "run":
            # 嵌套剧本：刻意不在 runner 里解析文件路径 —— 保持 runner
            # 只依赖传入的数据结构，文件解析是 CLI 层的职责
            raise TestError(f"nested run step '{params['test']}' is not supported in this runner instance",
                            hint="resolve nested scripts in the CLI layer and call run() on each")

        raise TestError(f"unknown action {action!r}", "check schemas/test.schema.json")

    def _assert(self, params: dict, adapter: Adapter, device: dict) -> dict:
        """断言求值 —— 本模块最核心的一段。

        【为什么不是瞬时采样】硬件是惯性系统：命令发出后 rpm 要几百
        毫秒才爬上来。开机立刻断言 rpm>1000 必然假失败。

        【语义】
        within = 时间窗：条件必须在窗内某一刻变真（否则 FAIL）
        settle = 稳定期：变真之后还要持续保持这么久才算数（滤除毛刺）

        【实现】轮询采样，条件为真时记 stable_since；中途又变假则清零
        重新计；超过 deadline 仍不满足即抛 TestFailure（携带全部采样，
        报告里能看到"值到底爬没爬上来"）。
        """
        metric, op, expected = params["metric"], params["op"], params["value"]
        unit = device["telemetry"]["fields"].get(metric, {}).get("unit", "")
        within = parse_duration(params.get("within", "0s"))
        settle = parse_duration(params.get("settle", "0s"))
        deadline = time.monotonic() + max(within, 0.001)
        samples: list = []
        stable_since: float | None = None
        last_value = None
        while True:
            sample = adapter.read_telemetry(device, [metric])
            last_value = sample.get(metric)
            samples.append(last_value)
            if _compare(last_value, op, expected):
                if settle <= 0:
                    break  # 无稳定期要求，条件成立即通过
                if stable_since is None:
                    stable_since = time.monotonic()  # 开始计时
                elif time.monotonic() - stable_since >= settle:
                    break                             # 稳定够了，通过
            else:
                stable_since = None                   # 条件又变假，稳定计时清零
                if time.monotonic() >= deadline:
                    raise TestFailure(self._fail_detail(metric, op, expected, last_value, unit, samples, params))
            # 窗内但条件尚未成立：也要能在 deadline 处退出
            if time.monotonic() >= deadline and stable_since is None:
                raise TestFailure(self._fail_detail(metric, op, expected, last_value, unit, samples, params))
            # 轮询间隔 = min(0.1s, within/10)：窗越短采样越密
            time.sleep(min(0.1, max(within / 10, 0.01)))
        return {"metric": metric, "op": op, "expected": expected,
                "measured": last_value, "unit": unit, "samples": samples[-10:]}

    @staticmethod
    def _fail_detail(metric, op, expected, measured, unit, samples, params) -> dict:
        """构造断言失败的 detail：实测值 + 单位 + 最近采样，给 Agent 判断差多少。"""
        return {
            "metric": metric, "op": op, "expected": expected,
            "measured": measured, "unit": unit, "samples": samples[-10:],
            "message": f"{metric}={measured}{unit} did not satisfy {op} {expected} within {params.get('within', '0s')}",
        }

    def _checked(self, result) -> dict:
        """把 ActionResult 转成异常协议：ok=False 时抛 TestError（含 hint）。"""
        if not result.ok:
            err = result.error or {}
            raise TestError(err.get("message", "action failed"), err.get("hint", ""), err.get("retryable", False))
        return result.data


def _compare(value, op: str, expected) -> bool:
    """按操作符比较。类型不匹配（如 contains 用于数字）返回 False 而不是崩。"""
    try:
        return {
            "eq": lambda: value == expected,
            "ne": lambda: value != expected,
            "gt": lambda: value > expected,
            "ge": lambda: value >= expected,
            "lt": lambda: value < expected,
            "le": lambda: value <= expected,
            "in_range": lambda: expected[0] <= value <= expected[1],
            "contains": lambda: expected in value,
        }[op]()
    except (TypeError, KeyError):
        return False
