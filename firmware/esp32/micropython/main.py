# harness_motor_demo（MicroPython 版）—— Hardware Harness 真板验收固件。
#
# 【这是干啥的】
# 虚拟电机：串口收 ASCII 行命令（start/stop，回 "OK ..."），
# 每 100ms 输出一行 JSON 遥测 {"rpm":x,"temp_c":y}。
# 物理模型与上位机断言对齐：rpm 一阶惯性爬向 1243（tau=0.5s），
# 温度按转速从 25.0 爬到 37.2 degC（定点 x10，上位机 scale=0.1）。
#
# 【实现说明】
# 两个线程：cmd_loop 阻塞 readline 收命令，tel_loop 周期打印遥测。
# MicroPython 的 print 加锁防两线程输出串行错乱。
# main.py 不退出（死循环）→ REPL 永远不接管串口，无提示符噪声。
#
# 部署：mpremote cp main.py :main.py   （复位后自动运行）

import sys
import time
import math
import _thread

TARGET = 1243.0   # 稳态转速 rpm
TAU = 0.5         # 一阶惯性时间常数 s
TEMP_MIN = 25.0   # 静止温度 degC
TEMP_MAX = 37.2   # 满速温度 degC

state = {"run": False, "t0": 0, "rpm0": 0.0}
lock = _thread.allocate_lock()


def cur_rpm():
    """rpm(t) = TARGET * (1 - e^(-t/tau))，停机后冻结在 rpm0。"""
    if not state["run"]:
        return state["rpm0"]
    t = (time.ticks_ms() - state["t0"]) / 1000.0
    return TARGET * (1.0 - math.exp(-t / TAU))


def emit(line):
    """带锁打印：两个线程的输出互不撕裂。
    注意 MicroPython 的 sys.stdout 没有 flush()，用 print 即可
    （ESP32 端口串口写入本身无用户态缓冲）。"""
    with lock:
        print(line)


def cmd_loop():
    """命令线程：阻塞 readline（REPL 未接管时 stdin 就是 UART0）。"""
    while True:
        line = sys.stdin.readline()
        if not line:
            time.sleep(0.05)
            continue
        s = line.strip()
        if s == "start":
            state["run"] = True
            state["t0"] = time.ticks_ms()
            emit("OK motor started")
        elif s == "stop":
            state["rpm0"] = cur_rpm()
            state["run"] = False
            emit("OK motor stopped")
        elif s:
            emit("ERR unknown command")


emit("READY esp32s3 harness motor demo (micropython)")
_thread.start_new_thread(cmd_loop, ())

# 主线程：100ms 周期遥测
while True:
    rpm = cur_rpm()
    frac = min(rpm / TARGET, 1.0)
    temp_raw = int((TEMP_MIN + (TEMP_MAX - TEMP_MIN) * frac) * 10)
    emit('{"rpm":%d,"temp_c":%d}' % (int(rpm), temp_raw))
    time.sleep(0.1)
