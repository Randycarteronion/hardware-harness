#!/usr/bin/env bash
# 蓝pill（STM32F103C8）固件构建脚本 —— Keil armcc v5，零库零依赖。
#
# 【经验固化 L15：armcc v5 裸机构建的五个要点】（2026-08-23 实战趟出）
#   1. 向量表放 0x08000000：C 侧 __attribute__((section("RESET")))，
#      链接侧 --first=main.o(RESET)。bash 里括号必须引号包住。
#   2. 复位入口指向 C 库 __main（自动做 .data 拷贝/.bss 清零再调
#      main），不要自己写 Reset_Handler —— 忘了定义会 L6218E，
#      自己写初始化循环容易漏 .data。
#   3. --entry 用符号 __main。用数值地址会 L6204E（指向数据段
#      "does not point to an instruction"）。
#   4. 必须 --ro-base + --rw-base 都给，否则 RW 段落进 flash，
#      运行期写它直接 HardFault。
#   5. 混合声明/语句要 --c99（armcc 默认 C90 会报错）。
# 改动这些参数前先跑通一次基线，逐项改 —— 链接器报错都不直观。
#
# 产出 fw.bin —— harness flash bluepill_f103 fw.bin --confirm confirm:flash
set -e
cd "$(dirname "$0")"

ARMCC="C:/Keil_v5/ARM/ARMCC/bin"

# --c99：允许声明和语句混排；-O1：遥测时序不敏感，可读性优先
"$ARMCC/armcc.exe" --cpu=Cortex-M3 --thumb --c99 -O1 -c main.c -o main.o

# --ro-base 代码基址(flash 0x08000000) / --rw-base 数据基址(SRAM 0x20000000)
# --first   把 RESET 段钉在镜像最前（=向量表在 0x08000000）
# --entry   __main：C 库 scatterload 入口（见 main.c 向量表注释）
"$ARMCC/armlink.exe" --cpu=Cortex-M3 --ro-base=0x08000000 --rw-base=0x20000000 \
    "--entry=__main" "--first=main.o(RESET)" --map -o fw.axf main.o

# axf -> 原始 bin（ST-LINK_CLI -P 吃 bin，偏移在烧录命令里给）
"$ARMCC/fromelf.exe" --bin -o fw.bin fw.axf
ls -la fw.bin
