# 硬件趟坑经验台账（Lessons Register）

> 规则：每条经验必须落到某个具体位置——功能修复、代码注释或设备声明——
> 不允许只存在于人的记忆里。新坑踩完当场登记，注明"固化到哪"。
> 跨项目的通用坑进本文件；单个文件的实现细节进该文件注释。

## L1 — pyserial 开口默认拉高 DTR/RTS，ESP32 被按在复位态（2026-08-23）

**症状**：串口打开成功，读线程正常，但板子零输出。
**根因**：pyserial 打开端口时默认把 DTR/RTS 置为激活态；ESP32 的
EN/IO0 由这两根线经自动复位电路控制，RTS 持续激活 = 一直按住复位。
**固化**：`serial_adapter.py SerialTransport.open()`——开口后立刻
`dtr=False; rts=False` 再脉冲复位（与 esptool/mpremote 行为一致）。
所有串口设备自动受益。

## L2 — 复位后立刻发命令会丢：启动有时延（2026-08-23）

**症状**：reset 后马上 send，命令无响应；等 0.5~1s 再发就正常。
**根因**：ROM 引导 + 固件初始化需要 300~500ms（MicroPython ~1s）。
**固化**：`serial_adapter.py _lifecycle reset`——读 binding 的
`boot_time`（如 "1s"）自动等待；每块设备按自己实际情况声明。
设备示例见 `examples/devices/esp32s3_dev.yaml`。

## L3 — 换固件家族必须先 erase_flash（2026-08-23）

**症状**：摄像头固件直接覆盖烧 MicroPython 后，文件系统报损坏。
**根因**：旧固件的数据区（nvs/文件系统）残留，新固件按自己的布局
解读出垃圾。
**固化**：`esp32_adapter.py` flash 失败 hint 里提示先
`esptool erase_flash`；流程经验记入模块 docstring。

## L4 — PSRAM 变体：N16R8 是八线，标准构建按四线初始化（2026-08-23）

**症状**：启动报 `quad_psram: PSRAM chip is not connected`。
**结论**：非致命——ESP-IDF 放弃 PSRAM 继续启动；不用 PSRAM 的应用
可忽略。选型时注意 R8=octal。
**固化**：`esp32_adapter.py` docstring + 本条。

## L5 — 国内网络：GitHub 大文件极慢，优先预编译/镜像（2026-08-23）

**数据**：Arduino ESP32 工具链 394MB @ ~60KB/s ≈ 5.5h；
MicroPython 官方固件 1.75MB 从 micropython.org 秒下。
**策略**：原型验证用 MicroPython；C 工具链夜间挂机下载，或找镜像
（dl.espressif.cn）。
**固化**：`esp32_adapter.py` docstring；arduino-cli 留在 `tools/`。

## L6 — MicroPython 1.28 的 sys.stdout 没有 flush()（2026-08-23）

**固化**：`firmware/esp32/micropython/main.py` emit() 注释。

## L7 — mpremote 新版语法是 `connect COM6`，不是 `--port`（2026-08-23）

且开串口瞬间可能触发一次板子复位（DTR/RTS 电平变化），首次命令
若失败，等板子起来重试即可。
**固化**：本条。

## L8 — Windows 蓝牙虚拟串口污染设备发现（2026-08-23）

**症状**：list_ports 列出 "蓝牙链接上的标准串行 (COMx)"，无 VID/PID。
**规则**：设备匹配永远用 match.serial_number / usb_vid+pid，
绝不按"第一个 COM 口"猜。
**固化**：`serial_adapter.py resolve_port()` 注释。

## L9 — pyocd 探针枚举在 Windows 可能挂死（libusb）（2026-08-23）

**固化**：`cli.py discover`——线程 + 3s 超时兜底，超时跳过探针段。

## L10 — CH343 实测参数（2026-08-23）

烧录波特率 921600 稳定（1.75MB / 16.6s）；USB 序列号可直接做
match.serial_number；VID 1A86 PID 55D3。
**固化**：`examples/devices/esp32s3_dev.yaml`。

