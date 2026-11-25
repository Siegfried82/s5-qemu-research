# S5 离线图像处理与 JPEG 输出

这里已经能生成可打开的 6000×4000 JPEG：主机生成合成图像，转换成实验所假设的 NV16 内存布局，将用户指定 BIN 内的实际 LUT 补丁交给 QEMU 执行，取回图像平面，再由主机 Pillow 编码 JPEG。它还没有模拟相机快门、传感器、ISP 或原厂 JPEG 编码器。

## 运行

双击上级目录的 `启动S5模拟器.command`，选 **7** 可从新建图像开始完整运行“真实 LUT → 原厂 JPEG 驱动 → 软件 JPU → 模拟内存导出 JPEG”。入口为：

```sh
python3 s5-emulator/capture/simulate_photo.py
python3 s5-emulator/capture/simulate_photo.py --input /absolute/path/to/photo.jpg
```

每次在 `results/photo-*/` 新建 `lut/` 和 `jpu/` 两阶段结果，最终 JPEG 为 `jpu/guest-output.jpg`；`result.json` 汇总检查。启动器实测记录为 `results/launcher-full-photo-check.txt`，完整运行结果为 `results/photo-20261124-200413-17ae09/`。此入口没有模拟机身菜单或快门任务。

在项目根目录执行：

```sh
python3 s5-emulator/capture/capture.py
```

依赖现有 QEMU 构建、`results/handoff-ui-check` 中从实际实验 BIN 提取并校验的输入、原始主程序转储，以及当前主机 Python 的 Pillow。脚本在 `capture/results/` 新建唯一目录，不覆盖旧结果。可用 `--output 新目录` 指定位置，或用 `--input 图像路径` 把自己提供的图像居中裁切缩放为 6000×4000；后一种仍是主机提供图像输入。

输出文件：

- `capture_off.jpg`：实际补丁开关关闭后的输出，由主机编码。
- `capture_lut.jpg`：实际补丁开关开启后的输出，由主机编码。
- `preview.jpg`：两张输出的缩略对比，标题明确标注 QEMU LUT 与主机 JPEG。
- `verification.json`：输入、固件、补丁、QEMU 和 JPEG 的散列及检查结果。
- `input-*.bin` 和两个 `qemu-*` 目录：实际输入平面、输出平面及运行记录。

首次实测结果位于 `results/first-capture/`：两个 JPEG 都重新解码验证为 6000×4000。关闭分支两个 24,064,000 字节平面逐字节不变；开启分支覆盖图像边角及固定随机点的 4,100 个像素对（8,200 像素）与独立 Python 整数插值参考完全一致。两个分支都保留全部 4,000 行的填充字节，恢复栈和应保留寄存器，并正常返回原调用点。完整帧都执行完毕；独立颜色参考仅验证上述抽样点。

NV16 CbCr、BT.601 全范围、图像描述 ABI 和缓存所有权仍是实验假设。主机负责图像生成、输入色彩转换、色度采样、预览转换及最终 JPEG 压缩；不把生成的 JPEG 当作相机原厂直出。JPEG 元数据也记录了此来源，没有伪造相机型号。

## 原厂 JPU 驱动的实际执行

```sh
python3 s5-emulator/capture/probe_jpu.py
```

结果位于 `results/jpu-driver-probe/verification.json`。探针先验证主程序转储的 `0x877f6600..0x877f6f04` 区段与原厂 V2.9 解密组件 `07_postboot3.bin` 逐字节一致，再在 QEMU 内调用三个原厂入口：

| 入口 | 实测结果 |
| --- | --- |
| `0x877f68ca` 输入平面配置 | 正常返回；将 Y 指针 `0x90000000` 写到 `0x7c157078`，C 指针 `0x92000000` 写到 `0x7c157084`，两路步长 6016、高度 4000 均正确。 |
| `0x877f6e98` 驱动启动入口 | 使用合成描述结构执行并返回，写出了输入、输出缓冲区和尺寸等寄存器值。 |
| `0x877f6ebe` 状态查询 | 返回状态结构 `[0, 0, 0]`。输出缓冲区起始 4096 字节保持探针填入的 `0xcc`，没有 JPEG 输出。 |

