# S5 原厂组件、存储与图形边界

这里的组件和 UI 调用来自 S5 V2.9 原厂解包数据。存储 HLE、CoreGraphics 绘图属于明确标注的软件替代。当前的后端自测图是合成 API 测试，**不是 S5 菜单截图**。

## 已验证的原厂执行

`prepare_components.py` 生成完整 resident image：program、postboot1–5、osddata 从 `0x06280000` 连续至 `0x08a67000`。`prepared/load-plan.json` 列出原厂 LOAD 头、scatter 表、组件地址和 SHA256。原厂 scatter 函数 `0x06280090` 在 QEMU 执行后，全部复制区逐字节与原厂源相同；只加载 program 时有三个 postboot RW 区域错误，记录在 `scatter-check/verification.json`。

`storage.StorageMap.read(address, length)` 只返回验证过的原厂组件字节，缺失区间报错。`20_osdover` 是按逻辑地址 `0x41260000` 读取的 FDL 字体，不应凭空指定 RAM 装载位置。`storage-map.json` 含原始包、各组件 hash 和映射依据。

原厂字节读取 `0x068098b2` 保持执行，在更低的 sector I/O `0x068609b6` 替代存储：`r0=sector, r1=count, r2=RAM destination, r3=1`，源地址 `0x40000000 + sector*512`，长度 `count*512`，成功返回 `r0=0`。原厂 reader 自己完成未对齐头、中、尾复制和错误返回，四种实测记录在 `storage-check/verification.json`。

## OpenVG 软件驱动适配器

```python
from display.graphics_hle import GraphicsHLE, UnsupportedVG
graphics = GraphicsHLE(emu, output_dir)
for pc in graphics.breakpoints:
    emu.breakpoint_add(pc)
# CPU 已因 breakpoint 停止，保持当前 CPU 选择。
event = graphics.handle(pc, emu.registers())
```

`handle` 读取 ARM softfp ABI 的 `r0..r3` 和栈参数，消费原始 path、坐标、图像字节与状态，在成功后按 `BX LR` 返回。未知 API、未知图像格式、未实现绘制状态会记录后抛异常，保持 guest PC，不假报成功。`0x065daab4` 是额外 guard，捕获漏接的原厂 VG public stub 在读取原驱动 context 前停止。

窗口宽高必须来自真实调用。例如在原厂 native window 创建入口 `0x064deeba` 捕获 `graphics.native_window_arguments(regs)`，执行原厂函数并捕获其真实 `r0` 返回值后，调用 `graphics.register_native_window(return_r0, **captured)`。这两个 observer 不修改原厂函数。也可传入有证据的 `window_resolver`；没有尺寸时会停止，不自动假设 960×640 或伪造 guest 结构体。

导出文件：`calls.jsonl` 保留调用入口、LR、状态、矩阵、path 原始坐标和异常；`image-*.bin` 保留实际 guest 图像 bytes；`frame-*.png` 仅在捕获到 `eglSwapBuffers` 时导出软件 framebuffer。必须结合实际固件调用 trace 才能判断是否真菜单。

已实现的绘制子集：clear、scissor、affine matrices、solid fill/stroke、constant-alpha linear-gradient fill/stroke（PAD/REPEAT/REFLECT 与独立 paint matrix）、标准 path 与 DMP direct path、RGBA8888/PRE/RGB565/RGBA5551/RGBA4444/L8/A8 图片、DMP standard image 创建与 subimage 绘制、SRC/SRC_OVER。曲线/文字轮廓由 guest 提供；不用主机字体，不含菜单文本或布局。

原厂 `0x07290098` 的 linear-gradient 画笔初始化和随后的实际 draw 均已触发。后端保留标准 paint 的类型、颜色、linear/radial 向量、完整 color ramp、spread、tiling 和 premultiplied 参数。当前已实现实际使用的 constant-alpha 线性渐变；variable-alpha/radial/pattern draw 仍停止。`replay_gradient.py` 重放 `isolated-display-vendor-state` 的真实 paint/state/path，4×4 初始化预热路径产生灰阶 222/224/225，证据在 `gradient-replay/verification.json`。这只是原调用重放与预热图，不是菜单截图或真实 swap。

`../menu-smp/results/isolated-display-gradient/graphics/` 随后在 QEMU 的原厂 GUI 执行中通过这些渐变调用，并命中首次真实 `eglSwapBuffers`。该 `frame-0000.png` 包含初始化透明清屏及很小的预热残留，**仍不是菜单**；不能以产生 PNG 或命中 swap 为由宣称菜单已显示。后端验证已包含 38 项，涵盖真实渐变插值、周期/反射、描边轮廓裁剪、paint matrix 和未知绘制停止。

