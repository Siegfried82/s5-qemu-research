# 原厂 RTOS → CPU3 Linux 交接

**已验证原厂交接链可以在当前四核机器中启动 CPU3 Linux，并运行原始用户进程。** 本目录的工具只读已有二进制与 RAM 快照，没有启动 CPU3，也没有修改固件、QEMU 或启动脚本。实际运行由 `menu-smp/boot.py --linux-peer` 完成。

实验仍暂缓原厂 task66，使用已有外设模型、只读存储 HLE 和图形 HLE；它不等于真实相机整机启动完成，用户进程存在也不能证明所有 RTOS/Linux 通信已完成。菜单尚未由这项分析验证。

## 已发生的原厂交接

基线 `menu-smp/results/isolated-display-gradient` 保存了这些相互吻合的证据：

- 原厂 `0x0644ce34 → 0x0644cd28` 读取 zboot、原始 DTB、zImage、rootfs1。成功后 `0x0644ce54` 取入口 `0x80120000`，调用 `0x0644cf44`。
- `0x0644cf48` 将入口写入共享 RAM `0x80128000`；随后以 `r0=2,r1=3` 调用 `0x0658baa8`。原厂 IRQ 表 `0x06ff1240[2]=2`，最终 `0x0643616c` 给 GICD_SGIR 写 `0x00080002`，即 SGI2、目标 physical CPU3。这不是尚未模拟的复位控制寄存器。
- 基线 RAM 的 `0x80128000` 已为 `0x80120000`，GIC 的 SGI2 pending mask 已为 `0x8`。但 CPU3 仍在人工 `0x10800` 自循环，GICC3 control/PMR 都为0，所以不会消费原厂请求。
- `27_zboot`、`28_dtb`、`29_zimage` 的 RAM 字节与原组件完全相同。两个 rootfs 仅末尾4字节由 `ffffffff` 变成 `90020000`；原厂 `0x0644cd7c/0x0644cdaa` 在加载成功后自行写版本 `0x290`，其余字节全同。这是已解释的缓存版本标记。

原厂等待端在 loader3 的 ARM `0x330` 分派到 CPU3 `0x370`：先把邮箱设为 `ffffffff`，在 `0x380` 执行 WFI，醒后读取邮箱，非 `ffffffff` 时由 `0x3b4` 跳往发布的入口。这条 CPU3 路径不使用栈。其 `STRB ff,[70019f10]` 仅清 CPENDSGIR0 的 byte0，不能误称为已确认清除 SGI2。

GICC3 初态有更早的直接来源：loader1 ARM `0x16c..0x19c` 对所有非0核计算 `0x70002000 + MPIDR*0x8000`，写 control=1、PMR=`f0`。loader2/3 继承这一状态。

后续运行在原 RTOS 发布前恢复该 GICC 初态，并从原 loader3 `0x330` 等待。CPU3 使用明确简化的 MMU/cache 初态，未强跳 zboot。**不能在已有发布快照上重新进入 `0x330/0x370`，因为这会把邮箱入口重新清成 `ffffffff`。**

## zboot、DTB、内存与 GIC

原 zboot `0x80120034..0x64` 检查 `(MPIDR & 0xf03)==3`，写 `*0x80200000=3`，设置 `r0=0,r1=3,r2=0x822ff000`，跳入 `0x82300000`。在此之前原代码关闭 MMU 和 data cache。其他核的 zboot 分支有另一个等待协议；RTOS CPU0/1/2 无需执行它。

原 DTB 只有 `/cpus/cpu@3`，reg=3；Linux 把它作为自己的 logical CPU0，物理 MPIDR 保持3。**不要套用独立 `s5.py` 演示的 cpu@0 与删除 cpu-offset 修改。** 原 DTB 没有 reservation 条目，声明 RAM `[0x80200000,0x83100000)`，47 MiB。

