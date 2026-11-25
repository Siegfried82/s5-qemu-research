# S5 SPI3 寄存器端点与 RTC 核验

最新局部显示实验的 SPI6 证据见本页末尾；它通过原厂 OS API 暂时延迟 task66，并非完成整机启动。

本目录从原厂指令和实际执行确定了此前 `0x7f043000` 接收等待的协议，并在有状态模型下越过该等待。首包是 **`04 39 ff`，读取寄存器 `0x0438`**。它不是 Flash JEDEC 指令；该端点至少包含 RTC 日历和保留状态寄存器。精确芯片型号、全部状态位含义和真实复位值仍未知。

没有连接真实相机，原厂固件指令没有修改。所有结论来自本地 Capstone 反汇编、QEMU 指令断点/单步或新构建的四核 QEMU；当前会话没有可用 REA 服务，这些产物不冒充 REA 输出。

## 已确认的接口

控制器布局与 PL022 SSP 相同：CR0=`+0`、CR1=`+4`、DR=`+8`、SR=`+0xc`、CPSR=`+0x10`；SR bit0/1/2/3 分别与发送 FIFO 空、可写、接收非空、接收满对应。原厂配置 CR0=`0x01c7`、CPSR=`2`，以 8 bit 传输。控制器表位于原厂 `0x08ab472c`，通道 3 指向 `0x7f043000`。

原厂 `0x06752fb0` 读函数、`0x06753036` 写函数构造独立三字节包：

| 操作 | TX 三字节 | 有效 RX |
| --- | --- | --- |
| 读 | `high(addr \| 1), low(addr \| 1), ff` | 第三个字节 |
| 写 | `high(addr & ~1), low(addr & ~1), data` | 原厂调用者忽略 |

CS 桥寄存器是 `0x7f081c0c`。原厂写 `0x10000001` 后传输，写 `0x10000000` 后撤销。这里只确认了编码寄存器的选中/撤销写值，**没有确认物理 CS 引脚是高有效还是低有效**。模型内部的逻辑极性不能反推物理引脚。

前两个 RX 字节被这些原厂调用者丢弃；模型返回 `ff`，这不是实际 MISO 电平测量。没有确认连续包或自动地址递增，因此模型不依靠这类行为。

## RTC 与状态字段

原厂 `0x064d060a` 依次读取以下寄存器，经过 `0x064d0982` 的 BCD 解码后构成年月日时分秒，年份再加 2000：

| 寄存器 | 含义 |
| --- | --- |
| `0x040c` / `0x040a` / `0x0408` | 年 / 月 / 日 |
| `0x0406` / `0x0404` / `0x0402` | 时 / 分 / 秒 |
| `0x0420..0x043e` 偶数地址 | 16 个应用保留状态字节 |

原厂初始化函数 `0x067531ac` 检查 `0x040e` bit0 与 `0x0410`，在需要初始化的分支自行写入默认日历。模型以显式冷启动假设提供状态；**没有把日期写入原厂返回缓冲区**。

两项仍未完全识别的状态：

- `0x041a`：bit0 与 GPIO `0x7f08005c` bit4 组合；bit1 在启动状态选择中使用；bit2 在日历/定时相关路径中测试并清除。不能仅据这些用途将各位命名为某个已确认的按钮或电源状态。
- `0x0488`：启动逻辑测试 bit2，其他原厂 RMW 函数操作 bit0、1、2、5。精确器件含义未知。

## 实际进展与证据

| 试验 | 结果 |
| --- | --- |
| [原首包捕获](results-first-entry/result.json) | 原始 TX=`0439ff`；仅 program 预载，历史对照 |
| [完整组件首包复核](results-full-components/result.json) | 补齐 postboot1–5 和 osddata 后首包完全一致 |
| [严格冷 RTC 初始模型](results-cold-rtc/result.json) | 原厂完成读0438、写0438、读0440；首次未知041a立即停止 |
| [两套 SPI transfer 完整覆盖](results-both-transfers/result.json) | 21 个真实事务完成；041a=0、0488=0均明确记录为额外实验假设；之后在70021004命令完成等待处停止 |
| [四核 QEMU 的 C 模型](results-smp-cold/result.json) | 原厂24次读、16次写，0个畸形帧；原厂自行写出2014-01-01日历；未知读仅041a、0488，共3次 |

前四项独立 Python 试验仍使用单核、合成 VFP/MMU 初始状态，并人工清除两个 peer 标志，只用于窄范围协议分析。最后一项调用新的 `menu-smp/boot.py`：真正执行 CPU0/1/2、共享一致 RAM，不人工清除 peer 标志；CPU3 暂停，没有 Linux peer。

最后一项已经越过 RTC 初始化。新的观察点是 `0x06554d8c` 从计时器 Value 寄存器读取 0，QEMU trace 反复出现 `0x7f020024`。原厂计时器表 `0x06ff2a50` 列出 13 对 `0x7f020000/+0x20` 至 `0x7f02c000/+0x20`；控制寄存器 `+8` 的 bits[3:2] 使用 1/16/256 预分频，微秒数乘27，支持27 MHz SP804兼容模型的判断。该后续设备由主机器实现工作负责。

当前没有到达真实菜单渲染。RTC 模型没有时钟流逝，也没有复现所有状态位副作用；C 模型将首次未知读取明确记为 `unsupported` 并返回实验值0，不能把这些读称作已验证硬件行为。

## 计时器 IRQ 的原厂映射

[`timer-irq-map.json`](timer-irq-map.json) 逐项提取原厂表，**没有使用等差外推**：通道索引表 `0x06ff2b20` → MMIO 表 `0x06ff2a50` 与逻辑 IRQ 表 `0x06ff2ab8` → 全局 GIC 映射表 `0x06ff1240`。原厂 `0x06554d00`、`0x0658bada`、`0x0651ea74` 的启用调用链确认了这些表的用途；最终编号直接用于 GICD_ISENABLER 的位索引，因此是含 SGI/PPI 的绝对中断 ID。

| 双计时器基址 | timer0 / timer1 的 GIC ID | 对应 SPI 输入 |
| --- | --- | --- |
| `0x7f020000` | 36 / 37 | 4 / 5 |
| `0x7f021000` | 38 / 39 | 6 / 7 |
| `0x7f022000` | 40 / 41 | 8 / 9 |
| `0x7f023000` | 42 / 43 | 10 / 11 |
| `0x7f024000` | 44 / 45 | 12 / 13 |
| `0x7f025000` | 46 / 47 | 14 / 15 |
| `0x7f026000` | 48 / 49 | 16 / 17 |
| `0x7f027000` | 50 / 51 | 18 / 19 |
| `0x7f028000` | 52 / 53 | 20 / 21 |
| `0x7f029000` | 54 / 55 | 22 / 23 |
| `0x7f02a000` | 56 / 57 | 24 / 25 |
| `0x7f02b000` | 58 / 59 | 26 / 27 |
| `0x7f02c000` | 60 / 61 | 28 / 29 |

