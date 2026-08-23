# Hardware Harness

A hardware execution runtime for AI coding agents. Agents already have
Filesystem / Shell / Git; this adds a **Hardware Workspace**:
`build → flash → run → observe → test → agent feedback`, all structured.

## Architecture

Generic core + capability model + adapter plugins. No universal protocol,
no device types — only Device / Capability / Transport / Action.

```
Agent (DSH / Claude Code / Codex / CI)
  └── harness CLI (MCP server later)      src/harness/cli.py
        └── Device Registry               src/harness/registry.py
        └── Test Runner (7 primitives)    src/harness/runner.py
              └── Adapters (anticorruption)
                    ├── MockAdapter       adapters.py        无硬件全链路演示
                    ├── SerialAdapter     serial_adapter.py  pyserial 命令+遥测
                    └── Stm32Adapter      stm32_adapter.py   pyocd 烧录+复位（组合串口）
```

- Capability contracts: `src/harness/capabilities.py` (`core.*` builtin; `vendor.*` must ship a spec)
- Schemas: `schemas/{capability,device,test,report}.schema.json`
- Devices declare capabilities + bindings; adapters translate per device
- Destructive actions (flash / rollback / power_cycle) require a confirmation token
- Asserts carry `within` (time window) and `settle` (stability) — hardware is inertial
- Firmware side: drop in `firmware/harness_firmware_stub.h` and print JSON lines

## Install

```bash
pip install -e .                     # core + serial
pip install -e '.[stm32]'            # + pyocd（烧录/ST-Link）
```

## Quick start (mock, no hardware)

```bash
harness list-devices
harness run-test examples/tests/motor_start.yaml
```

## Real hardware (Phase 1)

```bash
harness discover                                  # 列出串口 + ST-Link 探针
# 把序列号填进 examples/devices/stm32_nucleo.yaml（match + probe_serial）
harness build bluepill_f103                       # 编译设备声明的固件（真 armcc）
harness monitor stm32_nucleo --duration 5s        # 看换算后的遥测
harness flash stm32_nucleo fw.bin --confirm confirm:flash
harness run-test examples/tests/motor_start.yaml  # 剧本 device 改成你的设备 id
```

Flash 三道闸：CLI confirmation token → probe_serial 精确选探针 →
identity_check 核验，任一不过即拒绝烧录。

Firmware integration: copy `firmware/harness_firmware_stub.h` into the
project, call `HARNESS_TELEMETRY_U16("rpm", rpm, "temp_c", t*10)` in the
main loop and `HARNESS_REPLY(("OK", ...))` on commands. Declare matching
fields (with `scale`) in the device YAML — done.

## AI Agent integration

Two complementary routes, one tool implementation (`TOOL_FUNCS`):

1. **Built-in agent** (`harness agent "goal"`) — LLM tool-calling loop
   driving the 8 hardware tools. Providers (both OpenAI-compatible):
   local llama.cpp (`harness agent-serve` launches llama-server with
   `--jinja`) or DeepSeek API (`HARNESS_AGENT_PROVIDER=deepseek` +
   `DEEPSEEK_API_KEY`). Destructive ops pause for human y/n approval;
   full transcript archived per run. Config: `agent.toml` /
   `docs/模型接入.md` (includes this machine's VRAM/RAM reality).
2. **MCP server** (`harness-mcp`, 8 tools, stdio) — DSH / Claude Code /
   Codex as agent hosts:

```json
{"mcpServers": {"hardware": {"command": "harness-mcp", "args": []}}}
```

`hardware_flash` requires `confirm_token='confirm:flash'`; the agent
must ask the human for it. `hardware_run_test` reports PASS plus
`detail.measured` + unit so the agent can self-correct.

Device locks: serial connect acquires a cross-process file lock
(`~/.hardware-harness/locks/`), stale locks from crashed processes are
reclaimed automatically; concurrent agents get `E_DEVICE_BUSY` with the
holder's identity.

Device directory resolution: env `HARNESS_DEVICES_DIR` overrides the
default `examples/devices` (anchored to the repo, CWD-independent).

## Status

- Phase 0 ✅ mock full loop (schemas, registry, runner, report, artifacts)
- Phase 1 ✅ **real-board validated on ESP32-S3 N16R8** (flash w/ MAC identity
  check → reset → command → telemetry w/ scale → assert → report) and
  **STM32F103C8 bluepill SWD path** (stlink_cli flash w/ device_id identity
  check + vector-table sanity readback, pyocd path for newer probes).
  Telemetry over serial validated on ESP32; STM32 telemetry awaits a
  USB-TTL module (see docs/backlog/).
- Phase 2 ✅ MCP server (`harness-mcp`, 8 tools incl. build/deploy, stdio)
  — end-to-end tested with a real MCP client; destructive gate preserved
  at the agent boundary. CLI and MCP share `runtime.py`.
- Also done: firmware build integration (binding.build, real armcc
  verified), cross-process device locks w/ crash recovery, vendor.*
  capability spec loading, MicroPython script deploy (`harness deploy`)
- Phase 3 ⬜ CAN/GPIO/ADC capabilities, hardware CI, daemon
- Runbook (验收步骤/接线图/FAQ): docs/后续操作指南.md
- Lessons register (25 entries): docs/lessons.md
- Backlog: docs/backlog/
