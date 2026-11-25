# S5 固件局部执行工具

在合成 RAM 环境中执行真实 S5 V2.9 ARM/Thumb 固件指令。此工具使用 Unicorn，便于单独检查函数和补丁；它不实现整机 QEMU，不启动相机系统，也不连接相机。

## 安装与使用

从 `s5-emulator` 目录运行：

```sh
python3 -m venv lab/.venv
lab/.venv/bin/python -m pip install -r lab/requirements.txt
lab/.venv/bin/python lab/s5_lab.py inspect
lab/.venv/bin/python lab/s5_lab.py run --output lab/results/default.json
lab/.venv/bin/python lab/s5_lab.py run --case tile --tiles 2 --y 80 --cb 100 --cr 150
```

已有 Unicorn 安装目录时，可用 `--dependency-path /path/to/packages`，或直接使用安装了 Unicorn 的 Python。工具没有绑定临时目录。

默认读取 `~/相机功能验证/data/program_0x86280000.bin` 和同一工作区 `s5-v29-offline/lut-firmware-experiment/` 的既有实验产物；可以用 `--dump` 和 `--assets` 指定位置。运行前核对已知 V2.9 转储及补丁组件的 SHA-256、补丁载荷与组件内容、原调用和补丁调用目标。不匹配就拒绝执行。不会修改这些输入。

## 执行范围

- `getter`：从原厂图像描述读取函数进入，执行原厂 memcpy，完整返回到合成调用者，检查描述、状态码、栈、被调用方保存寄存器和图像未被改写。
- `guard`：从真实原厂调用位置执行已修改的分支，传入 5999×4000 描述，执行原厂读取和补丁保护分支，完整返回到调用者。
- `tile`：传入 6000×4000 合成图像描述，执行原厂读取、实验补丁及 LUT；完成指定数量的 120 像素块后，在下一次 LUT 调用前停止。默认 1 块，最多 1000 块。检查各块的输出、未处理像素及行尾填充；默认输入另与既有结果 108/125/131 比较。
- `all`：依次执行上述三种情形，默认选项。

`--timeout` 是每种情形的时间上限，默认 10 秒；`--instruction-limit` 默认 5000 万条。达到上限、内存访问错误或检查失败均返回非零退出码；JSON 写明原因和最后 PC，不会将提前停止标成成功。`--output` 保存与标准输出一致的 JSON，其中包含输入哈希、执行入口 trace、检查结果与限制。

每种情形在隔离子进程中执行，原生引擎崩溃也会生成失败结果。macOS 的受限沙箱可能禁止 Unicorn 所需的 JIT：本环境在沙箱内出现 SIGILL（信号 4），同样的固件和引擎在获准的本地进程中通过。遇到此情况请在允许 JIT 的终端运行；工具不会把引擎崩溃解释为固件失败。

## 证据边界

实际执行的是固定哈希的原厂代码和既有实验补丁。合成输入采用补丁所假设的 NV16 CbCr 布局；实际机身像素格式、DMA、缓存一致性、加载权限、完整画幅耗时和完整拍照路径仍未验证。`tile` 的成功意味着有限块执行达到了检查点，不能代表完整补丁返回或 JPEG 编码成功。任意自定义输入只检查实际输出的一致性和写入范围，不独立证明色彩转换正确性。

## 本地验证记录

- `results/default.json`：原厂 getter、补丁 guard 均完整返回；默认 120 像素块输出 108/125/131，所有检查通过。
- `results/cross-row.json`：输入 80/100/150，处理 51 块（跨越第一行，进入第二行），输出 90/100/151，未处理区域与行尾填充保留。
- `results/instruction-limit.json`、`results/time-limit.json`：主动设置过低执行限额，得到非零退出码和明确失败状态。
- `results/sandbox-failure.json`：记录受限 JIT 环境中的原生退出，顶层正常输出失败 JSON，没有把崩溃标为通过。

成功结果使用本目录 `.venv` 内的 Unicorn 2.1.4 生成；无需 `/tmp` 中的依赖。
