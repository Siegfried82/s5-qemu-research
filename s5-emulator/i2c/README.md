# S5 原厂 I2C 错误路径证据

已确认耳机放大器使用的原厂两字节写入链，以及能让原厂 ISR 报错的控制器状态。这里没有真实耳机放大器、没有成功 ACK，也没有宣称找到了硬件 NACK 位。

## 位置与调用

- 原厂 `0x064a0c30` 将 `[register,value]` 两字节送到 `0x066ce452`：bus=0、address byte=`0x30`、length=2。地址 byte 原样写进 DATA；不能因此把它称为 7-bit address `0x30`。
- 控制器地址：`0x7f058000 + bus * 0x100`，共五路。原表在 `0x06ff1188`。
- logical IRQ 表 `0x06ff119c` 是 `0x7c..0x80`，经 `0x06ff1240` 转换成 **GIC absolute ID 282..286 (`0x11a..0x11e`)**。
- worker `0x066ce106` → 四次重试 `0x066ce016` → transfer `0x066cdf62` → 原厂 ISR dispatcher `0x06499734`。

启动实际写 control `+0` 的 bit5、status `+4=0x10`、DATA `+0xc=address`、status `+4=0xf0`。RX 分支使用 `0xb0`。四种 ISR 都先检查 status bit3，置位便关闭/清除 IRQ，并报告 event18 mask2。worker 读取这个真实错误事件，四次失败后把非零状态交给原厂 callback `0x066ce2aa`；callback 再报告 main 正在等的 event19 mask2。

原厂 timeout 本来就会执行：`pulse-timer-boot` 记录了 351 次地址 `0x30` 和 TX start 写入。两字节请求的单次 event wait 参数是 60，重试四次。这里不把 OS 单位臆测成物理毫秒。

## 已执行验证

`probe_error_isr.py` 执行原厂机器码，测试 4 种 ISR mode、2 种 OS context、bus0/4。status=`0xf8` 的 **16/16** 案例均调用正确 IRQ wrapper，并将 event18 mask2 送到正确 OS 端点。另外 **8/8** 对照证实 status=`0xf0` 与 `0xf1` 都进入相同正常状态机。

```sh
python3 s5-emulator/i2c/probe_error_isr.py --output /tmp/s5-i2c-check
```

这项隔离测试使用 flat RAM 作为寄存器输入，拦截 OS context、IRQ wrapper 和最终 event 端点并记录参数。没有修改原厂指令，也没有证明完整 RTOS 的 IRQ/queue 已通过；后者须由 fullboot 验证。保存结果为 `error-isr-check-v2/verification.json`。

## 最小设备行为建议

可明确标注为“模拟通用控制器错误”：真实 start 写入后，锁存 status bit3 与 control bit4，按 control bit5 递送对应 IRQ。原厂 cleanup `0x06499752` 清 control bits4/5，写 status0；模型随之撤销 IRQ、清除错误。不要直接改 event、callback 状态或原厂分支。

本地 QEMU 的 Exynos I2C 实现布局相似，但它把无响应写成 status bit0。上述原厂执行对照证明 S5 的这几个 ISR 不会据此报错；直接复用可能让无响应请求进入正常完成。因此不能把现成 Exynos 空总线当作已验证的 S5 无 peer 模型，也不能把 bit3 重命名为真实 NACK。

精确地址、原固件 hash、boot RAM 证据片段、MMIO 记录与尚未确认的细节都在 `i2c-contract.json`；完整局部反汇编在 `driver-disassembly.txt`。本目录没有修改 C 模型或 graphics 后端。
