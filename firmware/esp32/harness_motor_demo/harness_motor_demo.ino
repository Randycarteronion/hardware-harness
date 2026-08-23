/* harness_motor_demo.ino —— Hardware Harness 真板验收固件（ESP32-S3）。
 *
 * 【这是干啥的】
 * 一个"虚拟电机"演示：串口收 ASCII 行命令（start/stop），回 "OK ..."；
 * 每 100ms 输出一行 JSON 遥测 {"rpm":x,"temp_c":y}。
 * 物理模型与上位机测试完全对齐：rpm 一阶惯性爬向 1243（tau=500ms），
 * 温度按转速比例从 25.0 爬到 37.2 degC（发定点数 x10，上位机 scale=0.1）。
 *
 * 【为什么虚拟】
 * 验收的是"链路"：烧录→复位→命令→遥测→断言，不是电机本身。
 * 板上没有电机也能全流程跑通，结论可迁移到任何真实外设。
 *
 * 编译：arduino-cli compile --fqbn esp32:esp32:esp32s3 <dir>
 */

#include <Arduino.h>
#include "harness_firmware_stub.h"

static const float TARGET_RPM = 1243.0f;   // 稳态转速
static const float TAU_MS     = 500.0f;    // 一阶惯性时间常数
static const float TEMP_MIN   = 25.0f;     // 静止温度 degC
static const float TEMP_MAX   = 37.2f;     // 满速温度 degC

static bool          running = false;
static unsigned long t0      = 0;
static float         rpm0    = 0.0f;       // 停机时刻冻结的转速
static String        cmdbuf;               // 行命令缓冲

void setup() {
  Serial.begin(115200);
  delay(200);                              // 等 USB 转串口就绪
  HARNESS_REPLY(("READY esp32s3 harness motor demo"));
}

// 当前转速：一阶惯性 rpm(t) = target * (1 - e^(-t/tau))
static float cur_rpm() {
  if (!running) return rpm0;
  float t = (float)(millis() - t0);
  return TARGET_RPM * (1.0f - expf(-t / TAU_MS));
}

void loop() {
  // ---- 非阻塞收行命令（阻塞读会拖垮遥测周期）----
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdbuf == "start") {
        running = true;
        t0 = millis();
        HARNESS_REPLY(("OK motor started"));
      } else if (cmdbuf == "stop") {
        rpm0 = cur_rpm();
        running = false;
        HARNESS_REPLY(("OK motor stopped"));
      } else if (cmdbuf.length() > 0) {
        HARNESS_REPLY(("ERR unknown command"));
      }
      cmdbuf = "";
    } else {
      cmdbuf += c;
    }
  }

  // ---- 100ms 周期遥测：rpm 原始值 + 温度定点 x10 ----
  static unsigned long last = 0;
  if (millis() - last >= 100) {
    last = millis();
    float rpm  = cur_rpm();
    float frac = constrain(rpm / TARGET_RPM, 0.0f, 1.0f);
    unsigned temp_raw = (unsigned)((TEMP_MIN + (TEMP_MAX - TEMP_MIN) * frac) * 10.0f);
    HARNESS_TELEMETRY_U16("rpm", (unsigned)rpm, "temp_c", temp_raw);
  }
}
