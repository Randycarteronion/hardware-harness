# B1 解锁指南：蓝pill 遥测通道（拿到 USB-TTL 模块后照此接）

## 接线（三根线，断电接）

```
USB-TTL 模块          蓝pill
─────────────        ─────────────
RX        ──────────  PA9  (USART1_TX)
TX        ──────────  PA10 (USART1_RX)
GND       ──────────  GND
```

注意：
- 模块和板子**必须共地**（GND 接 GND），否则乱码
- TX/RX 交叉接（模块 RX 接板 TX）
- 3.3V/5V 跳线选 3.3V（蓝pill USART 是 3.3V 电平，5V 模块若带电平
  选择务必拨对；纯 5V TTL 模块的 TX 接 PA10 前最好串 1kΩ 电阻）

## 接好后（无需改任何代码）

```bash
cd hardware-harness
harness discover                      # 应看到新 COM 口（有 vid/pid 的那个）
# 把序列号填进 examples/devices/bluepill_f103.yaml 的 match.serial_number
harness monitor bluepill_f103         # 应看到 {"rpm":0,...} 遥测行
harness run-test examples/tests/stm32_motor_start.yaml   # 全链路验收
```

测试剧本 `stm32_motor_start.yaml` 还不存在，到时候从
`esp32_motor_start.yaml` 复制、device 改成 `bluepill_f103` 即可。

## 为什么 ST-Link 不能顺便把串口也干了

这个山寨 ST-Link 是纯 SWD 仿真器形态（USB PID 3748，无 CDC 接口）。
NUCLEO 板的 ST-Link 才带 VCP（一根 USB 线全搞定）——如果以后换
NUCLEO，连 USB-TTL 都省了。