明确限制：无 GPU 硬件模型、无 variable-alpha/radial gradient、无 pattern/masking、无 perspective、无 linear-light image、无 glyph font object API、无 compressed image、无独立多 context 状态。EGL config 只支持已在原厂调用中确认的 ID 6（原始表 `0x0701d8dc + 5*84`，RGBA 每通道 8 bits、32-bit buffer、depth/stencil=0）；其他 config 查询、DMP 自定义 surface 属性、native window/内存 allocator 的 GPU 细节仍需根据真实初始化调用逐步接入。未知操作会停。自测不能证明完整 UI 初始化已通过，软件光栅化也不承诺与原 GPU 抗锯齿逐像素相同。

`live-egl-analysis/evidence.json` 记录真实 GUI 新增调用：原厂 context 请求六项驱动资源大小，软件后端只接受已捕获的精确列表并记录，不模拟原 GPU 内存池耗尽。实际 native window 由原 allocator 分配为 **1024×768**，不依据默认逻辑 viewport 随意裁成 960×640。窗口属性是标准 PRE alpha / sRGB；后端使用命名 sRGB、预乘 alpha bitmap。`0x064c46c4` 仅把软件 EGL surface 映射回观察到的原始 DWD window handle；`0x064de7da/0x064de686` 保持原函数执行，查询原厂分配的两个 3 MiB buffer。此前把后一个函数标成 format query 不准确：其 E010 查询返回下一 buffer 索引。native 字段证据在 `../serial/native-surface-abi.json`。

原厂属性 `0x3fdd=0x3fde` 控制一条已证实的 CPU 线性 buffer 复制路径（`0x064d2ebe → 0x064d2f34 → 0x06503e3c`），正式扩展枚举名未知。软件驱动使用线性 host bitmap 并记录这个模式；替代值 `0x3fdf` 仍停止。**原 guest 双 buffer 尚未同步，scanout 未实现**，导出 PNG 是实际 VG 调用驱动的软件后端结果。不能将仅命中的 AVIO 提交或 swap 视为原帧缓冲已经有正确像素。

本机依赖：系统 CoreGraphics，通过 Python ctypes 使用；Pillow 已存在。无需安装包。

```bash
python3 s5-emulator/display/test_graphics_hle.py
```

`backend-selftest/verification.json` 记录合成 API/guest ABI 验证：DMP 栈参数、path 坐标、图像格式/上下方向、原 LR 返回、未知调用保持 PC、masking 明确停止和原厂 config 6 请求。图形后端 `openvg_backend.py` 与 guest 适配器 `graphics_hle.py` 分离。

`probe_graphics_wrapper.py` 另外在 QEMU 执行未改的原厂 app wrapper `0x072a34d6`：它真实调用 DMP path API 和 vgGetError，再正常返回；结果 `wrapper-check/verification.json` 核对 stack/callee-saved/参数/像素。输入方块和 surface 仍是合成的，证明的是调用边界正确，不能称为真实菜单绘制。

## 地址与证据

`graphics-api-table.json` 是原厂 name/function 表提取结果（126 条，包括 EGL/DMP/VGU）；`graphics-hle-bindings.json` 加上 core VG 静态推断名称、ABI 参数数量和入口 16 bytes。core VG 函数名不是符号表恢复结果，不能混称全部已由字符串确认。

| 原厂入口 | 作用 / 证据 |
| --- | --- |
| `0x0647e83e` | uiTSK018 task entry；实际 TCB `0x08dffe14`，由 kernel agent RAM 观察确认 |
| `0x0648d636` → `0x06495ab6` | OSD callback 注册，原字符串 `avio_plane_osd_init()`；不是菜单 render 本身 |
| `0x07395fcc` → `0x072ed54e` → `0x072ed4d0` | 图形 heap/device/EGL 初始化，随后原厂 `eglInitialize/ChooseConfig` |
| `0x07435b54` | 上述原始 `eglChooseConfig` 属性 `{0x3028,6,0x3038}` |
| `0x07391388` → `0x072887e4` | 原厂显示初始化，函数指针引用位于 `0x06afec10` |
| `0x07288338` → `0x0728fe58` | native/EGL surface 创建；width/height 来自原厂 geometry 逻辑 |
| `0x07287de4` | geometry routine 默认宽高 960×640；不同 mode 会更改，不是全局固定分辨率 |
| `0x065da884` | export 表确认 `vgDrawPathDataDMP`；真实 app wrapper `0x072a34d6` 调用 |
| `0x065da992` | export 表确认 `vgCreateImageStandardDataDMP`；真实 caller `0x0729038c` |
| `0x065da76e` | export 表确认 `vgDrawSubImageDMP`；下层 `0x065ea75e` 验证 subrect |
| `0x064c3cda` | export 表确认 `eglSwapBuffers`；app 调用点 `0x07290378/0x07299282/0x072994d4` |
| `0x06495b96` | `AVIO_PLANE_SetGfxDisplaySource`，读取原始 buffer/width/height，真实 app 提交点 `0x07287dd2/0x0728947a` |

