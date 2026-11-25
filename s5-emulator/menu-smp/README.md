# S5 原厂多核启动与菜单探针

这里执行原厂 V2.9 主程序和 postboot 组件，目标是进入真实菜单任务。三个 RTOS 核心的共享内存握手、RTC 串行初始化、持续定时器中断已通过；第四核现可由原 RTOS 自然唤醒并启动原 Linux。**最新已完成原厂时区提示的实际绘制与EGL帧提交**：画面为“Please set the time zone”和“Set”，原字形路径和界面布局均由固件生成。仍未进入可操作的主菜单，`result.json` 中 `menu_rendered` 保持 `false`。

首张已目视核对的帧为`results/local-frame-query/graphics/frame-0000.png`，15次原绘制后真正swap，核验记录为`results/local-frame-query/visual-verification.json`。之后已实现共享图像离屏绘制及原DMP行对齐；`results/local-shared-image-stride/`里原次级切换阶段自行从2变1，等待11轮原输入循环后提交的CENTER真正确认了时区提示，进入state`0x20`/scene`0x70de`。释放没有重复确认。该实验仍显式隔离未知DSP等待，不代表完整相机启动。

`results/local-requested-lcd-refresh/`进一步完成110次绘制、5次swap。4次有真实GUI请求才触发的LCD232输入都经过原ISR、task18回调及queue115通知，源布局和实际绘图布局均到scene`0x70de`。画面随后被原modal`0x4a`“镜头未正确安装／请勿按住镜头释放按钮”覆盖；其输入来自SPI3外设寄存器`0x044e`的bit0，相关模型还在核验。原时区确认与告警来源证据见`../serial/timezone-key-evidence.json`和`lens-warning-evidence.json`。

先前 `results/local-terminal-menu-click-tx/` 完成了原模式35切换：91条由真实IRQ处理链生成的帧消息经过原队列消费，原代码发出并消费模式确认，最终step为正常终点`0x4a0`，stage为0、state1/mode1。这里的91是原检查调用次数，不是实机帧率或毫秒。显式TX控制器模型处理了1169次传输，未遇未知事务；在切换结束后送入的CENTER按下和释放也都由原输入任务处理。当时尚未修复资源状态寄存器，输出只有4次预热绘制和1次swap。

从项目目录运行：

```sh
python3 s5-emulator/menu-smp/boot.py --storage-hle --capture-gfx --seconds 15
# 明确的环境对照：SPI0 外设缺席、ADC11 原始样本 768、软件图形驱动
python3 s5-emulator/menu-smp/boot.py --storage-hle --capture-gfx --spi0-open-bus --adc11-sample 0x300 --graphics-hle --seconds 30
# 局部显示任务实验：明确延迟尚未模拟的协处理器任务，运行原厂 GUI
python3 s5-emulator/menu-smp/boot.py --storage-hle --capture-gfx --graphics-hle \
  --spi0-open-bus --spi6-open-bus --adc11-sample 0x300 --isolate-display \
  --seconds 120 --stop-after-swaps 2
# 四核/图形/软件按键基础实验（启动器第10项）
python3 s5-emulator/menu-smp/boot.py --storage-hle --capture-gfx --graphics-hle \
  --spi0-open-bus --spi6-open-bus --adc11-sample 0x300 \
  --isolate-display --isolate-display-ticks 600000 --linux-peer \
  --key-event KEY_CENTER --seconds 45
# 最新局部模式切换实验；明确含设备HLE和TEST中断输入，尚无菜单
python3 s5-emulator/menu-smp/boot.py --ram 2G --storage-hle --capture-gfx --graphics-hle \
  --spi0-open-bus --spi6-open-bus --tx-serial-hle --adc11-sample 0x300 \
  --isolate-display --isolate-display-ticks 600000 --linux-peer \
  --service-gui-once --probe-ui-mode35 --display-clock-test \
  --ui-service-loop --ui-service-postinit \
  --frame-irq-test-pairs 128 --frame-test-stage-seconds 10 \
  --key-event KEY_CENTER --key-click --seconds 45
```