这里的硬件地址由普通 RAM 承接，尚未实现 JPU 设备；上层描述结构也是根据代码读字段构造的不完整实验输入，并非相机采集的有效编码任务。驱动返回只能证明这些 CPU 指令和写寄存器动作执行了，不能证明硬件开始或完成压缩。探针没有注入完成中断或伪造编码结果。

当前后续工作点是建立真实编码任务的描述结构、JPU/CDU 寄存器语义、状态/中断及 DMA 模型；这部分也需与整机调度和真实菜单启动分开验证。

## 最小软件 JPU 设备：已产出 guest JPEG

在保留上述无设备基线的基础上，`probe_jpu_model.py` 增加了一个明确使用 Pillow 替代压缩硬件的软件设备。它通过调试器截获原厂驱动的实际启动写入，读取驱动写出的寄存器，编码输入，再将结果写回 guest 内存，最后调用原厂状态查询代码读取结果。

```sh
python3 s5-emulator/capture/probe_jpu_model.py
# 可使用 capture.py 的另一份已通过验证的输出：
python3 s5-emulator/capture/probe_jpu_model.py --capture-dir 已验证输出目录
```

默认使用 `results/first-capture/` 中经 QEMU 执行实际 LUT 补丁得到的两幅图像平面。成功实测在 `results/jpu-software-model-v2/`：

1. 未修改的原厂 `0x877f6e98` 驱动配置运行到 `0x877f6856` 启动序列，再到 `0x877f671c` 寄存器 store 指令。调试器确认 `r1=0x7c156004`、`r3` 的 bit 0 为 1，然后单步执行原厂写入并回读确认。
2. 软件设备完成之前，原厂 `0x877f6ebe` 返回的状态数组是 `[0, 0, 0]`，输出区起始哨兵保持不变。
3. 软件设备从原厂写出的 MMIO 值读取 Y/C 地址、步长、宽高及输出区边界，取出 LUT 后的 6000×4000 NV16 图像。Pillow 编码产生 806,097 字节 JPEG，再写回 guest 输出区。
4. 软件设备按已反汇编的状态读取规则更新完成寄存器和长度计数；没有直接写相机状态输出结构。原厂状态函数实际执行并返回 `[11, 806097, 0]`。
5. 按原厂返回长度从 guest 输出地址导出 `guest-output.jpg`，逐字节比对软件设备写入值，并重新解码验证为 6000×4000。`preview.jpg` 是该 guest JPEG 的缩略图。

模型实现的读取规则来自原厂代码：`0x877f6940` 同时检查 `0x7c157808` 的 bit 1 与 `0x7c15603c` 的 bit 6，成立时返回 `0x0b`；`0x877f6d88` 将 `0x7c156050/2c/30/34` 四个寄存器的低字节按高位至低位组合，并减去 14；`0x877f6ebe` 将长度和 `0x7c15704c` 的 bit 0 写入状态结构。因此软件设备将长度计数写为实际 JPEG 长度加 14。这只是所观察到的固件读取 ABI，未验证真实硬件计数及额外 14 字节的含义。

这条链路已经跑过原厂驱动、guest 输出缓冲区与原厂状态查询，但压缩算法、质量及量化表由主机 Pillow 提供。它是调试器介入的单任务同步软件设备，还没有接入 QEMU 的原生 MMIO 设备框架或中断控制器，也没有启动菜单/快门流程。合成编码描述结构不包含真实相机任务的完整参数。

第一轮 `Z2` 数据写 watchpoint 在本次 QEMU 运行中未能及时返回，连中断和 QMP stop 也超时，随后探针清理了进程。该尝试目录 `results/jpu-software-model/` 保留。成功版本使用精确指令断点和单步，不依赖该数据 watchpoint；这次失败不构成对所有 QEMU watchpoint 支持情况的结论。
