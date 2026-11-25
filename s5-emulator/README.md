# S5 固件研究工具：可运行的第一版

本机已构建并验证：**原厂 S5 V2.9 Linux 内核在专用 QEMU 机型中启动，读取原厂文件系统，并运行原厂 Bash、cat、ls 等用户态程序。** 另附真实固件函数和 LUT 补丁的局部执行工具。

这是一套离线研究工具。已增加“合成图像 → QEMU 执行实际 LUT 补丁 → 电脑编码 JPEG”的可运行管线，以及真正执行三个 RTOS 核心的新 `s5-rtos` 机型。多核握手、RTC 串行初始化和原厂 IRQ 处理已实际通过；完整相机菜单、原厂快门流程、视频、传感器、JPU/ISP 和主核完整启动仍未完成。Linux 命令测试不能证明主核补丁能在机身运行。

## 直接使用

在 Finder 中双击本目录的 **启动S5模拟器.command**，选择：

1. 运行原厂 Linux 演示，显示内核版本、根目录、Bash 版本。
2. 输入一条命令，在原厂 Linux / Bash 中执行。
3. 测试原厂图像描述读取函数及已有 LUT 补丁。
4. 校验原厂 48 个组件。
5. 测试用户指定的带菜单开关实验 BIN，执行完整 6000×4000 合成帧。
6. 生成合成场景，在 QEMU 中分别执行实际 BIN 的 LUT 开关分支，再由电脑辅助编码两张 6000×4000 JPEG。
7. 一键重新生成合成帧、执行实际 LUT，将输出交给原厂 JPEG 驱动，由软件 JPU 模型编码并写回模拟内存，再由原厂状态函数读取完成状态和长度，最后导出 JPEG。
8. 打开 S5 6K／5.9K 视频模块，进行源码／固件离线核对、模拟测试，或显式选择 Service USB 状态读取、菜单启用及恢复。
9. 执行原厂多核启动／菜单诊断，保存寄存器、设备状态和实际初始化记录；此项尚不显示菜单。
10. 执行原厂四核与 GUI 初始化实验：三个 RTOS 核心、由原核间中断启动的 Linux、原图形调用及软件按键队列；保存日志和初始帧，尚无可操作菜单。
11. 执行最新局部菜单启动实验：提供2GiB地址空间、TX控制器模型和有界测试中断。已提交原厂时区提示画面并完成模式切换，主菜单与按键响应仍在调试。各项HLE与测试输入都在记录中标明。

第 8 项是独立的主机 USB 模块，不是 QEMU 视频模拟。它按上游菜单方案接入 6K 3:2 与 5.9K 16:9 两种 23.98p 模式，已完成源码与离线验证，尚无本机实拍结果。说明见 [视频模块](../s5-video-unlocker/README.md)。命令行入口为 `python3 s5.py video --help`；`video status --sim` 不连接相机。

第 5 项已在 QEMU 中通过完整帧和函数返回检查，详细证据与范围见 `handoff/README.md`；这项使用真实主程序片段和合成内存，不是整机菜单启动。

第 6 项的首轮样片在 `capture/results/first-capture/`：`capture_off.jpg`、`capture_lut.jpg` 和并排预览 `preview.jpg`。两张 JPEG 均重新解码验证为 6000×4000。关闭分支的两个输出平面与输入逐字节相同；开启分支正常返回，抽查 8200 个像素与独立整数运算参考完全一致，全部行尾填充保留。最后的颜色转换与 JPEG 编码使用主机 Pillow，不是原厂 JPU/CDU 编码器。详见 `capture/README.md`。

第 7 项已从启动器完整实跑，导出 `capture/results/photo-20261124-200413-17ae09/jpu/guest-output.jpg`（6000×4000、806097 字节）。软件模型先观察原厂驱动实际写入启动位，再从驱动配置的寄存器读取图像输入与输出地址；软件编码结果写回模拟内存后，原厂查询函数自行读出状态 11 和正确长度。每次建立新的 `photo-*` 目录并保留两阶段记录。此模型以调试器断点衔接单次同步任务，使用 Pillow 替代压缩硬件，尚无原厂编码质量、完整 IRQ/DMA 模型或快门流程。

