# 完整 S5 项目交接 · 2026-11-25

这是同一私有仓库的完整本地归档，补齐首次Git提交未包含的固件、二进制、工具链、内存快照、所有实验记录与外部依赖。源码进度仍以仓库README、任务A报告和各次原始结果为准。原固件/实验修改BIN同时存在，不能将标有UNVERIFIED的文件当作机身刷写验证通过。

## 获取与恢复

先用有权限的GitHub账号下载本Release的**全部附件**。它们在私有仓库中，未登录或未获授权的同事无法下载；仓库所有者需自行邀请同事。本次没有增加任何协作者权限。

Python 3.9或更新版本，从附件所在目录执行：

```sh
# 默认只验证全部分片、gzip CRC、文件清单和每个归档普通文件的SHA-256
python3 restore_handoff.py
# 验证并恢复到一个不存在的新目录；需要至少约25GB额外空间
python3 restore_handoff.py --output /absolute/path/S5-handoff
```

也可以按文件名顺序拼接` s5-full-handoff.tar.gz.part-* `再用tar解压，但推荐脚本，它会逐文件验证且拒绝覆盖现有目录。`SHA256SUMS`是分片与清单的校验表，`archive-manifest.json`给出精确总大小、文件数和来源映射。

恢复后包括：

- `s5-emulator/`：完整当前源码、构建好的QEMU、官方源码、依赖、venv、全部RAM/SRAM快照与历史输出。
- `s5-v29-offline/`：原始更新包、48个组件、解密/补丁与UNVERIFIED实验产物。
- `s5-video-unlocker/`、`s5-sd-loader-prototype/`：完整相邻项目及输出。
- `external/相机功能验证/`：原`~/相机功能验证/`的全部内容，包括局部工具依赖的program转储、s5_decrypt.py和panasonic-aes5。

没有把`github-private/`递归打包进去（它是这次上传的副本与分片，会造成自包含递归），也没有打包项目目录之外的个人账号配置、GitHub凭据或SSH私钥。上游QEMU源码附带的测试密钥属于原官方测试夹具。不能归档的运行时socket会列在manifest中，它们不是源码或可恢复实验数据。macOS系统框架与Xcode/CommandLineTools不属于项目文件，需在目标机器上安装。

## 接手机器的路径与环境

当前构建适用Apple Silicon macOS。CoreGraphics图形后端并非跨平台。原本机项目路径是`/Users/lixiuqi/Documents/ChatGPT/相机/`；工具链、venv、某些脚本、依赖前缀与符号链接保留原路径，**不承诺解压到任意目录立即运行**。

1. `s5-emulator/`和`s5-v29-offline/`保持同级。
2. 部分局部LUT/JPEG工具仍查找`~/相机功能验证/`：把`external/相机功能验证/`复制到接手者home下对应位置（先检查目标不存在，不要覆盖已有研究）。
3. 首先检查`toolchain/build/qemu-system-arm --version`能否运行。若动态库前缀/符号链接失效，按`toolchain/README.md`和依赖锁定文件重新配置环境、重建，不要据此判断固件失败。
4. 完整归档保留原绝对符号链接供审计；恢复脚本不会悄悄改写它们。检查build、venv、mamba/deps的原用户名和路径后迁移或重建。只在相同机器/原路径恢复时最接近原环境。
5. QEMU需要JIT与本地Unix套接字权限。本次没有连接实体相机。

## 下一项应做什么

任务A最新成功对照在`qa/task-a/lens-release-high-taska-01/`：外部释放输入位高，原厂SPI读取有效值03、存储值仍02；镜头警告消失，实际界面到scene70de时区地图，425次绘制/10次swap。

当前新阻点：没有主动方向操作时，时区仍向东变化。方向键/拨轮默认输入是候选原因，尚未证明。先定位真实输入事件、修最小外部输入模型并做开关对照，再继续时区/日期向导和主菜单。不要批量把GPIO拉高，也不要直接写UI状态、完成标志或调用回调代替硬件行为。

多步按键`--key-sequence`仍有待验证：仅凭新增swap计数可能拿到旧scene排队帧，必须关联目标scene/modal和实际swap，再发下一按键。focused镜头输入测试的冷启动VFP缺陷已有独立修正建议补丁，原脚本未合入。

DSP/传感器/ISP未完成；GUI当前依赖显式局部调度、软件图形后端和TEST中断。JPEG样片来自合成输入+真实补丁/原驱动+软件JPU，不能称原快门到JPEG已通。

## 归档状态

所有项目输入按当时文件字节保存；普通文件附SHA-256，权限与符号链接保留。构建缓存、失败记录及重复大快照也保留。同事能据此审计和接手，但其中历史“成功”字段仍应按具体范围解释。归档校验通过只证明保存完整，不代表新增模拟功能通过。