## L11 — Harness 只看行协议，不关心固件语言（2026-08-23）

同一套设备定义/测试剧本，C 固件和 MicroPython 固件都能跑通——
只要按行协议输出（`harness_firmware_stub.h` 或 main.py 的 emit）。
这是架构分层的直接收益。
**固化**：本条（架构原则级）。

## L12 — 山寨 ST-Link 老固件（V2J16M4）pyocd 拒连，ST-LINK_CLI 兼容（2026-08-23）

**症状**：pyocd ProbeError "unsupported, older firmware version, must be
at least v2J24"。
**策略**：不升级探针固件（山寨升级有变砖风险且要 GUI）。用户机器上
现成的 STM32 ST-LINK Utility 自带 ST-LINK_CLI.exe，原生支持老固件。
**固化**：`stm32_adapter.py` 双工具路径——binding.tool 选 `stlink_cli`
或 `pyocd`，换工具不改测试剧本（capability/binding 分离设计的兑现）。

## L13 — ST-LINK_CLI v1.4.0 的坑（2026-08-23）

  - 退出码不可靠（失败也可能返回 0）——成败必须按输出关键词判断
  - 不支持 -ListStlink（多探针无法选择）、不支持 -Dump（读内存用 -r8）
  - 读内存命令：`-c SWD -r8 <addr> <n>`（烧录后物理验证用）
  - 烧录串联参数一次完成：`-c SWD -P fw.bin 0x08000000 -V -Rst`
**固化**：`stm32_adapter.py StlinkCliTool`（按输出判断成败）+ 本条。

## L14 — 中文 Windows 下子进程输出是 GBK，Python 3.14 会崩读线程（2026-08-23）

**症状**：subprocess(text=True) 的 reader 线程 UnicodeDecodeError，
工具输出全丢——烧录"成功"实际是瞎猜的。
**根因**：Python 3.14 默认 UTF-8 模式（PEP 686），而 ST-LINK_CLI 等
本地工具输出 GBK 中文（进度条字符 0xB1）。
**规则**：所有 subprocess 调用必须显式 `encoding="utf-8",
errors="replace"`。
**固化**：`stm32_adapter.py`（两处）、`esp32_adapter.py`（一处）。

## L15 — armcc v5 裸机构建要点（2026-08-23）

  - 向量表用 `__attribute__((section("RESET")))` + armlink
    `--first=main.o(RESET)` 放到 0x08000000
  - 复位入口直接指向 C 库 `__main`（自带 .data 拷贝/.bss 清零），
    不要自己写 Reset_Handler
  - `--entry=__main`（数值入口会因指向数据报 L6204E）
  - `--ro-base=0x08000000 --rw-base=0x20000000`
  - bash 里 armlink 参数含括号必须引号：`"--first=main.o(RESET)"`
  - 混合声明要 `--c99`
**固化**：`firmware/stm32/f103_motor_demo/build.sh` 注释 + 本条。

## L16 — MCP Python SDK：锁 1.x，2.0 重构了 API（2026-08-23）

**症状**：pip 默认装 mcp 2.0.0，`mcp.server.fastmcp` 不存在（模块树
大改：mcpserver/ apps/ runner/ …），FastMCP 新位置未定。
**策略**：锁 `mcp>=1.20,<2`（全生态当前文档对应的稳定线），等 2.0
文档和生态跟上再迁移。
**固化**：pyproject `[agent]` extra + 本条。

## L17 — "解析不到串口"第一大原因：设备没插（2026-08-23）

**场景**：换板子时拔了 ESP32，monitor 报 "cannot resolve serial
port"。hint 只说"查占用/跑 discover"，漏了最常见原因。
**规则**：端口解析失败的 hint 必须先问"设备插了吗"，再谈占用。
**固化**：`serial_adapter.py` E_PORT_OPEN_FAILED 的 hint 文案。

