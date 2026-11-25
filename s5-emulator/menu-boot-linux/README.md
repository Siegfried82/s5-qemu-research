# 原厂 Linux 启动与菜单服务核验

本目录实际运行了 S5 V2.9 原厂 Linux 启动流程、原厂 `rcS`、原厂 `nwa` 和原厂 `strace`。结果表明 Linux 启动脚本已经运行完，`nwa` 在 ICC 依赖检查处退出；没有运行出机身菜单。

全部试验使用离线 QEMU，无网络设备或真实相机连接；原厂内核、程序和根文件系统字节均未修改。`run` 模式的 PID 1、日志输送和 shell 包装命令是研究工具；`boot` 模式使用原厂 `/sbin/init`，但仍使用项目重建的 QEMU 引导环境。

## 真实运行证据

| 试验 | 观察 | 证据 |
| --- | --- | --- |
| 原厂正常启动 5 秒与 20 秒 | QEMU 和派生 DTB 摘要相同；各 108 条日志文本逐条相同，最后均为 ICC/ICSM v1.3.0 驱动信息 | [5 秒](results/stock-boot-5s-c44d3433/result.json)、[20 秒](results/stock-boot-20s-d403ab53/result.json) |
| 在研究 PID 1 下执行原厂 `rcS` | 脚本返回 0；约客体 0.54 秒时，`ps` 显示 `nwa` 已退出，两原厂模块均为 Live | [脚本与进程输出](results/stock-rcs-processes-a7660ca4/command-output.txt) |
| 直接执行原厂 `nwa` | 程序和动态库加载成功；`open("/dev/icc0", O_RDWR)` 返回 `ENXIO`，退出 1 | [原厂 strace](results/nwa-strace-304c2abe/command-output.txt) |
| 加载原厂 ICC/ICSM 模块后执行 `nwa` | 两次 modprobe 均返回 0；`/dev/icc0` 打开成功，`ioctl 0xc0085c03` 系统调用返回 0，但程序随后仍退出 1 | [原厂 strace](results/nwa-with-stock-modules-1af68219/command-output.txt) |

延长启动时间没有产生新的启动日志。这一点本身不证明所有进程都挂起；`rcS` 返回和 `nwa` 实际退出提供了更直接的证据。进程表中的 `nwa <defunct>` 出现在辅助 PID 1 环境，不能据此声称原厂 `/sbin/init` 也会留下僵尸进程。

## ICC 检查点

原厂 `nwa` 在地址 `0x120cc` 为后续调用准备 `CPU_Icc_Open(0xc001)`。原厂 `libicc.so` 的查询函数位于 `0x41bd09f4`，通过 `/dev/icc0` 发出 `0xc0085c03`，然后读取 8 字节参数结构偏移 `+4` 的返回字段。

`CPU_Icc_Open` 要求该有符号字段为正，才会继续打开 `/dev/icc1`。实际 strace 没有后续 `/dev/icc1`，而是直接退出；与反汇编结合，可定位到原厂库拒绝了返回的 ICC 查询数据。**目前没有直接读取该字段的具体值，也没有把它伪造成成功。** 系统调用返回 0 不代表核间业务状态已经可用。

后续需要真实主核/对端的共享状态与协议行为，才能继续判断网络、图传或菜单相关服务；不能仅靠强制 `nwa` 不退出就宣称菜单已启动。

## 保留的原厂菜单资料

- [`assets/MC801_allmenu.xml`](assets/MC801_allmenu.xml)：原厂 `/etc/menuxml/MC801_allmenu.xml`，307,714 字节，完整保留原始 CRLF。
- [`assets/menu-structure.json`](assets/menu-structure.json)：模型、版本、菜单分区、顶层项目、计数和语言清单。
- XML 标识：`model=S5`、`version=2.0`、`date=20201027`；包含 740 个 item、62 个 group、13 个 menu 和 11 种语言。
- SHA-256：`2cf69074d41ac134e9a65ca631cf4842cefc5c49d24cc855ac2094fdbd1e857c`。主机导出字节的摘要与客体原厂 OpenSSL 计算结果一致。

这份 XML 含 `camrply`、`cmd_mode=setsetting` 等协议字段，属于原厂远程控制/协议菜单数据。它不是机身 GUI 已经渲染的证据，本目录也没有用它自绘菜单页面。

`assets/nwa` 和 `assets/libicc.so` 同样由原厂客体逐字节导出，并与客体 OpenSSL SHA-256 核对，供上述窄范围依赖分析。动态库搜索中出现的许多 `ENOENT` 是加载器正常尝试的备用路径；最终所需库均成功加载，不能把这些备用路径误报为失败原因。

## 重复试验

从工作区 `相机` 目录运行：

```sh
python3 s5-emulator/menu-boot-linux/probe.py stock-boot-20s
python3 s5-emulator/menu-boot-linux/probe.py stock-rcs-processes
python3 s5-emulator/menu-boot-linux/probe.py nwa-strace
python3 s5-emulator/menu-boot-linux/probe.py nwa-with-stock-modules
python3 s5-emulator/menu-boot-linux/probe.py menu-xml-export
```

每次建立独立结果目录，保留 probe 类型、调用参数、原厂输出、退出状态、内核日志和 MMIO 记录。`probe.py --help` 可列出全部已有试验。XML 导出这一个试验临时将客体 printk 缓冲区设为 2 MiB，以完整保存原始数据；未改通用运行入口的默认设置。

机器可读结论见 [`summary.json`](summary.json)。这些结果只覆盖 Linux 侧启动与 ICC 检查，不覆盖主核 RTOS 菜单、传感器、拍摄或 JPEG 输出。

## GDB 数据观察点的独立对照

[`watchpoint_repro.py`](watchpoint_repro.py) 只运行三条新写的 ARM 指令：向普通 RAM 写两个字，再原地循环；没有加载任何 S5 固件。每个子试验另有 15 秒外层进程组强制超时。

[`验证 JSON`](results/watchpoint-repro-fe01f97c/summary.json) 记录了：无观察点和不命中的 `Z2` 均正常停在指令断点 `0x10008`，两次 RAM 写入逐字节正确；命中的 `Z2`、`Z4`（4 字节和 8 KiB 区域）虽然插入返回 `OK`，但继续执行和 GDB 中断均超时，最终需要 SIGKILL 结束 QEMU。首次 QMP `query-status` 仍能返回 `running`，因此不能说所有 QMP 命令立即失去响应。

这表明当前 QEMU/CPU 配置下的数据观察点问题可以完全独立于原厂固件复现。不能把这一超时直接归因于 S5 启动代码；后续真实启动诊断应先使用指令断点或其他证据。六次试验均已结束，并按记录的 QEMU PID 确认没有留下这些试验的进程。