每次创建独立结果目录；`--keep-ram` 额外保存 512 MiB 内存和协处理器 SRAM。`--linux-peer` 自动导出原 printk 日志，无需保存全部 RAM。`--stop-at 0x地址` 可在指定原厂指令处停止。时间上限为 600 秒；`--stop-after-swaps` 只统计真实客体调用的 EGL swap，并不判断画面内容。运行需要本机允许 QEMU JIT，不连接真实相机。

## 原模式请求与高地址内存实验

`--service-gui-once` 在原 task66 让出 2000 tick 后，让它调用原接收及分发函数处理队首的真实 `0x36` 请求。原 pool 释放、ACK 入队和输入任务消费均已观察；原 GUI 自行从 state0/mode6 进入 state1/mode1。它仍是协处理器初始化完成前的局部函数实验，不是完整启动，也没有由主机写入 ACK 或 GUI 状态。

`--probe-ui-mode35` 再处理实际队首 `0x35` 请求。原函数返回1，但其异步完成通知尚未产生。该分支在默认512MiB配置中触发了 `DFAR=0xffa91000`、`DFSR=0x808` 的写异常：原分配器的 arena 与第二个1GiB地址转换区需要高地址 backing，页表本身已有正确恒等映射。

`--ram 2G` 显式提供高地址 `0x80000000..0xffffffff` 的2GiB独立 backing，低地址只共享前1GiB，保留MMIO。它根据原软件地址布局建立，**不表示已测出实机DRAM容量**。83项地址、别名和外设检查通过；`results/local-ui-mode35-2g/` 的90秒运行无该异常，原代码在 `0xffa91000` 写出了正确保护字 `0x7d159000`，第四核也正常启动原Linux。GUI仍等待 `0x35` 的异步ACK（step0x436），LCD clock state1/AVIO completion0，图形仍只有预热帧。

```sh
python3 s5-emulator/menu-smp/boot.py --ram 2G --storage-hle --capture-gfx --graphics-hle \
  --spi0-open-bus --spi6-open-bus --adc11-sample 0x300 \
  --isolate-display --isolate-display-ticks 600000 --linux-peer \
  --service-gui-once --probe-ui-mode35 --key-event KEY_CENTER --seconds 90 --keep-ram
```

在2GiB配置下，`--keep-ram` 保存前512MiB的 `ram.bin` 和最高256MiB的 `upper-ram.bin`，不是全2GiB转储。原页表与分配器证据分别见 `../linux-smp-analysis/memory-layout-notes.md` 和 `../serial/mode35-fault-evidence.json`。

`--display-clock-test` 在上述模式35调用后执行明确的诊断输入：两个状态位保持指定测试值，向真实GIC依次输入 IRQ196、232、232；每一步必须观察到原ISR及正确状态变化才继续。这不是自动时钟模型，也没有恢复实机帧率、波形或设备ACK。`results/local-display-clock-test/` 已实测原 LCD state1→2→3→4、AVIO completion0→1→2、回调指针由原代码清零；三次输入后关闭TEST模式。主机没有直接调用回调或写客体状态。菜单仍在step0x436，输出仍为预热帧，显示链完成不能替代模式35异步确认。

`--ui-service-loop` 使用原task66栈下独立16字节帧，从原 `0x0675efba` 连续运行接收/分发循环，最多处理64条消息后在下一轮接收前停止。它不恢复到暂停的协处理器初始化栈，也不伪造消息或分配器返回值。`results/local-ui-loop/` 实际顺序处理了管线返回的 `0x40001` 与后续 `0x2b`，两次原dispatch均返回1；随后任务阻塞在原空队列接收。原 `0x40001` 使controller+0x15从0变1，GUI模式确认仍未完成。消息来自原厂静态ring，原接收器对它的pool释放报错后忽略，不能由主机额外free；合同见 `../qemu/peer-isa-analysis/protocol-feasibility/command-40001-contract.json`。