每组 timer1 的寄存器位于基址 `+0x20`，两路中断独立。原版 QEMU SP804 仅输出合并 IRQ，不能把它只接在 timer0 的编号上就覆盖原厂两路映射。新增的 C 输出与实际中断触发验证由主机器构建工作负责；本表提取器本身不声明已验证中断送达。

## Loader 已开启的自由计数器

新增计时器器件后仍需恢复启动交接状态。原厂 `01_loader2.bin` 文件偏移 `0x190` 的 ARM `BLX` 调用 Thumb 偏移 `0x5e6`；此函数从原表取 `0x7f020020`，依次向 Control `0x7f020028` 写 `0`、`0x06`、`0x86`，没有写 Load。`02_loader3.bin` 偏移 `0x2062` 存在相同实现。

最终 `0x86` 对应32位、除16、自由向下计数、开启且不产生中断；27 MHz 输入得到1.6875 MHz。原厂 getter 直接读 `Value +4`，延时计算使用无符号 `旧值 - 当前值`，没有先取反。确切交接时已走过的 tick 数尚未恢复，重新建立计数器只恢复模式与单调时间差。

[`verify_loader_timer.py`](verify_loader_timer.py) 在新的 `s5-rtos` 上隔离执行了这段原厂初始化函数：[验证结果](results-loader-timer/result.json) 为 Control `0x20 → 0x86`，随后 Value `4294967278 → 4294914407`，约30 ms客体运行期间减少52,871 ticks。原厂函数字节未改；返回断点和其他核的暂停循环位于单独临时 RAM。完整原厂 loader 启动不在此试验范围。依据见 [`loader-timer-disassembly.txt`](loader-timer-disassembly.txt)。

## 文件与重跑

- [`original-disassembly.txt`](original-disassembly.txt)：协议、RTC、状态位与计时器的原厂指令及源摘要。
- [`summary.json`](summary.json)：机器可读结论与证据路径。
- [`extract_timer_irq_map.py`](extract_timer_irq_map.py)：核对固件摘要后重建26路精确 IRQ 映射。
- [`register_model.py`](register_model.py)：严格的探索性 RTC/保留寄存器模型，未知首次读立即报错。
- [`trace_serial.py`](trace_serial.py)：原厂两套 SPI transfer 的逐条执行探针，仅使用指令断点，不使用已发现有问题的 Z2/Z4 数据观察点。

从项目工作区执行，输出目录必须不存在：

```sh
python3 -B s5-emulator/serial/trace_serial.py --output s5-emulator/serial/recheck-entry
python3 -B s5-emulator/serial/trace_serial.py --profile cold-rtc --initial-register 0x041a=0 --initial-register 0x0488=0 --output s5-emulator/serial/recheck-transport
python3 -B s5-emulator/menu-smp/boot.py --seconds 5 --output s5-emulator/serial/recheck-smp
```


## 主任务的启动输入等待（后续 RAM 证据）

`menu-smp/results/loader-timer-boot2/ram.bin` 中，三颗 RTOS 核处于 idle，但主任务已经运行过10次，并非没有创建。TCB `0x08dfe890` 的 `uiTSK001` / entry `0x063c88af` 当前状态3，等待类型8、event ID125，event bits=0，AND mask=`0x7fffffff`，timeout=`0xffffffff`。保存的返回地址链对应 `0x063c8be2` 中的永久事件等待分支。可复查 [`startup-wait.json`](startup-wait.json)（含287个任务、完整主TCB与保存栈）及 [`startup-wait.disasm.txt`](startup-wait.disasm.txt)。`uiTSK018` / entry `0x0647e83f` 的运行次数仍为0。

进入该分支的最后条件 `0x06548ed2` 分别连续10次 `LDRH` 采样 `0x7f080410` 和 `0x7f080094` 的 bit4：只要任一路从未读到高电平，就返回1。当前未建模输入均读0，因而形成持续有效的输入条件。显式 `inactive-inputs` 配置只给这两个输入 bit4=1，让原代码自然走后续路径，不修改任务 event。输入物理名称及真机上电电平尚未确定：独立 SD 驱动 `0x064b201c` 按两个 slot 索引对这两个输入做去抖，强烈提示卡检测用途，但不是已核实的引脚命名。

这些地址同时属于通用 GPIO 寄存器映射；`0x06831bba` 可以写其高位配置字段。bit4 的读取语义不能推广为整个32位寄存器只读。前一个 wrapper `0x06469eaa` 是向**另一个** event ID124 OR 入 mask，随后才等待125，不能描述成清除同一个事件。后续主启动的 `inactive-inputs-boot` 已越过此等待、到达 SPI0 初始化；尚无菜单渲染证据。

## Loader GIC 路由与真正的 IRQ 向量

[`loader-gic.json`](loader-gic.json) 从原 `loader3` 文件偏移 `0x6978` 提取全部416条 route/config，保留各自目标CPU与边沿/电平配置。原 Thumb 函数 `0x10e8` 将该表写入 `ITARGETSR 0x70001820` 和 `ICFGR 0x70001c08`；它不是把所有 IRQ 指向 CPU0。原 loader2 文件偏移 `0x1998` 写 `GICD_CTLR=1`，loader3 的 CPU0 启动路径写 `GICC_CTLR=1`、`PMR=0xf0`。

[`verify_loader_gic.py`](verify_loader_gic.py) 在 QEMU 上隔离执行原 `0x10e8`，原函数字节未改。实际正常返回、SP恢复；用独立 guest `LDRB` 辅助程序读回后，416个 targets 字节与104个 config 字节均逐字节匹配原表：[PASS 结果](results-loader-gic-guest-readback/result.json)。辅助程序只用于读取验证，不替代固件初始化。第一次尝试直接使用 GDB 批量读 GIC 时连接关闭，发生在原函数执行前；记录在 `results-loader-gic/result.json`，不作为固件错误。

IRQ 入口要求原 `02_loader3.bin` 继续驻留低地址0：`0x18 → 0x4c → 0x06280000 → 0x068f9a0c → 0x068f9c1c`，最后是原 RTOS 保存上下文及分发入口。当前诊断中的 VBAR0 与此链一致，空白 RAM0 才是缺口。恢复整个48KiB loader3作为驻留内容，不等于执行完整 loader。

