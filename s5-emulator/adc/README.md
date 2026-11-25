# 原厂 ADC 驱动与软件输入模型验证

原厂同步读取函数 `0x0659ed98` 已与新的 QEMU ADC 设备联合执行通过 10 个用例。输入 raw 0 时实际返回 `r0=1/out=0`；输入 raw `0x300` 时实际返回 `r0=1/out=0x300`。这不是主机直接替换采样函数返回值：原厂的寄存器配置、start 写入、完成轮询、读取结果及 `STRH` 输出均实际执行。

只有单函数隔离运行所需的 semaphore 25 acquire/release 被替代。未改原厂指令，未注入 IRQ，未执行或跳过主函数的电源判断分支。电池/休眠条件由完整 boot 对照另外验证；raw `0x300` 没有被解释为真实电池电压。

```bash
python3 s5-emulator/adc/probe_adc.py --output /tmp/s5-adc-check
```

现有结果：`driver-check/verification.json`。固件只加载原厂 program，以及原 scatter 指定的 program RW 源字节，来源/地址/hash 均记录。没有复制整份 boot RAM 或捏造结果指针表。

| Bank | MMIO base | 有效 logical channel | local channel | 原厂 base literal |
| --- | --- | --- | --- | --- |
| 0 | `0x7f030000` | 0–11 | 0–11 | `0x0659f0b8` |
| 1 | `0x7f031000` | 20–26 | 0–6 | `0x0659f0b4` |
| 2 | `0x7f032000` | 27–30 | 0–3 | `0x0659f0b0` |

读取 ABI：`r0=logical channel, r1=count, r2=uint16_t*out`。count 0 按 1 处理。`0x06f48dc4` 原始三个字节为 `[12,7,4]`；result 指针表位于原始 `0x070c87a4`，scatter 后为 `0x08aa7dec`，每 bank 间隔 `0x30`。

同步转换流程：

1. 若 control `+0` bit1 已设，直接读结果，不启动新转换。
2. 否则写 `+0xc=有效通道 mask`、`+8=count`、`+4=0`、`+0=1`。
3. 轮询 control `+0` bit0 与 status `+0x58` bit0，等待两者清零。
4. 读取 `base+0x14+4*local_channel` 的 word，直接保存低 16 bits，无缩放或右移；成功返回 1。
5. 超过约 1002 轮仍 busy 则清 start bit、返回 0，不写输出。bank1/2还分别受 `0x7f0382a0` bit0/bit1 gating 控制。

新 QEMU 模型在 start 时把独立外部输入锁存到 mask 选择的结果寄存器，然后清 busy。除 bank0/channel11 的可选 `adc11-sample` 外，其他输入目前为 0；这是显式实验输入，非真实硬件测量。

本次同步路径不需要 conversion IRQ，实际验证也未注入 IRQ。物理 ADC 的 IRQ 编号和异步/连续采样行为尚未确证，不应猜测 IRQ 断言。`adc-contract.json` 记录了这一限制及精确 pointer 搜索范围；不能用“没有发现 handler”断言硬件完全没有中断功能。

原始指令摘录在 `driver-disassembly.txt`。实际 boot MMIO 汇总中 `+14..40` 是 **read**，没有观察到初始化写结果寄存器；`+0/+4/+8/+c` 是 write，`+58` 是 read。