可选 `--ui-service-postinit` 从原 `0x0675efb2` 开始，额外实际执行 `0x0672060c`、`0x065b9fcc` 两个原调用，再进入同一循环。`results/local-ui-loop-postinit/` 已观察两次返回，以及原代码将 `0x08aa02af` 置1；45秒对照仍无新增绘图，模式检查计数仍为0。这个选项没有完成此前的DSP初始化，不能视为完整启动。

## 一次性定时器重新装载修复

原上电序列使用timer6：第一次到期后，后续原 `0x06554ca2` 只写LOAD重新装载，不重写CONTROL。原QEMU计时器在one-shot停止后没有因LOAD重启，导致sequence index一直为1；queue0x16为空，原task30正在正常等待，没有队列死锁。S5机型现显式启用 `load-rearms-oneshot` 行为，补丁由 `../qemu/apply_qemu_patches.py` 持久应用。

`../qemu/oneshot-reload-baseline/` 与 `oneshot-reload-fixed/` 对照实际验证：S5原先仅第一次到期，修复后连续三次LOAD-only重新装载均到期；disabled写LOAD不启动、周期模式正常，其他机型默认行为保持不变。随后 `results/local-timer-rearm/` 45秒实跑中，原上电序列自行从index1走到10，原 `0x0675948e` 将ready置1（`0x08aa7f18`八字节为`0001000101000a00`）。主机没有写这个标志。GUI仍等待由图像管线IRQ192/193驱动的帧消息；原LCD IRQ196/232完成与这条链不同。

`--frame-irq-test-pairs 2` 的 `results/local-frame-pairs2/` 已验证四次真实GIC中断及原ISR返回。原代码自行将source latch置1、形成gate向量`0100010000`，通过queue0x77产生并消费首条帧消息，最终模式检查计数从0增至1。第一组原代码有启动边沿抑制，故没有生成帧。这里仅提供明确的时序中断输入，没有写入像素、metadata、消息或ready；`0x220`字节消息内容是原厂模式及时序上下文，尚未确认像素plane ABI。菜单仍未显示，不能将这项结果称为传感器或ISP成像成功。

逐帧探针的首轮 `local-frame-paced-menu` 暴露了调试控制竞争：旧RSP `s` 在QEMU里对一个核设置step，却用 `vm_start()` 恢复全部核，其他核可能先命中断点。公共探针现改用明确的 `vCont;s:<thread>`，不吞掉未知停止。`../probe/results/smp-isolated-step-warm/` 实测8次目标核ADD/branch步进，其余三核完整寄存器不变。single-thread TCG冷启动须先让CPU0启动一次；正式固件运行已满足，合成测试先启动四核再复位其测试寄存器。

## 模式切换完成与串行发送实验

逐帧探针现在等待原task67消费完上一条消息并回到接收循环，才继续下一组IRQ192/193，避免重复使用原静态payload。`--frame-irq-test-pairs 128` 是输入上限；观察到原模式35确认已消费即停止输入，实际用了92组、91条帧消息。原检查在计数达到90后走自身fallback，调用原确认发送函数，随后由原输入任务完成切换。终点`0x4a0`表示这次切换已经结束，不能再当作等待状态。

此前2秒主机观察时限会截断一批原串行发送：每条原事务等待`0x7b193444`的bit8，外设缺席时在有限轮询后返回错误，再继续下一条。`results/local-frame-paced-menu-timeout10/` 将单阶段观察时限扩大到10秒，即使没有TX模型也完成了上述模式切换。因此有限串行超时不是永久固件死锁。

可选`--tx-serial-hle`根据原寄存器序列接收TX字节，完成后给出控制器发送完成bit8；原代码自行确认并停止。它只接收已核对的`0x81`写事务，未知格式停止，不提供RX、外设接受确认、DSP执行或像素。`../qemu/serial-tx-hle-verified/` 已实际执行原单条及批量发送函数，对比缺席时返回1、启用时返回0；完整启动中的1169次收发控制序列见最新结果。它不是实机串行时序模型。

