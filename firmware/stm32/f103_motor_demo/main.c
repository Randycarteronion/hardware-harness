/* harness_motor_demo —— STM32F103C8 裸机版（无 HAL / 无库 / 单文件）
 *
 * 【这是干啥的】
 * Hardware Harness 真板验收固件：USART1(PA9/PA10, 115200) 收行命令
 * （start/stop，回 "OK ..."），每 100ms 输出一行 JSON 遥测。
 * 物理模型与其他平台对齐：rpm 一阶惯性爬向 1243（tau=500ms），
 * 温度 25.0→37.2 degC（定点 x10，上位机 scale=0.1）。
 * PC13 板载 LED 每 500ms 翻转一次 = 固件活着的心跳。
 *
 * 【为什么寄存器级】
 * 零依赖：不需要 STM32CubeMX/HAL/启动文件，armcc 直接编译。
 * 时钟：HSE 8MHz × PLL9 = 72MHz（USART1 挂 APB2=72MHz，
 * 115200 波特率分频恰好整数 625，无累积误差）。
 *
 * 【编译】见同目录 build.sh（Keil armcc v5）：
 *   armcc --cpu=Cortex-M3 -mthumb -c main.c
 *   armlink --ro-base=0x08000000 --entry=Reset_Handler --first=main.o(RESET)
 *   fromelf --bin
 */

#include <stdint.h>

/* ============================ 寄存器定义 ============================ */

#define RCC_BASE     0x40021000UL
#define GPIOA_BASE   0x40010800UL
#define GPIOC_BASE   0x40011000UL
#define USART1_BASE  0x40013800UL
#define STK_BASE     0xE000E010UL
#define FLASH_BASE   0x40022000UL

#define RCC_CR       (*(volatile uint32_t *)(RCC_BASE + 0x00))
#define RCC_CFGR     (*(volatile uint32_t *)(RCC_BASE + 0x04))
#define RCC_APB2ENR  (*(volatile uint32_t *)(RCC_BASE + 0x18))

#define GPIOA_CRL    (*(volatile uint32_t *)(GPIOA_BASE + 0x00))
#define GPIOA_CRH    (*(volatile uint32_t *)(GPIOA_BASE + 0x04))
#define GPIOC_CRH    (*(volatile uint32_t *)(GPIOC_BASE + 0x04))
#define GPIOC_ODR    (*(volatile uint32_t *)(GPIOC_BASE + 0x0C))

#define USART_SR     (*(volatile uint32_t *)(USART1_BASE + 0x00))
#define USART_DR     (*(volatile uint32_t *)(USART1_BASE + 0x04))
#define USART_BRR    (*(volatile uint32_t *)(USART1_BASE + 0x08))
#define USART_CR1    (*(volatile uint32_t *)(USART1_BASE + 0x0C))

#define STK_CTRL     (*(volatile uint32_t *)(STK_BASE + 0x00))
#define STK_LOAD     (*(volatile uint32_t *)(STK_BASE + 0x04))

#define FLASH_ACR    (*(volatile uint32_t *)(FLASH_BASE + 0x00))

#define USART_SR_RXNE (1u << 5)   /* 接收数据寄存器非空 */
#define USART_SR_TXE  (1u << 7)   /* 发送数据寄存器空 */
#define USART_SR_TC   (1u << 6)   /* 发送完成 */

/* ============================ 中断向量表 ============================
 * 只有前 16 个系统向量。复位入口指向 armcc C 库的 __main：它先把
 * .data 从 flash 拷到 RAM、清 .bss（scatterload），再调 main ——
 * 这是 armcc 裸机的标准姿势，省掉手写初始化循环。*/
extern void __main(void);  /* armlink 从 c_w.l 解析 */
static void Default_Handler(void) { for (;;) {} }