## L18 — MCP 客户端是 anyio 异步 API（2026-08-23）

stdio_client/ClientSession 是 async context manager；测试用 anyio
自带的 pytest 插件（anyio 是 mcp 依赖，零额外安装）+ `anyio_backend`
fixture，不用引 pytest-asyncio。
**固化**：`tests/test_mcp_server.py` 模块注释。

## L19 — 构建 artifact 相对路径必须以 working_dir 为基准解析（2026-08-23）

**症状**：构建命令 exit 0，却报 "artifact not found"——命令在
working_dir 里产出了文件，检查却在 harness 进程的 cwd 找。
**固化**：`builder.py build_flow`（相对 artifact 拼接 working_dir）。

## L20 — 设备锁两个语义：跨进程互斥 + 同进程可重入（2026-08-23）

**踩坑过程**：锁上线即翻车——MCP server 每次工具调用重建
Registry/Adapter，同进程的第二次 connect 被自己的锁挡死
（E_DEVICE_BUSY，占用者是自己）；且 TestRunner 跑完不 disconnect，
锁挂到进程退出。
**规则**：
  1. 锁文件 pid == 当前进程 -> 视为"已是我的"（可重入）
  2. 长流程（run_test）结束必须显式 disconnect 释放设备
  3. 互斥测试要用真实子进程模拟"别的进程"，同进程两把锁不再互斥
**固化**：`session.py acquire`（重入分支）、`runner.py run`（收尾
disconnect）、`tests/test_builder_and_locks.py`（子进程互斥测试）。

## L21 — regex 遥测提取值是字符串，进换算前必须转数值（2026-08-23）

**症状**：regex 格式设备一读遥测就 TypeError（str * int）。
**根因**：re.groupdict 返回字符串；line_json 路径 JSON 解析天然
分型，所以这条路径此前从未被踩过。
**规则**：两种解析路径产出的类型契约必须一致（都是数值），
不一致早晚会炸在下游。
**固化**：`serial_adapter.py _ingest`（regex 分支 int/float 转换）+
`tests/test_vendor_and_regex.py`。

## L23 — 笔记本 4GB 显存（WDDM）跑不了 8B：nvidia-smi 的 free 是假的（2026-08-23）

**数据**：RTX 3050 Laptop 4GB；`-ngl 99` 需 4.46GB、`-ngl 22` 需
2.8GB、`-ngl 10` 需 1.5GB——全部 OOM，而 nvidia-smi 报 free 3962MB。
**根因**：WDDM 模式下显示栈 + 桌面应用（实测 ChatGPT 桌面版在列）
预留显存，CUDA 可分配量远小于标称 free。
**规则**：小显存笔记本别硬试部分卸载的"中间值"，直接 `-ngl 0`
纯 CPU 或换机器；`agent-serve` 默认值已按此设 0。
**固化**：`llm.py LLMConfig.llama_ngl` 注释 + `docs/模型接入.md`。

## L24 — 纯 CPU 跑 8B 前先看"空闲 RAM"而不是"总 RAM"（2026-08-23）

**症状**：CPU 模式加载 5GB 模型时整个 shell 开始 `fork: Resource
temporarily unavailable`，llama-server 被杀（exit 139）。
**根因**：16GB 总内存当时仅剩 4.3GB 空闲（开发环境本身占着大头），
模型 + KV 需要 ~6GB。
**规则**：跑本地大模型前 `tasklist`/任务管理器确认空闲 ≥ 模型体积
+1.5GB；不够就关应用、换 4B 模型或走 API。
**固化**：`docs/模型接入.md` 实测记录表 + 本条。

## L25 — 网络矩阵（2026-08-23 本机实测）

可用：micropython.org、st.com、modelscope.cn(API)；
极慢：GitHub 大文件（60KB/s）；
不通：huggingface.co 直连、hf-mirror.com（连接拒绝）、
ModelScope 无 Qwen 官方 GGUF。
**固化**：本条（下载任何模型/工具链前先查这张表）。