| 用途 | 物理地址范围（右端不含） | 当前低 RAM alias |
| --- | --- | --- |
| RTOS 实验 helper 页表 | `00100000..00104000` | 同地址 |
| zboot / 发布邮箱 | `80120000` / `80128000` | `00120000` / `00128000` |
| Linux 共享启动字 | `80200000` | `00200000` |
| Linux 初始页表 | `80204000..80208000` | `00204000..00208000` |
| 解压后的 kernel 文件 | `80208000..806a8620` | `00208000..006a8620` |
| kernel BSS（真实 printk） | `806a8620..806c028c` | `006a8620..006c028c` |
| 原始 DTB | `822ff000..82300000` | `022ff000..02300000` |
| zImage 组件 | `82300000..82580000` | `02300000..02580000` |
| 解压器初始 SP / heap end | `82554f90` / `82564f90` | `02554f90` / `02564f90` |
| rootfs1 | `83100000..83400000` | `03100000..03400000` |
| rootfs2 | `83400000..83e00000` | `03400000..03e00000` |
| RTOS 主组件起点 | `06280000` | 同地址 |

固定解压地址由 zImage `0x823000a0` 读取 literal `0x82300900=0x80208000`，不是 `0x80008000`。已知解压、页表、Linux RAM 分配范围不覆盖 `0x00100000` helper 页表，也不覆盖 `0x10800` 旧暂停代码。后续实际 RAM 快照还确认 helper 页表完整一致。未来未知 DMA/IPC 地址不由这项范围核对保证。

DTB 的 GIC `cpu-offset=0x8000`，原两个 reg base 为 `70001000/70002000`，长度均 `19000`。编译后的驱动 `c045f050..78` 以物理 CPU map 乘 offset，CPU3 使用 `70019000/7001a000`，现 `s5-rtos` 已映射。

不能套用标准 Linux 清空整个 distributor 的假设：本原厂 `gic_init_bases` 的 `c045f20c..25c` 逐项读既有 ITARGETSR，只有 target 含自身 CPU mask 的 IRQ 才写 priority=`a0`、ICENABLER。这里没有无条件重写全局 route/config。banked PPI/SGI 初始化仍执行；共享 GIC 与 IPC 的完整交互需以实跑核验。

## 实跑结果：linux-peer-loader

从 `menu-smp/results/linux-peer-loader` 的 RAM 解码出 **106 条原始 printk 记录**：

- `Booting Linux on physical CPU 0x3`，Linux 3.18.18，47 MiB RAM。
- `/cpus/cpu@3`、`mpidr 80000003`，只启动自己的1个 Linux 核；RTOS 其他3核保持独立。
- 原 zboot、zImage、解压 kernel 三个入口断点真实命中。
- squashfs 根文件系统在 device31:0 只读挂载。
- `cpu_iccdrv` 与 `icsm` 原厂通信模块加载，最后日志时间1.112秒。
- 有 `unable to open an initial console`；这不表示 init 未执行。未伪造串口控制台。
- 最终 CPU3 PC=`c0016cc8`，符号为 `cpu_v7_do_idle+8`；GICC3 enabled，核处于正常 WFI idle。
- 原 DTB 在 RAM 中完整一致，helper 页表在 RAM 中完整一致。

只读遍历原 Linux process list，逐项验证 next/prev 链并闭环，共36个进程。字段来自原机器码：tasks=`+17c`、comm=`+2d8`、pid=`+1f4`。其中有：

| PID | 原进程名 |
| --- | --- |
| 1 | init |
| 309 | nwa |
| 321 | net |
| 322 | picm |
| 323 | link |
| 324 | psm |
| 325 | lvdp |
| 326 | nws |
| 327 | bt |
| 328 | linuxutil |
| 346 | getty |

除 idle 外所见 process state raw 均为1。init 的保存调度返回点为 `sys_pause`；net/getty 为 `do_nanosleep`，nws 为 `schedule_timeout`，多个服务为 `schedule_hrtimeout_range_clock`。这里只报告一个已验证保存返回地址，**不把栈内残留地址扫描当成完整回溯或已完成的通信握手**。

## 重建证据

```sh
s5-emulator/lab/.venv/bin/python s5-emulator/linux-smp-analysis/inspect.py
python3 s5-emulator/linux-smp-analysis/inspect_runtime.py
```

`inspect.py` 核对原组件 SHA-256，提取原 DTB、地址边界、基线邮箱/GIC/加载一致性并输出原指令。`inspect_runtime.py` 解码已有 SMP 运行的 printk、process list 与 helper/DTB 完整性。二者不会运行 guest。

主要产物：`evidence.json`、`*.disasm.txt`、`linux-peer-runtime.json`、`linux-peer-kmsg.json`、`linux-peer-kmsg.txt`。完整相机启动仍受 task66 协处理器、其他未建外设与 GUI 状态条件限制；本项没有得到真实菜单图像或原厂 JPEG codec 输出。