原 IRQ handler 自身按 `MPIDR & 3` 使用 `GICC_IAR/EOIR = 0x7000200c/10 + CPU*0x8000`，所以 CPU1/2 的 SoC GIC alias 也必须建模；只恢复 CPU0 的接口不足。全部原指令见 [`loader-gic.disasm.txt`](loader-gic.disasm.txt)。此独立测试证明 route 初始化可执行，IRQ 真正递送及完整菜单仍需主启动另行验证。


## UART2 与 ADC 休眠检查

原 UART0..4 的 GIC ID 不是连续编号：`85, 86, 294, 295, 296`，由初始化数据和全局 IRQ 表逐项证明，见 [`uart-evidence.json`](uart-evidence.json)。UART2 最初卡在 `0x0648f354` 的 RX FIFO 清空循环：FR bit4应表示RXFE，未建模的全零读数却表示永远有数据。补标准 PL011 空FIFO语义后，主启动实际捕获 UART2 `34 27` 两个字节；这是原厂请求，未向其注入成功回复。两条请求分别期待5字节和4字节响应；普通请求超时阈值675,000个原timer1 tick，即当前时钟约0.4秒。原代码在缺席时能超时继续。

其后原厂主任务无条件调用 `0x0653ee1c`。`pre-sleep-checkpoint` 的真实断点 `0x0653efe2` 显示 ADC 采样0，r9=`0x296`、r10=`0x26d`、最终比较阈值r0=`0x25e`。保存栈中的采样也为0。源是 ADC 通道8（`0x7f030034`）和11（`0x7f030040`），程序对通道8作条件校正后选择两路最大值；两路全零使原程序主动进入 DRAM sleep 路径。UART返回值不是这条分支的直接条件。完整现场和分支推导见 [`power-sleep-evidence.json`](power-sleep-evidence.json)。

可测试的环境是显式提供 ADC11 raw=`0x300`，让同一原厂比较自然通过。本数值只表示实验 ADC 输入，不是已测得相机电压，未修改阈值或条件跳转。ADC控制器建模及正常采样后的实际启动由主启动工作验证。

还有一个独立的存储映射错误：原 sleep 代码切换到 SRAM 栈 `0x7002a3f0`，旧4KiB SRAM窗口未覆盖这里。trace中三个原返回地址写入都被忽略；随后的 POP 读0，跳进低地址 loader，使最终PC落在`0x98`。这不证明原厂打算重启；恢复 SRAM 代码和栈存储是必要条件。对应原指令保存在 [`uart-power.disasm.txt`](uart-power.disasm.txt)，[`inspect_uart_power.py`](inspect_uart_power.py) 可以从原文件和保留快照重建报告。


## ADC 通过后的首次系统延迟

`menu-smp/results/adc-environment-boot` 使用显式 ADC11 raw=`0x300` 后，原厂代码实际离开休眠路径，执行到 `0x063c8b76` 的外设初始化。新暂停仍为三核 idle，但原因已变：`uiTSK001` 的等待类型是2、剩余超时2 ticks，对象为空。保存返回链为 `63c8b7b ← 647e837 ← 647c4b1 ← 64a074d ← 66c246b`，逐项对应原厂调用指令。`0x064a070e` 给 GPIO `0x7f081c14` 写复位序列后，在 `0x064a0748` 请求普通1 tick延迟；RTOS `0x066c23e8` 将请求存为 `argument+1`。因此这里不是等待 RTC 秒数变化。

四个独立时钟计数 `0x08aaa5f4`、`0x08aaa7b4`、`0x08ab6394`、`0x08ab18f8` 全为1，说明原 timer0 回调和系统时钟都仅运行过一次。原 timer0 配置 Load=`0x6978`，当前27 MHz输入对应1 ms；GIC ID36按原loader配置为边沿触发。保存的GIC状态为该IRQ电平仍高、pending=0、active=0。原callback `0x06485178` 与 IRQ trampoline 未清 timer INTCLR；驱动配置还固定设置了标准SP804保留的Control bit4。

这些证据指向尚未建模的 SoC 定时器脉冲/自动清除行为。bit4的真实硬件含义仍是推断，不能当作已取得数据手册证明；需要在明确标注的推断配置下验证原回调重复、原延迟返回。不能以修改固件等待、GIC原配置或直接唤醒任务代替这项验证。可重跑 `python3 -B s5-emulator/serial/inspect_tick_wait.py`，产物为 [`tick-wait-evidence.json`](tick-wait-evidence.json) 与 [`tick-wait.disasm.txt`](tick-wait.disasm.txt)。本快照尚无菜单渲染。


后续 `pulse-timer-boot` 已验证这项推进：在显式推断的 SoC bit4脉冲配置下，三项单调时钟计数均增长至21,791，原 `dly` 返回，主任务继续执行。分频计数为1，符合mod10。没有改原厂延迟、GIC route或任务event；硬件bit4的真实名称仍未独立证实。

## I²C 音频初始化及有界后台重试

脉冲计时器启动后的新主等待是event19、mask3、OR模式、永久等待其后台完成回调。原字符串明确当前模块为 `audio_hpamp_on()`，向总线0、地址参数`0x30`写2字节寄存器；GPIO `0x7f081c14`是该初始化的复位输出，不能据地址索引命名为SPI5片选。原厂后台 `uiTSK033`（entry`0x066ce107`）等待的是另一event18，保存现场尚余29ticks，属于有界超时。每项操作最多4次重试，错误也会经原callback `0x066ce2aa`向event19置bit2，让主调用返回false；成功status0对应bit1。

因此20秒运行截断并不能证明永久卡死：主栈已经处于DRC寄存器循环的`0x26`/`0x66`，总线发生351次启动尝试。需要更长的有界启动，或明确声明缺席的generic-error控制器，验证原错误路径是否自然继续。没有构造成功ACK，也未确认音频放大器具体型号。完整现场和原代码见 [`i2c-wait-evidence.json`](i2c-wait-evidence.json)、[`i2c-wait.disasm.txt`](i2c-wait.disasm.txt)，可用 `inspect_i2c_wait.py`重建。

## 原厂软件按键入口（仅静态，不代表已打开菜单）