```sh
python3 capture/capture.py
# 也可将自己的图片送入同一真实补丁；电脑先裁切缩放到 6000×4000
python3 capture/capture.py --input /absolute/path/to/photo.jpg
# 完整图像实验，可省略 --input 使用合成场景
python3 capture/simulate_photo.py --input /absolute/path/to/photo.jpg
# 仅复跑原厂驱动/软件设备阶段，复用指定结果
python3 capture/probe_jpu_model.py --capture-dir /absolute/path/to/capture-result
```

命令行也可使用：

```sh
cd "/Users/lixiuqi/Documents/ChatGPT/相机/s5-emulator"
python3 s5.py run
python3 s5.py run --command '/bin/cat /etc/inittab'
python3 s5.py run --command '/bin/cat /proc/cpuinfo; /bin/ls /lib'
python3 s5.py run --command '/bin/cat /proc/mounts' --seconds 5
```

每次命令启动一个新的模拟环境，默认运行 3 秒，然后保存输出并退出。原厂根文件系统以只读方式挂载；该版本是批处理入口，尚无持续交互的串口 shell。命令需要较长时间时可设 `--seconds`，范围 0.2–60 秒。原厂文件系统不包含所有常见 Linux 工具，命令不存在会如实失败。

本机所需程序已经安装在此项目内，无需另装 QEMU。当前构建适用于 Apple Silicon macOS；工具链含本地路径，移动整个目录后可能需要重建。终端需允许模拟器使用 JIT；受限执行沙箱中的原生退出与客体固件错误会分别记录。

## 已验证的结果

`results/final-check/demo/command-output.txt` 是最终版本的实际执行输出，其中包含：

```text
S5_USERSPACE
Linux version 3.18.18 ... #1 SMP Thu Jun 12 14:54:26 JST 2025
bin
dev
etc
...
bash=3.2.57(1)-release
```

- `results/final-check/demo/result.json`：原厂内核启动、根文件系统挂载、命令完成，退出码 0。
- `results/final-check/long-output/`：一次 4097 字节输出被逐字节保留；输出中的 `Kernel panic` 和伪造结束标记不会干扰状态判断。
- `results/final-check/exit7/result.json`：执行 `exit 7`，正确记录客体退出码 7，主工具返回失败。
- `results/final-check/timeout/result.json`：先输出伪造结束标记，再无限循环；正确记录超时未完成并返回失败。
- `results/final-check/verification.json`：上述四项实际端到端检查全部通过。可运行 `python3 tools/smoke_test.py` 复验。
- `results/gic448-smoke/kernel.log`：原厂启动流程运行到加载 ICC / ICSM 模块；这不代表核间通信实际可用。
- `lab/results/default.json`：原厂 getter/memcpy 与补丁保护分支完整返回，120 像素 LUT 块通过。
- `lab/results/cross-row.json`：51 个 LUT 块跨行执行通过，输入 80/100/150、输出 90/100/151。

## 保存的证据与调试

每次运行在 `results/` 建立独立目录：

| 文件 | 内容 |
| --- | --- |
| `command-output.txt` | 客体原厂 shell 的命令输出 |
| `command-output.bin` | 未经文本解码的原始输出字节 |
| `result.json` | 启动状态、客体退出码、QEMU 退出码和停止位置 |
| `kernel.log` / `kernel-log.json` | 从暂停后的客体 RAM 读取的真实 printk 日志 |
| `registers.txt` | 停止时的 ARM 寄存器 |
| `mmio.log` | 尚未模拟的寄存器访问，最多记录前 256 次 |
| `invocation.json` | QEMU 参数、二进制摘要、设备树摘要、命令 |
| `initramfs.*` | 该次运行的测试入口及构建摘要 |