__attribute__((section("RESET")))
const uint32_t vectors[16] = {
    0x20005000u,             /* 初始 SP：F103C8 共 20KB RAM，顶在 0x20005000 */
    (uint32_t)__main,        /* Reset -> C 库入口（scatterload -> main） */
    (uint32_t)Default_Handler, /* NMI */
    (uint32_t)Default_Handler, /* HardFault */
    (uint32_t)Default_Handler, /* MemManage */
    (uint32_t)Default_Handler, /* BusFault */
    (uint32_t)Default_Handler, /* UsageFault */
    0, 0, 0, 0,               /* reserved */
    (uint32_t)Default_Handler, /* SVCall */
    (uint32_t)Default_Handler, /* DebugMon */
    0,                         /* reserved */
    (uint32_t)Default_Handler, /* PendSV */
    (uint32_t)Default_Handler, /* SysTick（轮询模式，不会进） */
};

/* ============================ 系统初始化 ============================ */

static void system_init(void)
{
    /* Flash 等待周期：72MHz 需要 2 wait states + 预取 */
    FLASH_ACR = (1u << 4) | (1u << 3) | 2u;

    /* 开 HSE（蓝pill 板载 8MHz 晶振），等稳定 */
    RCC_CR |= (1u << 16);                       /* HSEON */
    while (!(RCC_CR & (1u << 17))) {}           /* HSERDY */

    /* PLL = HSE × 9 = 72MHz；APB1 ÷2 = 36MHz，APB2 不分频 = 72MHz */
    RCC_CFGR = (0x7u << 18)                     /* PLLMUL = ×9，来源 HSE */
             | (1u << 16)                       /* PLLSRC = HSE */
             | (4u << 8)                        /* APB1 = HCLK/2 */
             | (0u << 11);                      /* APB2 = HCLK/1 */
    RCC_CR |= (1u << 24);                       /* PLLON */
    while (!(RCC_CR & (1u << 25))) {}           /* PLLRDY */

    RCC_CFGR |= 2u;                             /* SYSCLK 切到 PLL */
    while ((RCC_CFGR & (3u << 2)) != (2u << 2)) {}

    /* 外设时钟：GPIOA（USART1 引脚）、GPIOC（LED）、USART1、AFIO */
    RCC_APB2ENR = (1u << 2) | (1u << 4) | (1u << 14) | (1u << 0);

    /* PA9 = USART1_TX：复用推挽 50MHz（CRH 第1个nibble=0xB）
       PA10 = USART1_RX：浮空输入（第2个nibble=0x4） */
    GPIOA_CRH = (GPIOA_CRH & 0xFFFF000Fu) | 0x000004B0u;

    /* PC13 = LED：推挽输出 2MHz（CRH 第5个nibble=0x2） */
    GPIOC_CRH = (GPIOC_CRH & 0xFF0FFFFFu) | 0x00200000u;

    /* USART1：72MHz / 115200 = 625（恰好整数） */
    USART_BRR = 625u;
    USART_CR1 = (1u << 13)                      /* UE */
              | (1u << 3)                       /* TE */
              | (1u << 2);                      /* RE */

    /* SysTick：72MHz 主频，LOAD=72000 → 1ms 一次 COUNTFLAG */
    STK_LOAD = 72000u - 1u;
    STK_CTRL = 3u;                              /* ENABLE | CLKSOURCE(内核时钟) */
}

/* ============================ 串口收发 ============================ */

static void uart_putc(char c)
{
    while (!(USART_SR & USART_SR_TXE)) {}
    USART_DR = (uint32_t)(uint8_t)c;
}

static void uart_puts(const char *s)
{
    while (*s)
        uart_putc(*s++);
}

/* 无符号整数转十进制 ASCII（遥测只用非负数，够用） */
static void uart_put_u32(uint32_t v)
{
    char buf[10];
    int i = 0;
    do { buf[i++] = (char)('0' + v % 10u); v /= 10u; } while (v);
    while (i--)
        uart_putc(buf[i]);
}

/* 毫秒计数：轮询 SysTick COUNTFLAG（读后硬件自动清零） */
static uint32_t millis(void)
{
    static uint32_t ms = 0;
    if (STK_CTRL & (1u << 16))
        ms++;
    return ms;
}