[`extract_key_events.py`](extract_key_events.py) 提取125项原script表及4项动作表，输出 [`key-event-evidence.json`](key-event-evidence.json) 和 [`key-events.disasm.txt`](key-events.disasm.txt)。原解析函数Thumb `0x0664bf95`接受argc/argv：argv1为表名或十进制索引，argv2为动作索引；动作`0=OPEN`（释放）、`1=TRIGGER`（按下）、`2=REPEAT`、`3=ONE_SHOT`。它通过原消息API `0x067f0dda`提交`0x0011f002`，payload是两个u32 `{表索引, 动作索引}`。原接收器`0x06475886`将其送入`0x06643680`及现有输入状态机。

最有根据的MENU/SET候选是`KEY_CENTER`，索引7、bank0 mask`0x10`；原分发会在UI输入分支映射为UI key7/event`0x0f000002`，或在正常相机输入分支进入原`0x06646e84`的状态检查与转换。`KEY_Q_MENU`是独立索引15/mask`0x2000`；`KEY_SET`是索引58、另一bank，不能因为名称相似而混用。表索引、bitmask、动作索引及UI keycode也不是同一编号。

本轮没有向QEMU注入按键，尚未证明候选能打开菜单，也没有取得物理GPIO映射。如果后续测试通过，准确名称应为“原厂软件按键事件输入”。应让原消息处理及状态检查自然执行，不能直接调用最后的菜单转换来代替按键。尚未验证从任意暂停task调用此API的锁与调度安全性，运行时探针须选择合适task上下文并保留外层超时。


## 60秒基线：原 I²C 超时通过，UI 任务已就绪

`menu-smp/results/pulse-timer-60s` 保留未加速的I²C基线。原重试/超时实际结束，主启动命中 `0x063c8b94` 和原task18激活调用。UI任务`uiTSK018`已ready、priority108，但run_count仍为0；CPU0当前任务为priority56的`uiTSK066`。最终PC`0x18`只是周期中断的采样时刻，其被打断的原PC为`0x065491f0`：函数`0x065491e4`无超时、无yield地轮询`0x7b004000`，持续占用CPU0。

原启动函数已经上传另一执行体的嵌入payload：`0x06a2d918`的64KiB代码送至`0x7b020000`，`0x06a3d918`的128KiB数据送至`0x7b000000`，随后释放`0x7c14071c`的bit3。原ARM驱动使用逐word复制，未经过DMA。接下来由peer填`0x7b004000`及两个halfword-address共享指针，主机检查多处magic，确认结构后写第二阶段确认并等待peer完成。初始data没有这些magic；不能把主机要求当作已知真实响应直接填满。

[`coprocessor-wait-evidence.json`](coprocessor-wait-evidence.json) 含真实TCB、就绪队列、栈及双阶段协议；[`inspect_coprocessor_wait.py`](inspect_coprocessor_wait.py)重建报告并逐字节提取原 [`embedded-peer-code.bin`](embedded-peer-code.bin)、[`embedded-peer-data.bin`](embedded-peer-data.bin)，各自摘要写入报告。执行架构尚未识别，不能仅凭首字节命名为某种DSP；本工作没有执行这些peer指令或伪造其ready。

## 可选局部调度实验的原 API

[`isolated-delay-abi.json`](isolated-delay-abi.json)核实可以在task66的原Thumb busy-loop断点处，通过已经动态验证的原 `0x066c23e8(60000)`延迟当前任务约60秒客体时间，让其他ready任务执行。此函数使用原调度器；不直接修改TCB或event。必须保存上下文、保留原task栈、设独立scratch返回断点，返回后才恢复原上下文；不能在IRQ入口或另一task执行期间强行恢复。若UI依赖task66持有的锁或未完成状态，应保留该等待，不手工清除。

这只是一项明确标注的局部显示调度试验，不是整机启动完成，也不是协处理器完成。静态另定位原sus_tsk-like入口`0x06535969`和rsm_tsk-like入口`0x0654911d`，但未在本工作调用；首轮优先采用有界delay。原指令见 [`isolated-delay.disasm.txt`](isolated-delay.disasm.txt)。

## 局部实验实际进入 LCD 初始化：SPI6

`menu-smp/results/isolated-display-first` 通过上述原delay API后，真实命中task18入口`0x0647e83e`及callback注册`0x06495ab6`。CPU0当前TCB确为`uiTSK018`，run_count=9；未到图形生成。新停止点是SPI6控制器`0x7f046000`的发送FIFO空状态等待，原TX为1字节`60`，尚未送出。

原调用链及字符串将其明确定位为`avio_control_lcdinit_static()`→`avio_lcdevf_driver_serial_sendall(lcdevf=0)`→LCD写循环`0x06483aee`。首word来自原`14_eep_fix.bin`偏移`0x1a000`，加载到`0x0631a000`，值`0x3000`。每word拆成两次一字节传输：`((word>>8)&0x7f)<<1`、`word&0xff`，因此首pair为`60 00`。GPIO编码寄存器`0x7f083018`在pair前写`0x10000000`、pair后写`0x10000001`；物理引脚电平未测量。

单字节helper会读回RX，但此LCD循环丢弃返回值，不检查ACK。补齐PL022控制器可以继续执行这条原写路径，无需伪造成功响应。另一分支SPI4/`0x7f082818`对应EVF，并具有读取分支，不能套用LCD的接收语义。LCD具体型号、扫描输出、菜单渲染均未由本快照验证。

[`inspect_lcd_spi_wait.py`](inspect_lcd_spi_wait.py)可重建[`lcd-spi-wait-evidence.json`](lcd-spi-wait-evidence.json)与[`lcd-spi-wait.disasm.txt`](lcd-spi-wait.disasm.txt)，含当前任务、实际栈、配置表来源及前8组原TX；源码和EEPROM字节未修改。

## 原厂 native window 与双缓冲区

局部实验`isolated-display-context`已经真实调用`0x064deeba`并返回`0x0d7554e0`。它是原分配器创建的`DWD\0`对象，大小`0x38`，宽1024、高768、stride4096，每块缓冲区3MiB、共2块。原应用的960×640几何参数与该分配尺寸不同，后续必须继续遵守实际viewport和plane参数，不能统一改成某一尺寸。

`0x064c46c4`接收`r1=EGLsurface,r2=outNative`，检查真实surface的`+8==1`，返回其`+0x68`中的native对象。软件EGL只需在此建立host surface→原native指针映射，不能把host handle当客体结构解引用。

原`0x064de7da`查询`E011/E012`返回两块原buffer的base地址：本次分别`0x0dd0a800`和`0x0da0a800`。对应CPU别名`0x8dd0a800/0x8da0a800`在buffer descriptor另一字段。`0x064de686`查询`E010`实际返回下一buffer索引，公式`(current+1)%count`，本次为1；此前将它称为“format query”的guard标签不准确。内部像素format=0、32bpp已证，但仅凭这些字段不能确定通道字节序。

