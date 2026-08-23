# Backlog — 未实现 / 被阻塞项清单

> 维护规则：每项写清【状态/阻塞原因/解锁条件/替代方案】。
> 完成的项移到底部"已归档"区，保留完成日期，删细节。
> 与 docs/lessons.md 的分工：lessons 记"踩过的坑"，backlog 记"还没做的"。

## 🔴 被硬件/物料阻塞

### B1 — STM32 蓝pill 遥测通道（run-test 全链路最后一公里）

- **状态**：SWD 侧（烧录/复位/身份核验/回读验证）全部打通 ✅；
  串口遥测 ❌
- **阻塞原因**：手头没有 USB-TTL 模块，ST-Link 为纯 SWD 形态（无 VCP）
- **解锁条件**：一个 CH340/CP2102 USB-TTL 模块，接线见
  [stm32-telemetry-blocked.md](stm32-telemetry-blocked.md)（三根线，
  接完即跑，无需改任何代码——设备 yaml 已就绪）
- **替代方案**（按工作量排序）：
  1. USB-TTL 模块（首选，淘宝几块钱）
  2. 蓝pill 板载 USB 启用 USB-CDC（固件加 USB 栈，~1天）
  3. SWD 上跑 RTT 遥测（需 OpenOCD 或新探针，当前老固件 pyocd 拒连）

## 🟡 功能缺口（架构已承诺，代码未实现）

### B6 — power_cycle 动作
只有 Mock 支持。真实设备需要继电器/智能开关硬件，Phase 3+。

### B8 — regex 遥测的真设备实测
解析器已实现并修好类型 bug（regex 提取值是字符串，须转数值），
假串口单测已覆盖；差一台只会吐非 JSON 文本的真设备做终验。

## 🟢 Phase 2+ 规划内（未启动，不算欠账）

- **MCP server**（Phase 2 灵魂）：把 Tool API 暴露给 DSH / Claude
  Code / Codex，agent 侧零配置使用硬件
- 错误 hint 体系化打磨（Phase 2）
- CAN / GPIO / ADC / SPI / I2C 能力（Phase 3）
- Hardware CI：git hook → build → flash → run-test → 报告（Phase 3）
- 常驻 daemon + 多会话遥测流共享（Phase 3）
- GUI（明确延后，非核心）
- 波形 / 二进制 artifact（现在只有 jsonl 遥测流）

## 已知小毛刺（顺手修）

- discover 会列出蓝牙虚拟 COM 口（无 VID）——展示层可标注过滤
- ESP32 遥测期间 REPL 若被 mpremote 打断需手动复位恢复
- Arduino ESP32 工具链下载中断在 1.88%（国内 GitHub 限速；夜晚挂机
  或换 dl.espressif.cn 镜像重试；MicroPython 路线不受影响）

## 已归档

- 2026-08-23 — **B7 MicroPython 部署**：core.firmware spec 升 1.1.0 加
  install 动作（safe）；Esp32Adapter 经 mpremote cp+reset 部署，
  内置 L7 首连复位重试；CLI `harness deploy` + MCP `hardware_deploy`。
- 2026-08-23 — **B4 vendor.* spec 加载**：Registry.load_capability_spec
  （schema 校验 / 禁覆盖 core / 同名取高版本），约定目录
  <devices>/capabilities/ 自动扫描。
- 2026-08-23 — **B5 verify（部分）**：ESP32 路径 read_flash 比对 +
  首差异字节定位（rollback 仍远期）；STM32 侧 flash 后向量回读检查
  之前已内置。
- 2026-08-23 — **B2 core.firmware.build**：`builder.py` + binding.build
  声明（command/artifact/working_dir）+ CLI `harness build` + MCP
  `hardware_build`。真机验证：蓝pill 真 armcc 编译通过。产出校验含
  "退出码 0 但产物缺失"拦截。
- 2026-08-23 — **B3 Session Manager**：`session.py` 跨进程文件锁
  （O_EXCL 原子抢锁 / 陈锁自动回收 / 同 pid 可重入），接入
  SerialAdapter connect/disconnect；TestRunner 结束自动释放设备。
- 2026-08-23 — ESP32-S3 全链路验收 PASS（原 B 类阻塞：无固件通道，
  MicroPython 解决）
- 2026-08-23 — STM32 SWD 烧录链路（原阻塞：老固件 ST-Link，stlink_cli
  双工具路径解决）
