# 原 UI 单条消息服务合同与 DSP 启动协议缺口

本目录只做离线原代码及既有 RAM 快照审计，没有运行 QEMU、改固件、改 ready、发送消息或实现 DSP HLE。结论是：**可进行一个严格限定的原 UI 消息服务实验；现有证据还不足以实现经过验证的 DSP 启动协议模型。** 实验执行与结果由 `menu-smp/ui_service_probe.py` 及主 runner 单独记录；本目录的静态合同不能代替那些运行证据。

原程序身份为 `s5-v29-offline/unpacked-v29/03_program.bin`，SHA256 `fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe`。这里所有地址都是原程序低地址映射。Thumb 函数地址在调用时加 1；指令断点使用偶地址。

## 严格限定的消息

既有 `menu-smp/results/isolated-display-gradient/ram.bin` 中，原 GUI 已经向 queue `0x76` 发出 20 字节请求，五个小端 word 为：

```
{ 0x115, 0x11d, 0x36, 1, 0 }
```

这是实际排队请求，不是探针构造的消息。原 task66 入口 `0x0675ef84` 先清零 `0x0a603680` 起的 `0xe08` 字节上下文，然后调用 `0x0676ede0`；其内部等待尚未运行的 DSP，尚未到达正常 receive loop `0x0675efbc`。本实验仅在 task66 的原等待上下文中临时调用其原 receive 和 dispatch；随后恢复等待。不将初始化标成完成，也不称正常启动已通过。

## 接收与所有权

动态 queue object 是 `u32(0x08e1b94c + (queue_id - 1) * 4)`。原底层 `0x06722c60` 使用以下布局：

| 偏移 | 已证用途 |
| --- | --- |
| `+0x04` | 容量，以指针项计数 |
| `+0x0c` | 消息指针数组地址 |
| `+0x10` | 写索引 |
| `+0x14` | 读索引 |
| `+0x18` | 当前项数 |
| `+0x1c` | 等待接收任务链 |
| `+0x20` | 等待发送任务链 |

正常数组队头是 `u32(u32(object + 0x0c) + 4 * u32(object + 0x14))`。`+0x08` 不能当队头使用。底层先处理 `+0x20` 的等待发送者，因此本次审计的数组路径要求该字段为 0。

快照实际值：queue `0x76` object `0x08e1a08c`，容量 56，数组 `0x09aec448`，读索引 0，项数 1，等待发送者 0，队头 envelope `0x099cab3c`，其五个 word 完全匹配上述请求。`u32(envelope-4)` 与 pool6 object 都为 `0x08e1c2cc`，具备原释放函数检查的分配前缀。

调用原 `0x067ea67b`：`r0=0x76, r1=0x0a603680, r2=0`。它非阻塞接收，成功返回 0，空队列返回 -2，其他错误返回 -1。成功路径先复制 20 字节，然后调用 `0x065a2be8(pool=6, envelope)` 释放接收节点，最后返回 0；wrapper 忽略释放函数的返回值。探针必须检查收到的 20 字节仍是 exact tuple，**不能再 free envelope**。本请求最后一 word 为 0，所选 handler 没有额外 payload cleanup。

## 原 dispatch、mode1 与真实回复

调用 `0x0675f155(r0=0x0a603680)`。所选路径如下：

1. `0x0675f15a` 读取 `u16(message+0x10)`，0 走 `0x0675f18a → 0x0675eed2`。
2. `message+4` 的 source `0x11d` 匹配 ROM `0x06f492b0`，调用 `0x0675eb8e`。
3. `0x0675eba4` 的 TBB 表位于 `0x0675eba8`；code `0x36` 的表项字节 `0x69`，故目标为 `0x0675ec7a`，调用 `0x0653b7e6`。
4. `0x0653b7e6` 读取 mode `message+0xc=1`。它也经 `0x0653efea` 读取普通 RAM byte `0x08a9da70`，但这个 byte 无论零或非零，mode1 都到 `0x0653b814`。其他 mode 的外设/初始化路径不能套用本结论。
5. `0x0653b814` 设置 `r0=0x36, r1=1, r2=0`，尾调用原回复 helper `0x06759710`。
6. 所选 dispatcher 尾部 `0x0675eec8` 返回 1。原 task loop 看到非零立即继续 receive；这里没有额外 cleanup。

回复 helper 自己选择 `0x08e9f518 + old_index*20` 的静态 ring 槽，index 在 `0x08aab9d4`，递增后 `&0x7f`。它自行写入 `{0x76, 0x42, 0x36, 0, 1}`，发送到 queue `0x115`。这些字段由原 ROM 常量和原 handler 参数生成，**实验无需也不应构造回复**。