`0x065d9b0c` 的原厂 clear 路径进入 `0x0660aecc` 等 command builder，在 `0x0660afb8..0x0660afd0` 追加 GPU 指令。这证明已查看路径使用 GPU 提交，并不证明整个原厂库不存在 CPU fallback。

uiTSK018 创建在 `0x063b0760..0x063b077a`：entry 取自 literal `0x063b09e4`，task id `0x12`，priority `0x6c`，stack size `0x2800`，调用 `0x063bf4ac`。它进入 `0x0647e85e` 消息等待循环前先注册 OSD callbacks。正常启动已观察其 activation，但高优先级 task66 等待未模拟的 7b 协处理器，阻碍 GUI 调度。`isolated-display-*` 实验通过原 RTOS delay 暂缓 task66 后，确实执行原 uiTSK018、OSD init 和上述 EGL 调用；这属于局部执行，不能称为正常完整启动。

正常 activation 路由：uiTSK001 `0x063c8b94` 调 `0x063c7db6`；其中 `0x063c7eb0` 调 activation wrapper `0x065a2b2e`，参数从 `0x06f48ed8` 加载，已在 boot RAM snapshot 核对为 task 18。该 wrapper 按 task id 查 TCB，若 `TCB+0xec == 0x10`（dormant），经 `0x065a2b14` 置状态 2、`0x0647eda0` 重建 stack、`0x065085f4` resume。`task-activation-table.json` 保留该启动例程的 55 个 task id/调用地址；`pulse-timer-60s` 已命中上述主流程与 task18 activation。

2026-11-24 后续真实场景进展：`local-software-status` 修复软件写入的组件完成寄存器保留后，原布局任务开始提交新场景，不再停留于透明预热。`render-wait-analysis/evidence.json` 和 `original.disasm.txt` 保存 GUI queue101、event86、LCD232→task18 回调及原 `7c0151fc` 完成位 RMW 的证据；`inspect_render_wait.py` 只读重建。

原实际调用新增 `0x065d9c64 = vgTransformPath(dst,src)`，wrapper 进入 `0x064e4698`，检查 source capability8 / destination16，使用 PATH_USER_TO_SURFACE 矩阵追加路径。后端支持已见 FLOAT 目的路径和 M/L/Q/C、相对/H/V/smooth 规范化；保留 source、自追加快照、目的 scale/bias，保存每次原厂 source 轮廓与变换 JSON。整数目的转换、椭圆弧参数变换仍显式停止。`local-path-transform` 已通过真实字体轮廓缩放并绘出可读的 “Please set the time zone” 提示；该运行在画笔释放处停止，尚未完成 swap，不能把不完整 surface 当已呈现菜单。

