# 本地 QEMU 编译环境

QEMU 9.2.4 官方源码与依赖均保存在本目录，没有安装到系统目录。

- 程序：`build/qemu-system-arm`
- 源码：`qemu-9.2.4/`
- 编译依赖：`deps/`（macOS arm64）
- 构建记录：`build/build.log` 及 `build/smp-*-build.log`
- 官方来源及源码摘要：`source-manifest.json`
- Conda 依赖锁定清单：`dependencies-osx-arm64.txt`
- 附加 Python 依赖：`requirements-build.txt`

执行 `./build.sh` 会重新生成 boot helper、复制上一级 `qemu/` 中的 S5 板卡与 RTC 模型、幂等应用 SP804 扩展并增量编译。执行 `./build.sh --configure` 会重新配置。可以设置 `S5_BUILD_JOBS` 控制并行数。

`bin/pkg-config-utf8` 只修正旧版 pkg-config 对中文路径的逐字节转义，防止 Meson 解析其 UTF-8 输出失败。它仍调用本目录 `deps/bin/pkg-config`，没有更改系统 pkg-config。

此构建保留 QEMU 自带 ARM 板卡代码以复用外围设备模型，关闭图形、音频后端与大部分可选依赖。`s5-linux` 保留原来的单核 Linux 实验；`s5-rtos` 使用四核 Cortex-A7 与共享 RAM 别名。SP804 的 S5 bit4 脉冲扩展只在 `s5-rtos` 显式开启，标准行为保持默认。具体模型、实验假设、验证记录与边界见上一级 `qemu/README-rtos.md` 及主 README。