`--key-click`先提交原软件按键按下，再于原任务消费后提交释放。启用连续UI服务时，按下还等待原模式确认和切换终点；释放不依赖按下后的UI状态。两条消息都通过原分配、排队、处理与释放流程，完整保存恢复core/VFP寄存器；这不是GPIO电平输入，也不能仅凭消费成功声称菜单已打开。

## 原资源状态寄存器与实际界面绘制

原界面提交函数`0x068334b0`通过`0x06469ebe(2)`检查资源是否加载完毕。此前`0x7c0151f4..0x7c015200`四个状态字被未知MMIO模型读零、丢弃写入，导致原加载器成功后写入的bit1也无法读回，界面消息因而没有进入queue101。现仅对这16字节保持原客体写入值，初始全0，不产生任何IRQ、事件或完成位。原读写助手和检查函数的跨核、覆盖、清零对照通过，证据见`../qemu/software-status-verified/verification.json`。

`results/local-software-status/`中原代码自行将状态word2写成7，随后实际绘制了居中的矩形边框；绘制增加到9次。该轮在首次swap前进入尚未实现的`vgTransformPath`边界而停止。`graphics/incomplete-surface-60000002.png`只是停止时的未提交表面，不是已经显示的菜单。原API身份、capability与两句柄ABI见`../qemu/peer-isa-analysis/protocol-feasibility/transform-path-contract.json`。

## 已实际验证

- 新 `s5-rtos` 机型建立四个 Cortex-A7，以及低地址和 `0x80000000` 高地址的一致 RAM 别名。CPU0/1/2 执行原厂入口并自行清除共享完成标志。默认 CPU3 停放；`--linux-peer` 恢复原 loader 的 GICC `1/f0` 初态，从原 `0x330` 进入等待，由原 RTOS 写 `0x80128000`、发 SGI2 唤醒。没有改写启动完成标志或直接跳过 zboot。
- program、postboot1–5、osddata 按原厂常驻布局加载；原厂 scatter 初始化自行建立 RW 数据。所有 48 个组件均根据固定摘要的原始更新包重新校验。
- PL022 FIFO 与三字节寄存器协议模型让原厂自行初始化 RTC，实际写出 2014-01-01。RTC 冷启动值及未知寄存器的实验值被明确记录，尚未模拟真实时间流逝。
- 模块启停模型根据原厂请求/轮询关系应答；实际通过 94 次请求。真实硬件延迟未恢复。
- 13 对 SP804 计时器以 27 MHz 工作，26 路独立 IRQ 的映射来自原厂表。启动 helper 恢复 loader2/3 已确证的 timer1 Control 写序列 `0 → 6 → 0x86`。
- 原 loader3 驻留地址零，CPU0 真正调用其 `0x10e8` 路由初始化函数；各核恢复原 GICC 设置与 `CPU × 0x8000` 地址别名。实际异常进入原厂 IRQ handler 已验证。RTOS 强制置位的 Control bit4 被当前模型解释为脉冲 IRQ 扩展；这是由原厂 edge 配置与无 INTCLR 的处理链推定的行为，尚无硬件测量。完整启动中原厂系统 tick 已持续增长到 21,791。
- 五路 PL011 提供真实空 FIFO 和精确 IRQ 路由。UART2 在外设缺席时实际发送 `34 27` 两字节，再按原厂超时继续；没有注入成功回复。
- 三路 ADC 控制器按原厂 start/mask/result 流程提供原始样本，`--adc11-sample 0x300` 作为显式输入高于原启动阈值；没有修改原厂判断，也没有将样本值冒称物理电压。
- 可选只读存储模型仅在原厂扇区设备调用边界提供更新包内的真实字节。原厂字节读取、对齐和锁代码继续执行；缺失区间显式失败。

## 当前启动证据

`results/loader-timer-boot2/result.json` 保留输入状态未恢复时等待 event125 的对照。当前两个 GPIO bit4 采用明确的未激活电平；物理名称尚未确认，不能直接叫作 MENU/电源键。