原自定义surface请求`3fdd=3fde`不是任意成功标志：setter校验支持位并保存模式，消费者可选择原CPU可访问buffer的逐行memcpy路径。准确扩展名称尚未证实；软件桥接应记录这一模式并同步真实buffer，不静默吞掉未知值。原native flip函数还承担解锁，不能脱离其lock/present调用协议任意调用。

[`inspect_native_surface.py`](inspect_native_surface.py)重建[`native-surface-abi.json`](native-surface-abi.json)及[`native-surface-abi.disasm.txt`](native-surface-abi.disasm.txt)，包含真实对象和descriptor字节、属性ABI、地址映射与原指令。这里只读取快照；没有修改guest对象、分配器、任务或原指令。

## 原厂软件按键队列：实际消费已验证

前面的“仅静态”按键段保留当时证据边界。后续独立模块[`key_probe.py`](../menu-smp/key_probe.py)已完成，并由主启动器在`isolated-menu-key-vfp`实际验证`KEY_CENTER`按下：原API分配并复制payload，原queue115接收同一指针，原bank0输入状态处理器消费mask`0x10`，最终原分发器返回。此时实际产生的UI事件是`0x0f03002d(0,0)`，UI仍在启动mode6；没有菜单画面。

探针只在原task285下一次收消息前调用原`0x067f0dda`，timeout参数固定0。它检查真实任务身份、SVC/Thumb状态、IRQ开启、无IT块、调度/中断嵌套计数，以及原栈8字节对齐和4KiB余量。32字节临时参数位于该任务原栈，原调用返回后恢复。原TCB、event、输入状态和指令均不修改；所有软件按键仍经过原处理器的状态检查。只允许CENTER、方向、PLAY、Q_MENU、SET，以及按下/释放；不能传任意事件码。

真实QEMU的`g`包只有68字节，不能据target.xml包含VFP就认为`g/G`已保存浮点状态。当前实现从实际qXfer XML解析编号，单独以`p/P`保存、恢复并核对D0..D31、FPSCR、FPEXC；FPSID只读校验。无固件的调试器传输测试通过，见[`restore-result.json`](results-key-register-xml/restore-result.json)。9项host-only测试也通过，但它们不替代上述真实原程序消费证据。

ABI、guard和消费断点见[`key-queue-abi.json`](key-queue-abi.json)、[`key-queue.disasm.txt`](key-queue.disasm.txt)。`consumed=true`仅表示原分发器返回，不能当作菜单已打开。CENTER与独立的SET表项不能混用，物理MENU/SET引脚身份仍未验证。

后续 `arm(..., wait_for_ui_active=True)` 支持在原task285接收循环等待 UI 状态就绪：读取 `0x08a928b4` 的state与 `0x08a928bc` 的mode，state为0或mode为6时只单步下一条原指令并保留循环断点，不提交按键、不改变状态，也不延长期限。首次state非0且mode非6后仍须通过全部原调用guard。默认False保留此前行为；等待与消费期限由构造参数 `consumption_timeout` 设置，默认5秒、最高30秒。报告记录状态采样和等待次数，超时明确标为 `timeout_waiting_for_ui_active`。当前13项host检查通过；此开关自身不证明菜单已出现。

另有兼容默认关闭的 `wait_for_mode35_ack=True`：在同一个原循环停止点要求 `0x08aa1103` 低4位为`0xf`，才允许提交按键。可与UI状态gate同时启用；等待共用已有期限，不随循环重置。报告 `mode35_ack_gate` 保存实际mask及采样/等待次数，未满足时只执行下一条原指令。超时状态为 `timeout_waiting_for_mode35_ack`，关闭探针保留原因。16项host合同测试通过；此gate不写确认字，也不代表菜单已显示。

## 首次图形交换后的真实启动等待

`isolated-display-gradient`运行120秒，真实执行4次绘制、1次EGL交换；最初的4×4路径来自原`0x07290030`图形预热代码。task66的原60000tick延迟已经返回并恢复上下文，然后重新等待未实现的协处理器。延迟本身没有完成协处理器初始化。

快照中的GUI模式控制块`0x08aa10fc`处于阶段2，进度字`0x08cb0ee0=0x3c2`。原`0x066c0964`等待确认byte`0x08aa1104`等于`0x0f`，实际为`0x0d`，缺少bit1。原请求已经存在queue`0x76`中：消息包`0x099cab3c`为`{replyqueue=0x115, source=0x11d, code=0x36, mode=1, size=0}`。该队列的消费者正是task66：它必须先返回`0x0676ede0`初始化，才会在`0x0675efc2`开始收取请求。

完成消息`0x36`应由原流程返回给输入任务，后者在`0x064759aa`调用原`0x066c142c(2)`补齐确认。当前请求尚未消费，不能自行发送完成消息或置位。UI全局`0x08a928b0+0xc`仍为6；原`0x066beef0`在此模式跳过常规UI状态处理器。这足以解释当前只有预热帧、软件按键未打开菜单，无需假定Linux是这一处等待的原因。此快照CPU3明确被park，后续功能是否依赖Linux仍未由本分析排除。

[`inspect_gui_start_wait.py`](inspect_gui_start_wait.py)只读重建[`gui-start-wait-evidence.json`](gui-start-wait-evidence.json)及[`gui-start-wait.disasm.txt`](gui-start-wait.disasm.txt)，包含实际任务、队列、消息、状态和原调用证据。

## LCD 配置提交及场同步：已证中断链，时序模型尚不充分

[`inspect_display_clock.py`](inspect_display_clock.py)只读原程序、loader 和 `isolated-display-gradient` 快照，重建 [`display-clock-evidence.json`](display-clock-evidence.json) 与 [`display-clock-evidence.disasm.txt`](display-clock-evidence.disasm.txt)。七个中断的注册立即数、原函数指针和 loader 路由逐项校验通过：GIC ID `196、224、226、230、231、232、233`，对应 QEMU SPI 索引须减32。原 loader 将它们路由到 CPU0，并配置为边沿触发；快照中均已使能，pending、active、level 为0。这是暂停时的状态，单凭快照不能断言这些中断从未发生。

GIC196 经 `0x0649728e → 0x0648fdd8 → 0x06481598` 尝试提交 LCD 配置。原 `0x06489ae0` 先检查 bank 状态，再要求 `0x7c00011c` bit0 为1，才能将 `0x090db3d4` 中的脏配置提交至 `0x7c051c00`，返回 `0xa8c` 后把 LCD 状态从1推进到2。另一阶段读的是 pipeline0 的 `0x7c050ef0` bit28。寄存器的物理名称、字段极性和变化时序仍未证实。

