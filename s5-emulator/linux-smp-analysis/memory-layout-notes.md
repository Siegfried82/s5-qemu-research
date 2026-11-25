# mode35 大缓冲区地址与原 loader 映射

结论：本次高地址来自原厂声明的第二个 1 GiB 地址区域；**原 loader 页表不会把它折回低 512 MiB**。当前实验的 512 MiB backing 不覆盖该区域。可以据这些证据建立明确标注的 2 GiB 软件内存实验，但不能据此宣布实机安装了 2 GiB DRAM。

本目录只分析原文件和 `menu-smp/results/local-ui-mode35/ram.bin`，未修改 QEMU、boot、graphics 或原固件。

## 原 loader 的真实页表

`02_loader3.bin` ARM `0x4cc..0x10cc` 包含64个固定循环，每个循环写256个1 MiB section descriptor。`memory-layout-inspect.py` 从原机器码解码并检查全部循环，再重建16,384次 descriptor 写入；不是猜测地址公式。

四张表分别位于 `86270000/86274000/86278000/8627c000`，内容完全相同，SHA-256：

`d5ccd3e7eb5171677dae62a78c021285134f1f48f20e198eb94adb84ba585fd5`

| 虚拟区间 | descriptor = section base + | 地址结果 |
| --- | --- | --- |
| `00000000..3fffffff` | `15c06` | 恒等映射 |
| `40000000..7fffffff` | `2002` | 恒等映射 |
| `80000000..ffffffff` | `11c02` | 恒等映射 |

原 `0x4b0` 设置 TTBR0，`0x4c0..4c4` 设置 DACR=`ffffffff`；CPU0 使用第一张表，其他核在 `0x28c..2a8` 根据 MPIDR 加 `core*4000`。随后原代码将 SCTLR 的 MMU/data cache 位开启。这里记录原 descriptor 属性原值，不额外推断所有继承的 SCTLR/cache 状态。

因此 `fd159000` 的原页表输出仍是物理 `fd159000`，`ffa91000` 仍是物理 `ffa91000`。当前 synthetic 页表在这些项也是恒等映射；差异不是地址转换到低 RAM。root 的实时故障寄存器另已报告 DFAR=`ffa91000`、DFSR=`808`，属于写外部 abort，吻合缺失物理 backing；本只读 RAM 文件不包含 CP15 故障寄存器，未把该外部报告当成本脚本读取结果。

## 原厂地址转换表

原 program `0x06ff11b0` 的四行，每行四个 u32：

| size | field1 | field2 | field3 |
| --- | --- | --- | --- |
| `40000000` | `00000000` | `00000000` | `80000000` |
| `40000000` | `40000000` | `0000dead` | `c0000000` |
| `0` | `0000dead` | `0000dead` | `0000dead` |
| `0` | `0000dead` | `0000dead` | `0000dead` |

保存的运行时表与原文件完全一致。原 `0x06680838..0x0668089c` 检查 source 地址是否落在某行的 `[source,source+size)` 内，排除 `DEAD`，再把相对 offset 加到目标列。`0x066808c6` 指定 source=field1、destination=field3，所以：

`7d159000 - 40000000 + c0000000 = fd159000`

第二行明确不提供 field2；这与只将低 `0..3fffffff` 作为可用低地址 alias、保留 `40000000..7fffffff` 现有 MMIO 的实验方案一致。它不支持 `address & 1fffffff`。例如原第二区域 `fd159000` 与第一区域 `9d159000` 必须保持不同 backing；折叠会破坏原地址空间。

## 分配与故障地址

kernel 分析已定位原 arena 初始化 `0x0646adf0..0x0646ae36`，基址 `0fc00000`、总范围 `703e0000`、末端 `7ffe0000`，以及固定块大小 `2e87000`。本脚本独立记录 RAM 的父 arena `09049728` 和分配描述符 `09051738`：

- 父 arena base=`7d159000`、size=`2e87000`。
- 当前分配 base=`7d159000`、size=`2938080`。
- 原 `0x0646bca2` 调用地址转换，获得 `fd159000`；减去128字节 guard 空间后，`0x0646bcc6` 向 `ffa91000` 写入原 base。

这里的加减运算正常，地址不是随机损坏。原初始化预留如此大的软件地址范围，并不等价于每个字节都对应已独立识别的 DRAM 芯片；未知硬件映射/控制器信息仍不得编造。

## 有依据的模型边界

- 保留默认512 MiB模型以便对照。
- 可选2 GiB backing 的完整高地址视图为 `[80000000,100000000)`。
- 低 alias 仅覆盖 `[00000000,40000000)`，对应高地址第一部分；`40000000..7fffffff` 不应被整块 RAM 覆盖。
- 对 `fd159000` 和 `ffa91000` 做真实 guest 写读验证，并确认 `9d159000/1d159000` 没被错误混成同一区域。
- 原页表、guard 写指令、分配器检查与 fault 分支均不需要修补。

本分析没有从 loader2/3 确认实际 DRAM 芯片容量或硬件 decoder 手册；只得到确定的软件地址范围、原页表、地址转换和故障缺口。完整2 GiB模型是否足以继续原 GUI，须以原代码复跑结果判断。

重建命令：

```sh
s5-emulator/lab/.venv/bin/python s5-emulator/linux-smp-analysis/memory-layout-inspect.py
```

产物：`memory-layout-evidence.json`、`memory-layout-loader.disasm.txt`、`memory-layout-conversion.disasm.txt`、四份 `memory-layout-original-pgd-*.bin`。所有修改均限于本分析目录。
