/**
 * harness_firmware_stub.h —— 固件侧最小遥测桩（零依赖，头文件即用）。
 *
 * 【这是干啥的】
 * Hardware Harness 通过串口收"每行一个 JSON"的遥测（line_json 格式）。
 * 你的固件只要周期性调用下面的宏打印遥测、用 HARNESS_REPLY 回命令，
 * 就完成了接入 —— 设备 yaml 里对应声明 telemetry.format: line_json。
 *
 * 【怎么用】
 *   1. 把本文件拷进工程，#include 它
 *   2. 主循环里周期调用（建议 50~200ms 一次）：
 *
 *      HARNESS_TELEMETRY_U16("rpm", motor_rpm, "duty", pwm_duty);
 *      HARNESS_TELEMETRY_FLOAT("temp_c", temperature);
 *
 *      或一条混合多个字段：
 *      HARNESS_TELEMETRY_BEGIN();
 *      HARNESS_TELEMETRY_U16("rpm", motor_rpm);
 *      HARNESS_TELEMETRY_FLOAT("temp_c", temperature);
 *      HARNESS_TELEMETRY_END();
 *
 *   3. 命令处理里回复（Agent 的 expect 正则去匹配这个行）：
 *
 *      HARNESS_REPLY("OK motor started");
 *
 *   4. 设备 yaml 里声明字段和单位（scale 见下方浮点说明）：
 *
 *      telemetry:
 *        format: line_json
 *        fields:
 *          rpm:    {type: uint16, unit: rpm}
 *          temp_c: {type: float32, unit: degC}
 *
 * 【浮点说明】
 *   小 MCU 上 printf 常没有 %f（newlib-nano 默认关）。两个方案：
 *   a) 用 _U16/_I32 宏发定点整数，yaml 里用 scale 换算 —— 推荐：
 *      固件发 temp*10（如 372），yaml 声明 scale: 0.1，Agent 看到 37.2 degC
 *   b) 工程开了浮点 printf 就直接用 FLOAT 宏
 *
 * 【约束】
 *   - 输出走 stdout（重定向到 UART 的 printf），行必须以 \n 结尾
 *   - 不要在中断里调用（printf 非线程安全）
 *   - 字段名要和 yaml 的 telemetry.fields 键完全一致
 */
#ifndef HARNESS_FIRMWARE_STUB_H
#define HARNESS_FIRMWARE_STUB_H

/* 双目标：STM32（stdout 重定向到 UART 的 printf）和
 * Arduino/ESP32（printf 不走串口，必须用 Serial.printf）。
 * 在 Arduino 环境里编译时自动切换到 Serial 变体。 */
#ifdef ARDUINO
#include <Arduino.h>
#define HARNESS_PRINTF(...)  Serial.printf(__VA_ARGS__)
#else
#include <stdio.h>
#define HARNESS_PRINTF(...)  printf(__VA_ARGS__)
#endif

/* 发一行完整遥测（2 个整数字段的最常用形态）。
 * 产出形如：{"rpm":1234,"duty":560}\n */
#define HARNESS_TELEMETRY_U16(k1, v1, k2, v2) \
    do { HARNESS_PRINTF("{\"%s\":%u,\"%s\":%u}\n", k1, (unsigned)(v1), k2, (unsigned)(v2)); } while (0)

/* 多字段形态：开头/中间/结尾 三段拼一行 */
#define HARNESS_TELEMETRY_BEGIN()        do { HARNESS_PRINTF("{"); } while (0)
#define HARNESS_TELEMETRY_U16(k, v)      do { HARNESS_PRINTF("\"%s\":%u,", k, (unsigned)(v)); } while (0)
#define HARNESS_TELEMETRY_I32(k, v)      do { HARNESS_PRINTF("\"%s\":%ld,", k, (long)(v)); } while (0)
#define HARNESS_TELEMETRY_FLOAT(k, v)    do { HARNESS_PRINTF("\"%s\":%.3f,", k, (double)(v)); } while (0)
/* 结尾宏会吃掉上一个字段多打的逗号：退格一位再收口 */
#define HARNESS_TELEMETRY_END()          do { HARNESS_PRINTF("\b}\n"); } while (0)

/* 回复一条命令结果（一行纯文本，Agent 的 expect 正则匹配它） */
#ifdef ARDUINO
#define HARNESS_REPLY(msg)               do { Serial.printf msg; Serial.printf("\n"); } while (0)
#else
#define HARNESS_REPLY(msg)               do { printf msg; printf("\n"); } while (0)
#endif

/* 用法示例（伪代码）：
 *
 *   if (uart_line_equals("start")) {
 *       motor_start();
 *       HARNESS_REPLY(("OK motor started"));
 *   }
 *   if (tick_100ms()) {
 *       HARNESS_TELEMETRY_U16("rpm", motor.rpm, "temp_c", (uint16_t)(motor.temp * 10));
 *   }
 */

#endif /* HARNESS_FIRMWARE_STUB_H */