后续 GIC230 / 232 按原 byte `0x08ab1bba` 选择，当前值0对应232。原处理器检查并清除 sibling IRQ231 / 233 的 GIC pending；经过 `0x0649694e → 0x0648b728 → 0x0648fe4e → 0x064815c8`，两次有效调用依次推进状态2→3→4，调用原回调8、9。最终回调9才将 AVIO 等待字 `0x08a9c7f8` 设为2。这条 GIC pending 清除证据不是显示设备 W1C 寄存器的证明。

保存快照中 LCD 状态仍为1，回调 `0x0647dcf7` 仍挂起，配置 dirty 为1，AVIO 等待字为0。实际 trace 显示相关 MMIO 写入被忽略，包括原表对 `0x7c000134` 两次写入 `0x6df91`。若假设27MHz输入和 reload-minus-one 语义，可算出约59.94Hz；**两项前提和寄存器的帧周期含义都未取得证据**。目前还缺设备侧 enable/mask、196与232的相位关系、field翻转规律和设备侧ACK，不能据这一数值实现并宣称正确的场同步时钟模型。

本分析未注入中断、调用完成回调或修改任何状态。即使后续真实完成 LCD 同步，task66 尚未消费的 queue76 启动请求仍是独立的 GUI 阻点；这项硬件等待与未知协处理器执行都需保留，不能用直接设置 callback、GUI ack 或 scene 状态来替代。

后续独立 [`display_clock_probe.py`](../menu-smp/display_clock_probe.py) 已用明确的 TEST 输入实际检验这条链，结果见 [`local-display-clock-test/result.json`](../menu-smp/results/local-display-clock-test/result.json)。它默认不操作客体；只有显式 `start()` 且真实LCD初始状态/GIC路由满足时，才通过QOM提供两个已知状态位与三次有限的真实GIC脉冲：196一次、232两次。原ISR全部命中，LCD从1经2、3到4，AVIO从0经1到2，原callback清零，原dirty由1清零。结束后测试输入恢复关闭。11项host测试检查默认无副作用、错误/超时关闭和有限阶段；它们不替代该实际原指令执行结果。

这是外部测试信号，不是已还原的自动时钟：没有假定59.94Hz、自动周期、field翻转或未知设备ACK。模块不写UI、DSP、TCB或完成字，也不直接调用callback。实际完成LCD链后仍只有原4×4预热帧，mode35确认仍缺失；因此不能称完整菜单已运行。

## 单次原厂 GUI 服务：函数级局部执行探针

[`ui_service_probe.py`](../menu-smp/ui_service_probe.py)提供独立 `UIServiceProbe`，由主启动器显式集成；导入和构造不会执行客体代码。其范围是：在原 task66 的 DSP 初始化等待处暂停，临时调用原队列接收和分发函数处理**已经排队**的一条 mode1 请求，再恢复原等待上下文。这改变了原启动调用顺序，是函数级局部实验；即使成功也不表示 DSP 初始化、LCD 同步或整机启动完成。

探针仅接受 queue76 当前实际队头 `{0x115,0x11d,0x36,1,0}`。它通过原 queue table 动态读取 object、read index、pointer-array 和 envelope，检查无 waiting sender、pool6 前缀正确；同时检查真实 task66、SVC/Thumb、IRQ开启、无IT块、无中断嵌套或 dispatch disable、原栈至少4KiB余量。原SP为4 mod8时，只将调用SP向下对齐8字节，保留并检查原栈现场。

接收调用为 `0x067ea67a(0x76,0x0a603680,0)`：原函数复制20字节到该任务自己的工作缓冲并释放 pool6 envelope，探针不复制、构造或再次释放消息。随后调用原 `0x0675f154(buffer)`；默认command36的严格 mode1 分支经 `0x0653b7e6` 执行原 ACK helper `0x06759710(0x36,1,0)`。静态完整合同见 [`ui-service-contract.json`](../qemu/peer-isa-analysis/protocol-feasibility/ui-service-contract.json) 与[原指令](../qemu/peer-isa-analysis/protocol-feasibility/ui-service-disassembly.txt)。另有下述独立command35诊断合同；其他命令或mode不在允许范围。

`handled=1`本身不算成功。探针还要求原pool释放返回0、原ACK入队返回0，并核对原ring中的回复 `{0x76,0x42,0x36,0,1}`。原分发返回后检查CPU、任务、栈和调度状态，才恢复完整core包与独立35项浮点状态；FPSID只读，不写。模块不人工写入任何客体RAM，原API产生的工作buffer和队列效果保留。不调用GUI确认函数、不设置DSP ready/magic，不改UI状态。

接口为 `start(cpu)`、`handle(pc,cpu)`、`active/completed`、`timeout_hint()`、`check_timeout()`、`close()`、`report`。独立返回断点默认 `0x1bd00`，仅用Z0；每阶段默认2秒期限。若调用未返回、ACK失败或上下文不匹配，停止局部实验并保留现场，不向未知任务强行恢复寄存器。16项host合同检查通过：`python3 -B s5-emulator/serial/test_ui_service_probe.py`。这些检查不执行固件；实际结果须引用主启动器运行产物。

主启动器的 `local-ui-service-first` 已实际完成默认command36：原receive、pool release、ACK enqueue均返回0，dispatch返回1，原等待context恢复；DSP ready仍未完成。后续UI实际进入state1/mode1，但不能据此称菜单已渲染。

## 新阶段0x436：第二条请求，不是旧确认字或LCD直接检查

[`inspect_gui_stage436.py`](inspect_gui_stage436.py)重建[`gui-stage436-evidence.json`](gui-stage436-evidence.json)和[原指令](gui-stage436.disasm.txt)。在 `local-ui-service-first` 快照中，旧确认byte `0x08aa1104` 已为`0xff`；当前step `0x436` 的原分支 `0x066c0a54` 实际读取**另一个byte `0x08aa1103`**，当前`0x0d`、要求`0x0f`。缺少的mask2由原输入任务接收code`0x35`后在 `0x064759b4 → 0x066c1436(2)`补入。

原queue76中确实还有两条未消费消息：队头 `{0x115,0x11d,0x35,1,0}`，下一条 `{0x115,0x11d,0x2b,0,0}`。所以这一处直接等待是task66的后续软件确认；不能因为LCD state1/AVIO0同时存在，就说step436直接读取LCD完成字。LCD同步仍是独立未完成条件。