原厂内核没有找到可用的内置 PL011 串口驱动，因此日志通过 QMP 读取物理 RAM。自编 `/init` 通过管道接收原厂 shell 输出，每次最多 384 字节，以十六进制记录写入真实 `/dev/kmsg`，绕开原厂单次 992 字节写入限制；主机按原顺序还原字节，不额外插入换行。每次运行使用独立随机标识隔离输出与状态，只有内核 facility 的 panic 消息会被判为内核崩溃。

日志缓冲区扩为 256 KiB，十六进制编码约占两倍空间。大量输出仍可能覆盖早期记录；若本次开始标记被覆盖，工具会记录 `command_output_truncated=true` 并返回失败，不把不完整输出报为完整成功。

```sh
# 使用原厂启动流程，保留 64 MiB 模拟内存供分析
python3 s5.py boot --seconds 5 --keep-ram

# 开放仅本机可访问的 GDB 端口，在指定时间内调试
python3 s5.py boot --seconds 60 --gdb 1234

# 从已有快照重新读取日志
python3 analysis/read_kmsg.py results/某次运行/ram.bin --ram-base 0x80000000

# 验证固件并重新导出内核符号
python3 s5.py verify
python3 analysis/inspect_kernel.py
```

`analysis/System.map` 已恢复 28,401 个内核符号，用于把停止地址转换成函数名。

## 模型范围与输入

输入来自同级 `s5-v29-offline/`。每次启动都会验证原厂更新包的固定 SHA-256、CRC 和全部 48 个明文组件的原厂表内 SHA-256；不依赖可编辑报告声称的“通过”。原厂内核和文件系统字节不修改。

专用 `s5-linux` 机型当前包含：

- 一个实际执行的 Cortex-A7 CPU；GICv2 提供四个接口、448 个 IRQ。
- `0x80000000` 起 64 MiB 模拟 RAM；内核使用原厂指定的 47 MiB Linux 区域。
- 原厂地址处的 GIC、SP804 定时器、PL011 串口模型。
- 两个原厂 SquashFS 镜像，分别载入 `0x83100000` 和 `0x83400000`。
- 明确的研究替代：时钟与 IPC 寄存器使用 RAM 后备区；其他未知 MMIO 读零、写入丢弃并记录。

为启动单核 Linux，派生设备树把物理 CPU3 改为 CPU0、去掉 GIC `cpu-offset`、禁用未实现的 SDIO，并扩大 printk 缓冲区。另有一小段自编引导代码预配置 GIC 路由和优先级；这段代码是重建的启动条件，不是原厂 BootROM。

`run` 额外载入自编 initramfs，挂载原厂 rootfs1/rootfs2 后执行原厂 `/bin/sh -c`。它避免进入依赖其他相机硬件的完整启动服务。无图像硬件、真实 SD 卡、USB 相机连接或 Flash 写入。

## 局部固件与 LUT 测试

```sh
lab/.venv/bin/python lab/s5_lab.py run
lab/.venv/bin/python lab/s5_lab.py run --case tile --tiles 51 --y 80 --cb 100 --cr 150
```

该工具使用 Unicorn 2.1.4，默认读取 `~/相机功能验证/data/program_0x86280000.bin` 及已有 LUT 实验产物，均先核对固定摘要。它执行真实指令，但 RAM、图像输入和停止条件由工具设置。此 Unicorn 工具只做局部测试；后续 QEMU 完整画幅和主机辅助 JPEG 测试见 `handoff/README.md`、`capture/README.md`。原厂 JPEG 编码、真实缓存与 DMA 行为仍未验证。详细说明见 `lab/README.md`。

## 菜单和原厂拍照流程的实测范围

