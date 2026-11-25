# S5 主程序启动探针

本轮实际执行了 S5 V2.9 主程序，从 ARM 入口 `0x06280004` 进入原厂初始化链，经过浮点初始状态和 RAM 属性问题、共享标志等待，最终到达串行控制器接收等待。**尚未到达 OSD 初始化断点 `0x06495ab6`，未启动真实菜单。**

所有测试都在本机 QEMU `none` 机器上进行，没有连接相机。没有修改原厂固件文件或跳过原厂函数。用来建立 VFP/MMU 状态的两段合成 ARM helper 位于另外的临时 RAM；后续清除共享标志、设置 ready 位的情况明确分为探索 case。

## 原厂代码身份与入口

- 原厂输入：`../../s5-v29-offline/unpacked-v29/03_program.bin`，16,674,816 字节。
- SHA-256：`fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe`。
- 入口：`0x06280004`，ARM 状态。开头读取 MPIDR，再初始化 CPU 各模式的栈，随后通过 BLX 进入 Thumb 运行库初始化。
- 地址依据：已有上游记录 `低地址Reset加载跳转CFG_20261119.md` 的启动交接点 `0x800003c4` 指向 `0x06280004`；历史记录还提供 SP=`0x13000`、LR=`0x3c4`、CPSR=`0x600001d3`。本次把这些值作为受限初始条件，不声称恢复了完整 loader 状态。
- 对照输入：已有 `~/相机功能验证/data/program_0x86280000.bin` 转储。它含运行时数据，不能当成与更新包逐字节相同的原厂组件。

## 实际执行结果

| Case | 实际结果 | 证据 |
|---|---|---|
| 原厂组件，默认 CPU 状态 | Undefined instruction；LR=`0x063bad86`，对应 `0x063bad84: vmsr fpscr,r0` | [results-original/result.json](results-original/result.json) |
| 运行时转储，默认 CPU 状态 | 同样停在上述 VFP 指令 | [results-dump/result.json](results-dump/result.json) |
| 重建 VFP 状态，保持 MMU 关闭，不使用数据 watchpoint | 越过 VFP 初始化，停在 Data Abort；DFAR_S=`0x08aa0929`、DFSR_S=`0x801`，故障指令为 `0x063e305a: str r1,[r5]` | [results-vfp-no-watchpoints/result.json](results-vfp-no-watchpoints/result.json) |
| VFP + 合成 Normal RAM 页表/MMU | 越过未对齐写异常，稳定等待 `0x063b0124..0x063b0138` 共享标志循环 | [results-vfp-mmu-normal/result.json](results-vfp-mmu-normal/result.json) |
| 上述条件 + 清除两个 peer 完成标志 | 进入串行外设轮询；`0x0642c458` 读取 `0x7f04300c`，等待 bit 0 为 1 | [results-peer-flags-cleared/result.json](results-peer-flags-cleared/result.json) |
| 上述条件 + 静态 status=3 | 发出了 3 次数据写入，但没有收到数据；最终在 `0x0642c500..0x0642c50c` 等待接收状态 | [results-serial-ready/result.json](results-serial-ready/result.json) |

原厂 VFP 指令未被替换。`enable-vfp.S` 通过正常 ARM 指令设置 CPACR 的 CP10/11 权限和 FPEXC.EN，再恢复交接寄存器从原始入口重跑。当前 QEMU 的 ARM CP15 GDB setter 不实现写入，因此没有把 GDB 的写命令误当成生效证据。设置后 CPACR_S 读回 `0x00f00000`，原厂 `vmsr fpscr,r0` 实际执行通过。

未对齐写异常发生时 SCTLR_S=`0x00c50078`，MMU 关闭。额外 case 在 `0x00100000` 放置明确合成的 1 MiB section 页表，RAM 使用 Normal non-cacheable 属性，候选外设区 `0x70000000..0x7fffffff` 保持 Strongly-ordered，开启 MMU 后 SCTLR_S=`0x00c50079`。这确实消除了该次未对齐写异常；它不是恢复出的原厂页表。

## 当前可复现的阻塞

共享标志循环读取 `0x000209c4`、`0x000209c8`，两个值均为 `0xf0f0f0f0`，求和非零就继续等。在探索 case 中只将两个字段清零，没有执行其他核的初始化工作。原厂代码随后离开这一循环。

离开同步等待后，串行函数 `0x0642c43c` 使用基址 `0x7f043000`：控制寄存器位于 `+4`、数据寄存器位于 `+8`、状态寄存器位于 `+0xc`。首次停在 `0x0642c458`，等待状态 bit 0。把状态静态设为 3 后，原始循环完成了发送计数 `r5=3`，接收计数仍为 `r4=0`；随后不断测试状态 bit 2 是否允许从数据寄存器读取，直到 3 秒界限。最后的数据寄存器值为 `0x00ff`，但这里的 RAM 不具备 FIFO 或外设回包语义。

因此下一项实质工作是确认该串行设备及其回包/状态变化，建立对应模型。继续把更多状态位固定为 ready 会引入没有来源的响应，不能证明菜单可用。所有当前结果的 `menu_booted` 都是 `false`；没有观察到 OSD 初始化断点，更没有把 callback 注册返回称为显示成功。

## 模型限制

- 单个 Cortex-A7，其他 CPU、核间通信、时钟、中断和显示设备没有完整建模。
- 同一输入分别载入 `0x06280000`、`0x86280000`，这两份 RAM 不是一致性别名。合成 MMU 使用 identity mapping，也未实现真实硬件的别名关系。
- 低地址额外 loader 数据、完整复位状态、真实页表和设备响应均未恢复。历史交接时 r1=`0x00c5187d` 可能与 SCTLR 状态有关，但当前证据不足以把它直接当作寄存器快照。
- 首轮使用大范围 GDB Z4 数据 watchpoint 时 QEMU 曾无法响应 GDB 中断和 QMP stop。移除这些 watchpoint 后，同一启动路径能正常停在明确指令/异常点。因此该无响应属于探针工具限制，**不计作固件故障或 SoC 阻塞**。脚本现在默认只用指令断点。

## 重跑

从 `s5-emulator` 目录执行；工具需要可执行 TCG 的本地环境：

```sh
python3 -B menu-boot-rtos/probe_boot.py --source original --output menu-boot-rtos/recheck-baseline

python3 -B menu-boot-rtos/probe_boot.py --source original --enable-vfp --enable-mmu --checkpoint 0x06495ab6 --seconds 3 --output menu-boot-rtos/recheck-barrier

python3 -B menu-boot-rtos/probe_boot.py --source original --enable-vfp --enable-mmu --release-peer-flags --serial-status 3 --checkpoint 0x06495ab6 --seconds 3 --output menu-boot-rtos/recheck-serial
```

`--checkpoint` 可设置一个原厂指令断点；`--steps-after-checkpoint` 可在命中后最多逐步执行 1000 条指令并保存轨迹。每个实例结束后关闭 QEMU。`result.json` 保存源摘要、初始/最终寄存器、CP15 状态、人工设置及其限制，`invocation.json` 保存实际启动参数。