显式 `UIServiceProbe(..., command=0x35)` 是新的控制处理器诊断，默认36行为不变。35的实际分支取决于flag `0x08a9da70=0`、previous mode `0x08aa0e10=0xff`：原 `0x0653b86c` 会调用 `0x066d04dc(0xff)` 和 `0x067361a8(0)`，不是无条件短ACK。它访问原对象及显示控制接口、注册callback、投递其他请求，后续硬件/DSP依赖尚未完全确定。

该选项只接受上述真实队头，并要求这组flag/previous值、两个ready byte，以及原对象/vtable/方法指针均符合快照证据。仍用原receive和原dispatcher；只观察控制函数入口，不跳过异步初始化，不改previous或确认字。若原dispatch返回1而未同步发出ACK，报告 `handler_returned_ack_pending`；`completed`仅表示原调用已返回并恢复等待context，**不表示mode完成**。未知状态、未知方法或阶段超时均停止并保留现场。command35真实执行结果由后续主启动器报告，不能用host检查代替。

## command35 返回后的实际 DATA_ABORT：高地址池的保护字写入

`local-ui-mode35` 中原command35处理器返回1、ACK仍待异步完成、等待context成功恢复；随后发生异常的是CPU2上的原task285。原 `0x0646bcc6: STR r6,[r7]` 向 `0xffa91000` 写入 `0x7d159000`。复跑 `local-ui-mode35-fault-regs` 记录 DFAR=`0xffa91000`、DFSR=`0x808`（写操作的同步外部异常）、TTBR0=`0x100000`，与首次快照中的identity section映射一致。它不是翻译或权限异常。

分配descriptor `0x09051738` 中base=`0x7d159000`、size=`0x02938080`，末尾保护区128字节。原地址转换先将base变为`0xfd159000`，再加`size−128`得到故障地址。这个减法没有下溢。栈中四条调用返回地址逐一匹配原allocator调用链；ABT模式r13=0不是被中断task的栈指针，不能把它当作栈损坏证据。

巨大arena范围来自原固件的明确初始化：`0x0646adf0`以start=`0x0fc00000`、size=`0x703e0000`构造39项，结束于`0x7ffe0000`；最后一项正是本次base。原转换表 `0x06ff11b0` 的第二行把field1地址 `0x40000000..0x7fffffff`转换到field3地址 `0xc0000000..0xffffffff`，field2则为`DEAD`。因此地址不是本次调用随意生成的坏指针，也不能按低512MiB折返修复。

这些证据支持**显式2GiB高地址RAM backing实验**，以检验原保护字访问是否自然通过；它不证明实机物理容量。第二bank没有可用field2地址，不能因此覆盖low `0x40000000..0x7fffffff` 的Device/MMIO映射。后续初始化是否进一步限制该池、以及后续真实硬件等待，仍需原执行证据。

[`inspect_mode35_fault.py`](inspect_mode35_fault.py)只读重建[`mode35-fault-evidence.json`](mode35-fault-evidence.json)和[原指令](mode35-fault-evidence.disasm.txt)，包含完整arena表、转换表、真实异常寄存器、page section、分配descriptor和验证过的保存返回地址。未填指针、改allocator、跳异常或写DSP ready。

## mode35异步确认的真实帧消息入口

[`inspect_mode35_frame_wait.py`](inspect_mode35_frame_wait.py)只读重建[`mode35-frame-wait-evidence.json`](mode35-frame-wait-evidence.json)和[选定原指令](mode35-frame-wait.disasm.txt)。`local-ui-loop-postinit`中LCD已完成，原task66已消费40001并置自己的controller+15，postinit原调用已返回；但原task67仍阻塞在queue77，队列read/write index均0、count0。模式控制轮询半字 `0x08aac26c` 仍0。

实际链是queue77帧消息→原task67 `0x06705544`→原payload复制→按code0..3进行帧处理→`0x066d19c2`→`0x066d179c`→`0x066d16fe`。后者通过原准备状态或按调用次数的fallback，再调用 `0x067361a8(1)`→`0x066d1a22`发送code35确认。这里的计数不是已验证的毫秒/帧率，不能直接调用轮询代替缺少的帧源。原payload处理也可能先等待其他event；产生一次源中断不保证完成模式确认。

源消息helper `0x065206b8` 使用原static ring构造 `{0,0x2001,code,payload,0}`；相关code0..3来自原 `0x0671edfa` 下游。已接合到IRQ192/193相关服务链，与LCD196/232不同。下一诊断应保留真实源ISR、metadata生成及队列消费，不能合成GUI ACK。另见 [`timer6-queue16-wait-evidence.json`](timer6-queue16-wait-evidence.json)：原初始化序列停在index1时，queue16已消费一条并正常等待下一条；timer6 LOAD重启行为由QEMU模型检查，不能靠修改序列index或ready字绕过。

后续 `local-timer-rearm` 实测原序列自然到index10，`0x08aa7f19`由原代码设为1。新的 [`frame_irq_probe.py`](../menu-smp/frame_irq_probe.py) 提供默认关闭的 `FrameIRQProbe(emu,pairs=1)`，只支持1至4组显式192→193真实GIC TEST输入，不供应像素或新MMIO状态。开始前检查原ready、postinit、40001回复、源gate和真实GIC路由；首次源latch允许为0，交由原ISR建立，首帧变化抑制也保留。

每个脉冲观察原ISR入口和实际LR返回；LR必须紧随原BLX寄存器指令，返回须匹配CPU0、IRQ模式和原栈。共享返回断点若命中其他核或其他栈，只执行下一条原指令并继续有限等待。阶段超时或未知状态关闭TEST、保留原执行现场，不强改PC或恢复未知上下文。11项host检查通过；这仅验证工具合同，真实ISR、metadata及模式确认的结果由主启动器产物记录。

`local-frame-pairs2` 后续实际完成了四个ISR入口/返回，原source latch与gate由原代码推进，q77真实生产并消费，原模式轮询计数达到1；仍没有新菜单绘制。当前探针上限已扩至128组、默认仍1组：从第二组起，每次193返回后等待原task67接收成功、`0x067055ba`处理完成、`0x06705560`回到接收循环，并确认本组消息排空及轮询推进，才发下一组，避免静态payload覆盖。