`menu-boot-linux/` 保留原厂启动脚本、NWA 服务及原厂 strace 的实际运行证据。ICC/ICSM 模块已能加载，NWA 可以打开 `/dev/icc0`，但设备查询后被原厂库拒绝并退出；结合反汇编，阻点位于核间通道状态检查，具体返回字段值尚未直接读取。单独运行原厂 rcS 已正常结束；同一构建下 5 秒和 20 秒正常启动的 108 条内核日志逐条一致，不能把延长等待视作已进入菜单。

原厂 `/etc/menuxml/MC801_allmenu.xml` 已导出并以客体原厂 OpenSSL 校验摘要，其中有 S5 远程控制菜单定义。这是协议资源，尚未作为机身屏幕绘制，也没有把它做成仿造界面来代替原厂菜单。

`menu-smp/` 是最新主程序启动入口，详见 [多核菜单探针](menu-smp/README.md)。CPU0/1/2 实际运行原厂代码，通过一致共享 RAM 完成握手，不人工清标志；PL022/RTC、26路计时器及恢复的 loader GIC/向量让原厂 IRQ handler 实际执行。五路 PL011 已按原厂 IRQ 表连接。SPI0 未知外设默认遇首请求停止，另有明确的无外设／MISO=0 对照；该对照下 UART2 实际发出 `34 27` 两字节并按原厂超时继续。当前还没有菜单绘制，后续输入条件和阻点见各次结果。

新的 `--isolate-display` 局部实验通过原 RTOS 延迟 API，让仍在等待未知协处理器的 task66 暂时让出 CPU；原 AVIO 与 GUI 随后实际执行 EGL/OpenVG 初始化，并发出原路径绘制调用。软件后端按真实调用处理，不创建替代菜单。这个实验不代表协处理器已执行或完整整机已启动，且主机图形帧尚未同步回原生扫描缓冲区。原 `s5-linux` 的内核、rootfs、ICC/ICSM 加载回归仍通过。

进一步的 `--linux-peer` 已使第四核通过原 loader 等待、原 RTOS SGI2 和 mailbox 启动原 Linux，挂载原根文件系统并运行 nwa 等用户进程。实际 GUI 首次 swap 已导出，但只有初始化预热像素；软件按键也已被原队列消费。启动器第10项可复现这个基础实验，不会把初始帧标为菜单。

后续局部实验已解决原图像分配器的高地址异常、资源状态寄存器丢写和一次性定时器重装问题，原模式35切换也已完成。原厂时区提示已经真正绘制并提交；CENTER按下、释放实际确认了提示，进入时区选择状态。最新110次绘制、5次swap中，显示更新已走完真实IRQ及原任务通知，但时区页面仍被原镜头释放按钮告警覆盖，正在核对这一路串行输入。未知协处理器仍未执行；复现命令、HLE与TEST条件及记录见 [最新菜单实验](menu-smp/README.md)。

`menu-boot-rtos/` 保留此前单核探索：其中人工清除两个 peer 标志和静态 serial ready 值仅是历史诊断，不能用于新版本多核成功结论。

本构建的 GDB 数据观察点存在独立于固件的调试限制：三个合成 ARM 指令也能复现命中观察点后继续/中断超时，无观察点和不命中对照正常。已改用指令断点定位，不把该工具问题当作 S5 自身卡死；复现记录见 `menu-boot-linux/README.md`。

`capture/probe_jpu.py` 已实际执行原厂 JPU 配置、启动、查询函数，确认驱动写出输入平面地址、步长和尺寸。惰性 RAM 承接寄存器的基线没有生成 JPEG，也没有完成状态；驱动函数返回不等于编码完成。

## 重建

```sh
./toolchain/build.sh
```

QEMU 9.2.4 官方源码、固定来源摘要和项目内依赖保留在 `toolchain/`；自定义机型源文件为 `qemu/s5.c`、引导代码为 `qemu/board-setup.S` / `.h`。构建脚本将机型复制到 QEMU 源树后增量编译。测试入口由 `userspace/build_initramfs.py` 使用系统 clang 和 Python 生成，不需要 ARM libc 或交叉链接器。