`results/pulse-timer-boot/result.json` 记录原厂代码越过 ADC 低值休眠分支和 1 tick 延迟。更长的 `results/pulse-timer-60s/` 证明 I²C 等待会按原超时继续，不能将短时等待称为永久阻塞。当前 I²C 模型提供明确的通用控制器错误，通过原 ISR、四次重试与失败回调继续，没有伪造成功 ACK。

正常完整启动随后停在 uiTSK066 的 `0x065491ee`：等待另一个协处理器写 `0x7b004000`。其 64 KiB 代码及 128 KiB 数据已由原 ARM 上传代码完整复制，快照与原嵌入数据逐字节相同；尚未识别该处理器 ISA。模型不写 ready 或校验魔数。任务18虽已启动，但优先级较低，被该无让出循环阻挡。

`--isolate-display` 是明确的局部执行条件：仅在原 task66 的安全任务上下文调用原 `0x066c23e8` 延迟 API，原调度器让它等待指定 tick（默认 60000，可显式设为 1..600000）。没有改 TCB、事件标志或原厂指令。它让原 AVIO 任务18和原 GUI 任务开始执行，**不代表完整相机启动成功**。实际记录已到原 EGL 初始化、1024×768 双缓冲窗口、OSD 图形源注册、paint 初始化、原路径/线性渐变绘制及 swap。SPI6 的 LCD 初始化按原代码发送配置，接收值被原代码丢弃；目前是明确的 MISO=0 缺席外设模型。

可选软件图形后端只消费原厂调用传入的路径、像素和状态；未知接口停止。输出 PNG 是主机 EGL/OpenVG 后端帧，尚未同步回客体原生扫描缓冲区。合成自测图、原厂 GUI 帧、完整整机启动是不同的验证范围。

`results/isolated-display-gradient/` 的首次原厂 swap 包含透明 clear 和左下 25 个非透明预热像素；没有菜单文字或图标。38 项后端测试以及实际调用重放通过，不能拿自测图替代菜单。

`results/isolated-menu-key-vfp/` 记录了真实软件按键输入：在原输入任务285的循环边界调用原消息 API，timeout=0；原 pool 分配、queue115接收、按键处理及返回均已观察。core 与 D0..D31/FPSCR/FPEXC 完整保存恢复。菜单仍未打开；当前原状态机 mode6/step0x3c2 的确认值为0x0d，缺少 task66 对 queue0x76 中原消息0x36的回复。task66 尚未结束协处理器初始化，因此还没有进入该队列接收循环。没有伪造这条回复或强设 UI 状态，详见 `../serial/gui-start-wait-evidence.json`。

`results/linux-peer-loader/` 则验证了第四核实际执行原 loader→zboot→zImage→Linux；原 DTB 无修改，日志明确记录 physical CPU3、只读根文件系统挂载、ICC/ICSM 模块加载。原进程环表里已有 nwa/net/picm/link/psm/lvdp 等用户进程。它们存在与休眠并不证明相机网络协议已经可用。证据及精确内存边界见 `../linux-smp-analysis/`。

未知协处理器已有原代码的 64bit 半字排列与候选调用/常量格式分析，但未能与 Teakra 或 GNU MeP 解码形成一致匹配；尚无可信执行器。下一步需要可匹配的具体 ISA/SoC 文档或带架构信息的原编译产物，不能用写入 ready/magic 来代替原程序执行。见 `../qemu/peer-isa-analysis/README.md`。

`0x06495ab6` 仅是 OSD callback 注册；命中此处也不等于菜单已经显示。`--capture-gfx` 会记录原厂 `0x06495b96` 传入的图形源描述符。软件 OpenVG 后端另在 `display/` 开发，不能把其合成自测画面算作原厂菜单。

器件模型与实跑证据分别见 `../qemu/README-rtos.md`、`../serial/README.md`。旧 `menu-boot-rtos/` 单核探针保留为历史对照；其中人工清标志的探索条件不用于这里的多核成功结论。