模式确认另行观察原 `0x06759710(0x35,1,0)` 和 `0x06759766` 入队返回0，核对原ring消息 `{0x76,0x42,0x35,0,1}`。若原ACK已入队但UI尚未消费，停止测试输入让正常任务调度，明确报告 `original_mode35_ack_enqueued_ui_pending`；只有实际确认掩码低4位为`0xf`才报告UI确认已消费。16项host合同测试通过。首次逐帧实跑遇到多核legacy单步的调试器停止点，证据见 [`frame-paced-debugger-stop.json`](frame-paced-debugger-stop.json)，不计为固件异常或模式完成。

## 原模式确认完成及完整软件点击对照

`local-frame-paced-menu-timeout10` 已实际执行92组有限帧输入、消费91帧，原轮询计数到91后走原fallback；原code35 ACK入队返回0，并由原输入任务补齐确认字。TEST输入随后关闭。这没有补写DSP ready，也没有调用轮询函数代替帧处理。此前2秒阶段上限停在原有限设备轮询中；延长至10秒后它自然返回，证据见 [`frame-device-poll-evidence.json`](frame-device-poll-evidence.json)。

进度字 `0x4a0` 是成功终态，**不是新的硬件等待**：原 `0x066c0b3a..0b52` 清transition stage、提交模式、发 `0x0f000015`，写step4a0并返回1。只读复现 [`inspect_gui_terminal_key.py`](inspect_gui_terminal_key.py) 生成 [`gui-terminal-key-evidence.json`](gui-terminal-key-evidence.json) 与[原指令](gui-terminal-key.disasm.txt)。

`KeyProbe.arm(...,wait_for_transition_complete=True)` 在原task285循环同一次采样中要求step4a0、stage0、state1、mode1。等待只执行原指令且保持原期限。`local-terminal-menu-click-tx` 的完整press1→release0均通过原poolcopy、queue receive、原分发和寄存器恢复；press仍只产生通用事件 `0x0f03002d`，release未产生已观察到的UI事件，尚无菜单。这排除了“仅缺释放”与“仅早于transition完成”的解释。

为定位原分支，探针增加有界只读 `original_key_route` 记录：原输入路由的返回值、实际guard字段及原task栈上的0x58字节局部payload；CENTER共享表断点还须匹配精确原表槽 `0x06f10554`。不重写返回值、payload或guard。当前22项host合同测试通过，包括完整请求后再次arm、只读observer和栈边界拒绝；真实route结果以之后的主启动器运行产物为准。

## 原GPIO采样到软件CENTER的精确映射

[`inspect_key_input_source.py`](inspect_key_input_source.py) 对 `local-terminal-menu-click-tx` 只读复现 [`key-input-source-evidence.json`](key-input-source-evidence.json) 和[原采样指令](key-input-producer.disasm.txt)。原 `0x06545536` 读取16位 `0x7f082814`，bit4为0时产生raw mask `0x8000`；原mapping表将它转换为bank0 mask `0x10`，对应原命名表的 `KEY_CENTER`。mapping表的23项逐字节匹配原program offset `0xe4939c`；对实际raw值转换后精确得到全局输入值 `0x03b04f51`。

这证明当前零输入环境包含持续按住的CENTER，支持后续单独验证该输入bit4=1的释放环境；本分析没有修改GPIO模型。也不能据此直接认定它吞掉软件点击：原 `0x066435a0` 会将本次bank0的current/trigger/repeat/release四字段完整替换为 `{0x10,0x10,0,0}`，只保留其他bank及开关字段。物理面板标签与SoC pad名仍未独立核实，未知GPIO不能泛化置高。

## 原时区提示已经显示，原确认按键已经执行

`local-frame-query` 实际呈现了原厂 “Please set the time zone” 和 “Set” 按钮；modal为 `0x23c`，不能仅凭scene字段将提示认作scene1001。该轮press/release虽然都被原队列消费，但原 `0x0664322c` 输入门返回1，实际secondary stage为2，未进入正常按键分支。这是该轮直接观察到的拒绝原因，不能再以GPIO全局输入值代替该证据。

新增 `KeyProbe.arm(..., wait_for_input_route_ready=True)` 默认关闭。启用时在原task285循环读取原门条件：pending mode为ff、primary stage和secondary stage均为0或1；未满足时只执行原循环，保留同一个有界期限，不调用按键API，不修改状态。25项host检查通过。

在 `local-shared-image-stride` 实跑中，门经过12次采样、11次原循环等待后自然放行。原press实际产生UIkey7，命中 `0x0682e248 → 0x068b728e(2) → 0x068b7b3c`，把UI state改为 `0x20`、源scene改为 `0x70de`。随后release正常消费，没有再次进入时区确认；两次调用的core、浮点状态和栈均恢复。该轮渲染副本仍是scene1001/modal23c，因此证明的是原时区确认已执行，尚不证明下一页面已呈现。复现见 [`inspect_timezone_key.py`](inspect_timezone_key.py) 与 [`timezone-key-evidence.json`](timezone-key-evidence.json)。

输入锁是原异步配置流程的一部分：stage2/step870等待callback `0x0682e88c` 清pending byte；原queue16工作任务完成 `0x0646d5e8` 后才调用它，随后原UI循环推进stage1/step888。早期 `local-path-transform` 因图形停止而保存了中途wait状态，**不证明永久阻塞或缺少硬件**。后续自然解除已经覆盖这个不确定性；没有因此新增外设、写完成字或强行调用callback。静态链与这一区分见 [`secondary-input-stage-evidence.json`](secondary-input-stage-evidence.json)，可由 [`inspect_secondary_input_stage.py`](inspect_secondary_input_stage.py) 只读重建。

`local-requested-lcd-refresh` 随后通过原请求驱动的有限LCD232 TEST输入，实际执行110次draw、5次swap。源与渲染副本均更新为scene70de；显示的是覆盖其上的modal4a，原文字为镜头安装/释放按钮警告。press仍只执行一次原确认，release没有重复确认；未因这条警告改写UI状态。

[`inspect_lens_warning.py`](inspect_lens_warning.py) 复现 [`lens-warning-evidence.json`](lens-warning-evidence.json) 和[精确原指令](lens-warning.disasm.txt)：原串行读取0448/044a/044c/044e形成status `0x024a7710`，其中044e bit0经取反产生告警输入。原代码在该输入持续超过1000个原时间戳单位后置逻辑bit30，输入任务再添加modal4a；输入解除时同一原路径可移除警告。这里不是直接GPIO采样，不能泛化把GPIO拉高。候选最小外部环境是仅令044e读值bit0为1、保留其余位；其物理引脚和寄存器读写语义仍需模型审计。该分析没有执行输入变更，也不证明真实镜头已连接或DSP功能已完成。