`vgDestroyPaint` 现在按原 `64ff594`/`64fd7c0` 的生命周期处理：立即撤销 public handle，已绑定绘制对象保留到替换/解绑；`vgSetPaint(0,mode)` 恢复默认黑色。原 `64fd9e8` 从 bound-object+4 返回 handle，该字段在 destroy 时被清零，故 `vgGetPaint` 返回0。已根据原实际调用链加入 `65d9bc6 = vgRemovePathCapabilities` 和 `65d9962 = vgGetParameterfv` 的 paint 查询。标准依据是 [Khronos OpenVG 1.0.1 §8.6.8、§9.1.1–9.1.2](https://registry.khronos.org/OpenVG/specs/openvg_1_0_1.pdf)。`test_transform_paths.py` 的12项主机测试覆盖路径/像素/ABI、生命周期、能力位和查询；它们都是合成输入测试，不是相机截图。
# Captured frame-query boundary

`local-frame-query` subsequently passed this boundary and submitted its
first actual scene through `eglSwapBuffers`: `graphics/frame-0000.png`
contains the original “Please set the time zone” prompt and Set button.
It then reached 27 draw operations and stopped at image-pbuffer creation.
This is an original firmware startup dialog rendered by the replacement
software graphics driver, not proof of completed camera startup or capture.

The `local-paint-lifecycle` run reached the original `eglQueryFrameDMP`
entry `064c4362`, called by `07296c62` with `(display, 0x3fff, out_u32)`.
The original function selects the current OpenVG draw surface and calls
`064daee2`: read `surface+0xa0`, clear that word, write exactly four bytes
to the supplied output, and return true. It returns no framebuffer address.

The sole observed setter `064daefe` is called at `064e5eac` after the GPU
wait handler reads status register `+0x58` bit 26, initiates recovery using
`+0x1214`, and waits for that bit to clear. The exact hardware bit name is
unverified. The original app checks the query after `vgFinish` and retries
with `VG_RENDERING_QUALITY=VG_RENDERING_QUALITY_FASTER` if it is nonzero.
The software renderer keeps a separate read-and-clear latch per surface;
it does not generate this hardware-specific fault. Unsupported rendering
continues to stop, rather than being converted to a successful status.

The adjacent unnamed entry `064c42b8(display, mode)` accepts only 0 or 3
and stores `surface+0x94`. Original GPU command kinds 1/8/10 are skipped if
the fault latch is 1 and the mode is 3; other commands use other branches.
The adapter captures mode changes. It explicitly stops if the fault latch
is nonzero when changing mode because no software command mapping for that
hardware recovery case has been established.

Disassembly is in `render-wait-analysis/query-frame-dmp.disasm.txt`.
The 16 synthetic tests in `test_transform_paths.py` now include the exact
three-argument query ABI, four-byte output with adjacent bytes preserved,
per-surface read-clear behavior, valid current state, mode retention, and
explicit rejection of unknown attributes/recovery behavior. The 38 prior
backend checks still pass. These tests do not establish a completed S5
startup or guest scanout; the real run stopped before the scene's swap.

## Captured image-pbuffer binding

The next observed call, `eglCreatePbufferFromClientBuffer` at `064c2878`,
has five arguments. Caller `07290600` uses type `0x3096` (OpenVG image),
the actual 64×40 `VG_sRGBA_8888_PRE` image it just created, config 6, and
a null attribute pointer. The original code binds the image as the current
draw/read surface, renders into it, restores the earlier surfaces/context,
then destroys the temporary surface. The image continues to be used.

Native `064d3112` increments the image reference count; `064da79c` stores
that same image at surface+68. `064da966` obtains its pixel buffer and
stores the reference at surface+74. `064dadbe` releases the reference on
surface destruction. This matches [EGL 1.4 section 3.5.3](https://registry.khronos.org/EGL/specs/eglspec.1.4.pdf).
The software implementation shares a Canvas with the image and retains
modified pixels after surface destruction. Premultiplied bytes are kept
without an unpremultiply/premultiply round trip. It does not synchronize
these host pixels into the original guest DMA allocations.

`test_client_buffer.py` has six passing synthetic tests for the stack
argument, shared pixels, destruction order, exact low-alpha storage,
current-target image-use error, and rejection of unsupported attributes.
The 16 path/lifecycle tests and 38 initial backend checks also pass.
Native evidence is in `render-wait-analysis/client-buffer.disasm.txt`.

The original DMP standard-image allocator also supplies padded rows.
`065e7224(..., mode=0)` retains width 8, otherwise rounds width up to a
multiple of 16, and rounds height up to a multiple of 8. `065e73ae` stores
pitch as that storage width times bytes per pixel. This is now used when
capturing DMP images, with the original exact allocation-size check. The
10×56 Set-button images therefore use pitch 64, not 40; treating padding
as pixels caused the earlier dotted edges. `test_client_buffer.py` now
contains seven tests, including the width-8 exception and padded-height
case. Evidence: `render-wait-analysis/standard-image-stride.disasm.txt`.

The subsequent `local-shared-image-stride` run completed two swaps and 31
draws without an unsupported API. A pure host replay of its captured
semantic events produces byte-identical PNG files. The center prompt is
pixel-identical in both frames; their only difference is the added battery
icon in bounding box `(866,21)-(929,59)`. Verification is stored in
`render-wait-analysis/two-frame-pixel-verification.json`. The second frame
does not lose the center prompt even if a preview displays only changes.

The optional LCD refresh probe now passively observes the original task18
receive-loop boundary `0647e85e` when queue15 already contains work. It
never clears that queue. If original work clears the request, it emits no
TEST input; a pulse is eligible only if the queue has drained and the real
event86 request remains set. Five passive-deferral tests and nine original
probe lifecycle tests pass. This establishes no physical display cadence
and does not by itself establish the cause of any input-state gate.

