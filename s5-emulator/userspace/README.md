# S5 原厂 Linux 用户态命令环境

这个目录生成一个最小 ARM `initramfs`，供项目的 S5 QEMU 机型使用。它由新写的静态 `/init` 启动，挂载原厂 S5 V2.9 的两个 SquashFS 文件系统，然后在原厂文件系统内执行原厂 `/bin/sh -c`。

原厂内核没有可用的内置串口控制台，因此命令的 stdout、stderr 先写入 pipe，由父进程分块编码后通过真实 Linux `/dev/kmsg` 写入内核日志，再由主运行工具还原原始字节。这里没有预先生成或模拟命令输出。

## 已验证的运行结果

2026-11-24，项目自定义 QEMU 机型实际启动原厂 Linux 3.18.18，并成功运行：

```sh
echo S5_USERSPACE
/bin/cat /proc/version
/bin/ls /
echo "bash=$BASH_VERSION"
```

真实输出包含 `S5_USERSPACE`、原厂内核版本、根目录文件名和 `bash=3.2.57(1)-release`，最后返回 `S5LAB_END status=0x00000000`。

初版运行证据位于 [`../results/userspace-first/kernel.log`](../results/userspace-first/kernel.log)，同目录的 `invocation.json`、`kernel-log.json`、`ram.bin` 保存了调用参数、解析记录和运行时快照。初版直接记录命令文本；当前版本使用下述分块编码格式。此结果证明原厂 Linux 内核和这些原厂用户态程序在模拟环境中执行成功；它不证明相机主程序、菜单、拍照或图像硬件已经运行。

当前 pipe/token 版本的实际端到端结果见 [`../results/final-check/`](../results/final-check/)：正常命令成功；4097 字节的单次输出被完整保留；输出中的 `Kernel panic` 或假结束标记不影响判断；退出码 7 被准确报告；打印假结束标记后无限循环仍按超时失败处理。

## 构建一个命令包

在工作区 `相机` 目录中运行：

```sh
python3 s5-emulator/userspace/build_initramfs.py \
  --command 'echo hello; /bin/cat /proc/version; /bin/ls /' \
  --output /tmp/s5-command.cpio
```

也可以使用 `--command-file /absolute/path/commands.txt`。输入将原样传给模拟机内的 `/bin/sh -c`，不会作为主机 shell 命令执行。命令须为 1–4095 个 UTF-8 字节，且不能包含 NUL。原厂文件清单中没有 `uname`，可用 `/bin/cat /proc/version` 查看系统版本。

输出包括 CPIO 包、独立静态 ARM ELF 和包含 SHA-256 的 JSON 清单。构建只依赖 Python 3 和支持 ARM 汇编的 `clang`；无需额外链接器或 libc。`--clang` 可以指定编译器路径。

主运行工具为每次执行生成独立标记。手动构建可传入 `--marker-token 0123456789abcdef`：参数必须恰好为 16 个十六进制字符，内部统一为小写，记录在清单的 `marker_token` 字段。省略该参数时，保留不带 token 的标记，方便手动启动。

生成的包需配合本项目的 S5 机型、原厂内核、设备树和文件系统映像使用；它本身不是完整 Linux 发行版。主运行入口见 [项目说明](../README.md)。

## `/init` 的行为

1. 创建并打开 `/dev/kmsg`，父进程通过它记录状态，stdin 指向 `/dev/null`。
2. 只读挂载原厂 rootfs1 到 `/root`，原厂 rootfs2 到 `/root/mnt/rfs2`，挂载 `/proc`。
3. 创建 pipe 和子进程，将子进程 stdout、stderr 指向 pipe，`chroot` 到原厂 rootfs，执行原厂 `/bin/sh -c`。
4. 父进程每次读取最多 384 字节，转换为十六进制写入一条内核日志，避免超过原厂 `/dev/kmsg` 单次写入限制。
5. 读到 pipe EOF 后等待子进程退出，写入结束标记，然后保持暂停，等待主机运行工具停止 QEMU。

标记格式：

```text
S5LAB_BEGIN_0123456789abcdef
S5LAB_ROOTFS_MOUNTED_0123456789abcdef
S5LAB_OUT_0123456789abcdef 68656c6c6f0a
S5LAB_END_0123456789abcdef status=0x00000000
```

上面的输出块解码后是 `hello` 加一个换行。主机应按记录顺序拼接 hex 解码结果，不额外插入换行；命令输出中的同名标记也只是编码数据。`ERROR`、`WARNING` 同样带本次 token。无 token 时省略整个 `_<token>` 后缀。

`status` 是十六进制原始 `wait4` 状态。`00000000` 表示 shell 正常退出且退出码为 0。正常退出码为 `(status >> 8) & 255`，低 7 位表示终止信号。`S5LAB_ERROR_<token>` 表示父进程工具自身失败；这种情况下结束字段可能是负 errno 的 32 位表示，不能当作子进程退出状态解释。子进程启动失败会返回 127，并通过输出 pipe 提供错误文字。

## 范围与限制

- 构建工具只读取原厂固件，生成文件存放于指定输出路径。原厂两个文件系统在模拟机内只读挂载。
- 这个启动流程使用自建 `/init`，不运行原厂相机启动服务；需要专用硬件、核间通信或设备节点的原厂程序仍可能失败。
- 这里只提供通过日志返回结果的批量命令执行，不提供交互式串口终端。
- 主运行工具默认使用 256 KiB 内核日志缓冲区，其中还包含启动日志及十六进制编码开销，不能视为 256 KiB 的原始命令输出容量。若日志覆盖本次开始标记，主工具报告 `command_output_truncated=true` 并失败；大量输出应分批执行。
- 通过该环境运行的用户命令与原厂相机的真实硬件行为之间，仍需要分别验证。

`init.S` 使用 Linux ARM EABI 原始系统调用。构建器检查 ELF object，仅解析同一段内的 ARM 分支和绝对地址重定位；遇到不支持的重定位、外部符号或额外分配段会失败。token 在临时汇编源码中替换后重新汇编，因此字符串地址和状态字段偏移会随实际长度重新计算。