`0x06680752/0x066807d4` 只保存/恢复本核 IRQ mask 并调用 DSB leaf。原 `0x065477bc → 0x068f8db8` 根据本核 system-state 判断任务/中断上下文。任务路径 `0x0657b7f4` 和中断路径 `0x065968d4` 都最终调用 `0x066f254c`，timeout 0。该函数直接交付/排入消息指针，不复制 ring 内容，也不另分配 envelope。后续消费和清理由原接收方决定，探针不能补做。

**dispatch 返回 1 仅表示识别并处理了请求，不代表发送成功。** 必须在 `0x06759766` 观察发送返回 `r0==0`。非零时原 helper 会调用 `0x06666240(error=0x100f)`；该 case 应停止，不能直接改错误返回或自行补 ACK。

对这个 exact mode1 路径，已检查的分支和 helper 没有 DSP 共享 RAM、ready/magic、外设 MMIO 或 DSP 初始化结果依赖。它依赖已运行的 RTOS 队列、pool6、原回复 ring、实际 task66 上下文及正确 SMP 锁/调度。dispatcher 还读取 task66 上下文附近 `0x0a604474` 的两个 word，但本选中分支不会使用这些加载结果。静态合同支持受限实验，不等于尚未执行的运行验证。

## 执行边界

- 单独 opt-in case，保留真实启动 baseline；仅服务这一个已存在的请求。
- 停在真实 task66 的 `PC=0x065491ee`；确认 current TCB，且 `u32(0x209c0 + cpu*4)==0`。不要把 IRQ vector 中的现场当 task 上下文。
- 保存 GPR、CPSR、SP、LR，在原任务栈按 8 字节对齐调用。允许原调度和必要的其他核运行，不能通过冻结锁持有者形成假死。
- 读取 live queue 布局并检查边界、计数、等待发送者、完整 tuple 和 pool6 前缀；receive 仅一次，timeout 0。
- receive 返回 0 且复制值匹配后，才进入原 dispatch；检查真实发送返回 0，及 dispatch 返回 1。
- 只有原调用正常返回后才恢复保存的 CPU 上下文到原 DSP 等待点。保留原代码产生的 queue/ring/调度副作用，不能整体回滚 RAM。
- 若调用超时、异常、发送失败或收到不同消息，停止独立 VM；不要在半完成的 RTOS 操作后强行恢复寄存器继续。
- 不写 ready、magic、共享指针、UI ackbyte、event 或 TCB，不直接调用最终 ACK handler；不把这一局部服务等同于 DSP 执行或完整开机。

## DSP 启动协议 HLE 仍缺什么

ARM 消费端给出了可复核的结构检查，但不足以建立完整服务语义。原 `0x065491e4` 上传/释放 peer 后等待 `7b004000!=0`；从 `+0xc/+0x10` 取 word-address，乘 2 加 `0x7b000000`。它检查第一结构 `+0x68` 的 `0x1234`，第二结构 `+0x160/+0x3208/+0x40e8/+0x4370/+0x4524/+0x79f0` 的六个 magic。原 ARM 配置函数 `0x0652e8c0` 写第一结构 `+4..+0x34` 的硬件地址，随后 ARM 写 `7b004004=1` 并等待 `7b004008!=0`。

lane 归一化 peer 中，第一、第二结构候选 word base `0x2100/0x2138` 与这些 magic/偏移一致；候选代码还会读取 ARM 配置并在多个未知子调用之后写 phase2 ready。这是结构对应的强证据，但候选 ISA 仍未识别，load/store、控制条件、调用约定、地址空间及未知子调用语义没有得到一套真正 decoder 的验证。

因此尚缺：

- 第一阶段 ready 的确切语义和 producer 时序；它可能不是布尔值。
- 两个共享结构所有非 magic 字段的 ownership、初始化值、有效范围和更新顺序。零填不是已验证默认值。
- phase2 对 ARM 配置的实际硬件动作，以及候选调用 `0x6985/0x3b72/0x4782` 的效果和错误状态。
- 后续 shared command、completion、异步事件/中断、缓冲区生命周期及失败恢复合同。

仅写主机期待的 magic/ready 会绕过这些缺口，不能叫验证过的协议模型。当前采用的单条原 ARM 消息服务实验无需作出这些假设。

## 复现

从项目根目录运行：

```sh
s5-emulator/lab/.venv/bin/python -B s5-emulator/qemu/peer-isa-analysis/protocol-feasibility/audit_ui_service.py
```

生成 `ui-service-contract.json` 和 `ui-service-disassembly.txt`。脚本核验原程序 SHA、ROM 常量、TBB 精确表项以及既有快照的 queue/分配前缀。`arm-protocol-disassembly.txt` 是早期宽范围研究记录，含线性越过 literal/table 的片段；不要把其中数据误解码当证据。精确 UI 合同以新的选中基本块文件为准。

## 后续 command 0x2b：不发送 ACK

独立离线审计 `command-2b-contract.json` / `command-2b-disassembly.txt` 对应 exact tuple `{0x115,0x11d,0x2b,0,0}`。复现为同目录 `audit_command_2b.py`，仍用 `lab/.venv/bin/python -B`，不连接客体或改 allowlist。