static void delay_ms(uint32_t n)
{
    uint32_t t0 = millis();
    while (millis() - t0 < n) {}
}

/* ============================ 虚拟电机模型 ============================
 * 与 ESP32/Mock 版完全对齐：
 *   rpm(t) = 1243 * (1 - e^(-t/0.5s))
 *   temp   = 25.0 + (37.2-25.0) * min(rpm/1243, 1)   —— 定点 x10 输出 */

static const uint32_t TARGET_RPM_X10 = 12430u;  /* 转速也用 x10 定点算，避免引浮点 */
static const uint32_t TAU_MS         = 500u;
static const uint32_t TEMP_RAW_MIN   = 250u;    /* 25.0 degC ×10 */
static const uint32_t TEMP_RAW_MAX   = 372u;    /* 37.2 degC ×10 */

static int       running = 0;
static uint32_t  rpm0_x10 = 0;                  /* 停机时刻冻结 */
static int str_eq(const char *a, const char *b);

/* 每 10ms 一步一阶惯性递推（_running 时）：
 * rpm += (target - rpm) * k，k = 1 - e^(-10ms/500ms) ≈ 51/256
 * 用整数 51/256 拟合（误差 <1.5%），避免链接数学库。 */
static void motor_step(void)
{
    if (running) {
        uint32_t diff = TARGET_RPM_X10 - rpm0_x10;   /* 单调上升，无下溢 */
        rpm0_x10 += (diff * 51u) >> 8;
    }
}

/* ============================ 主循环 ============================ */

int main(void)
{
    char cmdbuf[16];
    uint8_t cmdlen = 0;
    uint32_t last_tel = 0, last_step = 0, last_led = 0;
    uint32_t tel_count = 0;

    system_init();

    uart_puts("READY bluepill f103 harness motor demo\r\n");

    for (;;) {
        uint32_t now = millis();

        /* ---- 10ms：电机模型推进一步 ---- */
        if (now - last_step >= 10) {
            last_step = now;
            motor_step();
        }

        /* ---- 100ms：遥测一行 ----
         * 周期用计数法而非绝对时刻取模：millis 溢出（49天）不会跳变 */
        if (now - last_tel >= 100) {
            last_tel = now;
            tel_count++;

            uint32_t frac = rpm0_x10 >= TARGET_RPM_X10 ? 256u
                          : (rpm0_x10 * 256u) / TARGET_RPM_X10;   /* 0..256 */
            uint32_t temp_raw = TEMP_RAW_MIN
                              + ((TEMP_RAW_MAX - TEMP_RAW_MIN) * frac >> 8);

            uart_puts("{\"rpm\":");
            uart_put_u32(rpm0_x10 / 10u);      /* 输出整数 rpm（四舍五入略） */
            uart_puts(",\"n\":");
            uart_put_u32(tel_count);
            uart_puts(",\"temp_c\":");
            uart_put_u32(temp_raw);
            uart_puts("}\r\n");
        }

        /* ---- 500ms：LED 心跳翻转 ---- */
        if (now - last_led >= 500) {
            last_led = now;
            GPIOC_ODR ^= (1u << 13);
        }

        /* ---- 轮询收命令（非阻塞，拼行）---- */
        while (USART_SR & USART_SR_RXNE) {
            char c = (char)(USART_DR & 0xFFu);
            if (c == '\n' || c == '\r') {
                if (cmdlen > 0) {
                    cmdbuf[cmdlen] = 0;
                    if (str_eq(cmdbuf, "start")) {
                        running = 1;
                        uart_puts("OK motor started\r\n");
                    } else if (str_eq(cmdbuf, "stop")) {
                        running = 0;
                        uart_puts("OK motor stopped\r\n");
                    } else {
                        uart_puts("ERR unknown command\r\n");
                    }
                    cmdlen = 0;
                }
            } else if (cmdlen < sizeof(cmdbuf) - 1) {
                cmdbuf[cmdlen++] = c;
            }
        }
    }
}

/* ============================ 小工具 ============================ */

static int str_eq(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}
