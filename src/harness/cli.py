"""harness CLI：list-devices / get-info / discover / flash / monitor / run-test。

【定位】这是同一套 core 的"人类/CI 壳"。将来 MCP server 只是
另一个壳（调同样的 Registry + TestRunner），不存在第二套逻辑。

【退出码约定】（CI 和 Agent 判断成败只看它）
  0 = PASS    1 = FAIL    2 = ERROR（加载/执行出错）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .registry import RegistryError
from .runner import parse_duration
from .runtime import build_registry

app = typer.Typer(no_args_is_help=True, help="Hardware Harness: a hardware execution runtime for AI agents")
console = Console()


@app.command("list-devices")
def list_devices(devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir")):
    """列出已加载的设备：id / 名称 / adapter / 能力清单。"""
    registry = build_registry(devices_dir)
    table = Table(title="Devices")
    for col in ("id", "name", "adapter", "capabilities"):
        table.add_column(col)
    for dev in registry.devices.values():
        table.add_row(dev["id"], dev["name"], dev["match"]["adapter"],
                      ", ".join(c["capability"] for c in dev["capabilities"]))
    console.print(table)


@app.command("get-info")
def get_info(device_id: str, devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir")):
    """输出单个设备的完整定义（含 description/binding/telemetry）。

    这就是给 Agent 看的"设备自描述"—— LLM 调它即可知道这块板
    能干什么、命令怎么发、遥测有什么字段什么单位。
    """
    registry = build_registry(devices_dir)
    console.print_json(json.dumps(registry.get(device_id), ensure_ascii=False, default=str))


@app.command("discover")
def discover(devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir")):
    """枚举物理资源：串口列表 + ST-Link 探针列表。

    填设备 yaml 的 match.serial_number / probe_serial 就靠它。"""
    registry = build_registry(devices_dir)

    # ---- 串口（借道任意 serial adapter 的 discover 动作，拿结构化结果）----
    serial_adapter = registry.adapters.get("serial")
    result = serial_adapter.execute({"id": "_", "capabilities": []}, "core.lifecycle", "discover", {})
    table = Table(title="Serial Ports")
    for col in ("port", "vid", "pid", "serial_number", "description"):
        table.add_column(col)
    for p in result.data.get("ports", []):
        table.add_row(p["port"] or "", p["vid"] or "", p["pid"] or "",
                      p["serial_number"] or "", p["description"] or "")
    console.print(table)

    # ---- ST-Link 探针（pyocd 装了才列；没装给提示而不是报错）----
    # 注意 libusb 枚举在部分 Windows 机器上很慢甚至挂住，
    # 用线程 + 3 秒超时兜底：超时就跳过探针段，不能拖死整个命令
    from .stm32_adapter import HAVE_PYOCD
    if HAVE_PYOCD:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_list_probes)
            try:
                probes = future.result(timeout=3)
            except concurrent.futures.TimeoutError:
                probes = None
        if probes is None:
            console.print("[yellow]pyocd probe listing timed out (libusb slow)[/yellow] - skipped")
        else:
            t2 = Table(title="Debug Probes (pyocd)")
            t2.add_column("unique_id")
            t2.add_column("description")
            for probe in probes:
                t2.add_row(probe.unique_id, str(probe.description or ""))
            console.print(t2)
    else:
        console.print("[yellow]pyocd not installed[/yellow] - probe listing skipped "
                      "(pip install 'hardware-harness[stm32]' to enable)")


@app.command("build")
def build(
    device_id: str,
    devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir"),
):
    """执行设备声明的构建命令（binding.build），产出固件 artifact。"""
    from .builder import build_flow
    registry = build_registry(devices_dir)
    try:
        device = registry.get(device_id)
    except RegistryError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2)
    result = build_flow(device)
    console.print(result.get("output", "")[-1500:])   # 编译输出原文（错误可读）
    if result["ok"]:
        console.print(f"[green]build ok[/green] -> {result.get('artifact', '?')}")
    else:
        console.print(f"[red]build failed:[/red] {result['message']}")
        if result.get("hint"):
            console.print(f"[yellow]hint:[/yellow] {result['hint']}")
        raise typer.Exit(1)


@app.command("flash")
def flash(
    device_id: str,
    image: Path = typer.Argument(..., exists=True, readable=True),
    confirm: str = typer.Option(None, "--confirm", help="必须传 confirm:flash 才会真正烧录"),
    devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir"),
):
    """烧录固件到设备（destructive：需 --confirm confirm:flash + 探针身份核验）。"""
    # 走共享流程（runtime.flash_flow）：与 MCP server 完全同一套闸门
    from .runtime import flash_flow
    result = flash_flow(build_registry(devices_dir), device_id, str(image), confirm)
    if not result["ok"]:
        console.print(f"[red]flash failed:[/red] {result['message']}")
        if result.get("hint"):
            console.print(f"[yellow]hint:[/yellow] {result['hint']}")
        raise typer.Exit(2)
    console.print(f"[green]{result['message']}[/green]")
    console.print(result.get("detail", ""))


@app.command("monitor")
def monitor(
    device_id: str,
    duration: str = typer.Option("5s", "--duration", help="采集时长，如 5s / 500ms"),
    devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir"),
):
    """连接设备并实时打印换算后的遥测（每 200ms 一行 JSON）。"""
    registry = build_registry(devices_dir)
    device = registry.get(device_id)
    adapter = registry.adapter_for(device)

    result = adapter.execute(device, "core.lifecycle", "connect", {})
    if not result.ok:
        err = result.error or {}
        console.print(f"[red]connect failed:[/red] {err.get('message', '?')}")
        if err.get("hint"):
            console.print(f"[yellow]hint:[/yellow] {err['hint']}")
        raise typer.Exit(2)

    end = time.monotonic() + parse_duration(duration)
    try:
        while time.monotonic() < end:
            sample = adapter.read_telemetry(device)
            if sample:
                console.print_json(json.dumps(sample, ensure_ascii=False))
            time.sleep(0.2)
    finally:
        adapter.execute(device, "core.lifecycle", "disconnect", {})


@app.command("deploy")
def deploy(
    device_id: str,
    script: Path = typer.Argument(..., exists=True, readable=True),
    target: str = typer.Option("main.py", "--target", help="板上的目标文件名（main.py = 开机自启）"),
    devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir"),
):
    """部署应用脚本到 MicroPython 设备（B7：写文件系统，不动固件，安全）。

    例：harness deploy esp32s3_dev firmware/esp32/micropython/main.py"""
    registry = build_registry(devices_dir)
    try:
        device = registry.get(device_id)
        adapter = registry.adapter_for(device)
    except RegistryError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2)
    result = adapter.execute(device, "core.firmware", "install",
                             {"script": str(script), "target": target})
    if not result.ok:
        err = result.error or {}
        console.print(f"[red]deploy failed:[/red] {err.get('message', '?')}")
        if err.get("hint"):
            console.print(f"[yellow]hint:[/yellow] {err['hint']}")
        raise typer.Exit(2)
    console.print(f"[green]deployed[/green] {script.name} -> {device_id}:{target} "
                  f"(board reset, script now active)")


@app.command("verify")
def verify(
    device_id: str,
    devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir"),
):
    """读回 flash 与构建产物比对（read-only，不碰设备状态）。"""
    registry = build_registry(devices_dir)
    try:
        device = registry.get(device_id)
        adapter = registry.adapter_for(device)
    except RegistryError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(2)
    result = adapter.execute(device, "core.firmware", "verify", {})
    if not result.ok:
        err = result.error or {}
        console.print(f"[red]verify failed:[/red] {err.get('message', '?')}")
        if err.get("hint"):
            console.print(f"[yellow]hint:[/yellow] {err['hint']}")
        raise typer.Exit(1)
    console.print(f"[green]verified[/green] {result.data.get('verified')} bytes "
                  f"match {result.data.get('artifact')}")


@app.command("agent")
def agent(
    goal: str = typer.Argument(..., help="自然语言目标，如：验证 ESP32 电机能到 1000 rpm"),
    max_steps: int = typer.Option(12, "--max-steps", help="LLM 最大轮次（防小模型死循环）"),
    quiet: bool = typer.Option(False, "--quiet", help="不打印每步工具调用"),
):
    """自主 Agent：LLM（llama.cpp 本地 / DeepSeek API）驱动硬件工具完成目标。

    配置见 agent.toml 或环境变量（HARNESS_AGENT_PROVIDER=local|deepseek）。
    本地大脑先用 'harness agent-serve' 启动。危险操作（烧录）会暂停
    征求你的 y/n 批准。"""
    from .agent import run_agent
    from .llm import LLMClient, load_config

    config = load_config()
    client = LLMClient(config)
    if not client.health():
        console.print(f"[red]LLM 不可达:[/red] {config.base_url} "
                      f"(provider={config.provider}, model={config.model})")
        console.print("[yellow]hint:[/yellow] 本地模型先跑 'harness agent-serve'；"
                      "API 模型设置 DEEPSEEK_API_KEY 且 provider=deepseek")
        raise typer.Exit(2)

    def _confirm(description: str) -> bool:
        # 终端批准流：LLM 请求 + 人类 y/n —— 三道闸的人类一环
        console.print(f"[yellow]Agent 请求危险操作批准:[/yellow] {description}")
        return typer.confirm("允许执行？", default=False)

    result = run_agent(goal, config=config, max_steps=max_steps,
                       confirm_fn=_confirm, verbose=not quiet)
    console.print(f"[cyan]最终答复 ({result['status']}, {result['tool_calls']} 次工具调用):[/cyan]")
    console.print(result["final"] or "<无>")
    console.print(f"转写: {result['transcript']}")
    raise typer.Exit(0 if result["ok"] else 1)


@app.command("agent-serve")
def agent_serve(
    port: int = typer.Option(None, "--port", help="默认取 agent.toml/8080"),
    detach: bool = typer.Option(True, "--detach/--foreground", help="后台运行/前台占用终端"),
):
    """启动本地大脑：llama-server（Qwen3-8B，--jinja 开 tool-calling）。

    参数与 C:/llama 的实际安装对齐；模型/路径可在 agent.toml 覆盖。"""
    from .llm import load_config
    import subprocess
    import time as _t
    import urllib.request as _url

    config = load_config()
    port = port or config.port
    base = f"http://127.0.0.1:{port}/v1"
    # 已在线就不重复起（幂等）
    try:
        with _url.urlopen(f"{base}/models", timeout=3):
            console.print(f"[green]llama-server 已在线:[/green] {base}")
            return
    except Exception:
        pass

    exe = Path(config.llama_server_exe)
    model = Path(config.llama_model)
    if not exe.exists() or not model.exists():
        console.print(f"[red]llama.cpp 未找到:[/red] {exe}")
        console.print("[yellow]hint:[/yellow] 在 agent.toml 里改 llama_server_exe / llama_model")
        raise typer.Exit(2)

    # --jinja 是关键：用模型自带的 chat template，Qwen3 的 tool-calling
    # 依赖它；-ngl 默认 22 = 部分层上 GPU（4GB 显存实测值，见 L23），
    # 大显存机器在 agent.toml 里改 llama_ngl=99 全卸载提速
    cmd = [str(exe), "-m", str(model), "--port", str(port),
           "--jinja", "-ngl", str(config.llama_ngl),
           "-c", str(config.llama_ctx)]
    console.print(f"启动: {' '.join(cmd[:4])} ...")
    proc = subprocess.Popen(cmd,
                            stdout=subprocess.DEVNULL if detach else None,
                            stderr=subprocess.DEVNULL if detach else None,
                            creationflags=0x00000008 if detach else 0)  # DETACHED_PROCESS

    # 等就绪：大模型加载要几十秒（模型加载是 IO+显存拷贝，别设太短）
    console.print("等待模型加载（首次约 30~90s）...")
    deadline = _t.time() + 180
    while _t.time() < deadline:
        try:
            with _url.urlopen(f"{base}/models", timeout=3):
                console.print(f"[green]llama-server 就绪:[/green] {base} (pid {proc.pid})")
                console.print(f"试一句: harness agent \"列出硬件设备并总结\" ")
                return
        except Exception:
            if proc.poll() is not None:
                console.print("[red]llama-server 启动即退出[/red] —— 前台模式重跑看报错")
                raise typer.Exit(2)
            _t.sleep(2)
    console.print("[red]180s 未就绪[/red] —— 用 --foreground 前台跑看日志")
    raise typer.Exit(2)


@app.command("run-test")
def run_test(
    test_path: Path = typer.Argument(..., exists=True, readable=True),
    devices_dir: Path = typer.Option(Path("examples/devices"), "--devices-dir"),
    confirm: str = typer.Option(None, "--confirm", help="Confirmation token required by destructive tests"),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
):
    """执行测试剧本，打印 JSON 报告，报告与 artifact 落盘 runs/<run_id>/。"""
    # 走共享流程（runtime.run_test_flow）：与 MCP server 完全同一套逻辑
    from .runtime import run_test_flow
    result = run_test_flow(build_registry(devices_dir), test_path, confirm, runs_dir)
    if not result["ok"]:
        # 执行错误（剧本不合法/缺 token）：错误必须带 hint，Agent 读完就会修
        console.print(f"[red]error:[/red] {result['message']}")
        if result.get("hint"):
            console.print(f"[yellow]hint:[/yellow] {result['hint']}")
        raise typer.Exit(2)
    report = result["report"]
    console.print_json(json.dumps(report, ensure_ascii=False))
    # 退出码即结果：CI 和 Agent 靠它判断，不用解析 JSON
    raise typer.Exit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    app()