在 `local-ui-service-first/ram.bin` 的真实 queue76 中，队头为 command35，下一条才是 command2b；两者均有正确 pool6 分配前缀。不能跳过前一条去服务后一条，应由未来实验重新检查 live 队头。

2b 的通用 receive / type0 / source11d 路由与上述合同相同。TBB 项 `0x0675eba8[0x2b]=0xed` 跳到 `0x0675ed82`，该指令直接跳到 `0x0675ede8`。整个 body 只有三条指令：

```
0675ede8  LDR  r0, =0x090c92e8
0675edea  STRB r5, [r0, #0xc6]   ; r5 在 0675eb94 已置 0
0675edee  B    0675eec8          ; return handled=1
```

确切原副作用是将普通 RAM `byte[0x090c93ae]` 清零，没有函数调用、MMIO、DSP 共享区/ready 依赖或 ACK 发送。后面的 dispatcher 和原 task loop 不做额外 cleanup；接收节点仍由原 receive 内 pool6 release 处理。字段的更广应用含义尚未识别，不能因值为零就称硬件状态复位。

该快照中 `0x090c93ae` 已为零。因此未来验证必须记录原选中 body 的实际执行和函数返回；只读到零不能证明 handler 跑过。这条请求也不会直接补 command35 的模式确认 ACK。此次仅交付静态合同，没有执行 2b 或扩展局部服务白名单。

## 原 worker 的 command 0x40001

`audit_command_40001.py` 生成 `command-40001-contract.json` 和选中基本块
`command-40001-disassembly.txt`。真实队头 `{0x7f,0x4a,0x40001,0x08aab920,1}`
由原 worker `0x0651e662` 在固定静态槽 `0x08e9dac4` 构造；它不是 pool6 分配，
其前缀是零。原 receive 仍执行 pool6 release，ownership 检查返回 `-17`，
原 wrapper 忽略这个值后返回接收成功。这只支持该精确原静态槽的例外，不能
把任意无前缀指针视为有效消息，也不能额外 free 静态槽或 payload。

选中原 handler `0x066d13b0(9,1)` 仅写 `byte[0x08aac2f9]=1` 并返回。
没有 payload 解引用、ACK、MMIO 或 DSP 共享结构依赖。上层返回 handled=1，
不表示整个 mode35 已完成。root 的 `local-ui-loop` 后续实跑已由原 loop 消费
此响应和 2b，仍等待后续 frame 服务。

## 后初始化 gate 与一次性定时器

`postinit-frame-gate-evidence.json` 与 `postinit-frame-gate-disassembly.txt`
记录原 IRQ192→`0x065b9c3a`、IRQ193→`0x065b9e1a` 的入口，以及 gate vector
`0x08aa02c8` 五项来源。原 postinit `0x065b9fcc` 设置 `0x08aa02af` 仅满足其中
一项，不会自动执行这两个中断服务。第一项的 latch 又依赖原定时上电序列的
`0x08aa7f19`，由原 table callback `0x06759460` 设置。

调查发现 QEMU 9.2 的 one-shot 到期后写 LOAD 不重新启动内部 ptimer；原序列
后续阶段正好只写 LOAD。修复以 `load-rearms-oneshot` opt-in 限定在 s5-rtos，
有官方 SP804 手册、合成前后对照和 root 原程序 `local-timer-rearm` 动态验证。
该 run 中原 index 自然到 10、ready 自然为 1；没有人工设置 ready。
192/193 的真实物理时序及图像缓冲区合同仍未知，新增接口只提供默认关闭的
显式 TEST GIC 边沿，不声称传感器或 DSP 已正常执行。

## 0x7b1934xx 发送完成轮询

`audit_serial_tx.py` 生成 `serial-tx-contract.json` 和 `serial-tx-disassembly.txt`，
核验原程序 SHA、两套发送函数和真实批次的 157 条原始 record。原停止在索引144，
地址 `0x078d`、数据0；对应 outgoing bytes `81 07 8d 00`。原循环有 `0x10000`
次计数上限，超时累计返回1并继续下一 record，不能把短 host watchdog 当死锁。

后续默认缺席对照 `local-frame-paced-menu-timeout10` 让原超时自然完成，mode35
ACK 也由原任务正常发出。可选的 `tx-serial-hle` 因此只用于发送完成生命周期实验
及减少轮询开销。其独立验证 `qemu/serial-tx-hle-verified/verification.json` 运行
原 `0x067091f2` 和 `0x067093de`；并非直接跳过函数或设置 GUI 确认位。
发送槽记录真实 bytes，完成后只由原 ACK 清除。它不模拟外部器件接受、RX、像素
或 DSP 指令，且默认关闭。合同中的 `possible_hle_boundary` 是实施前的离线
建议；实际模型与验证以 `qemu/s5-rtos.c`、上述结果和机型 README 为准。
