# 任务 A：镜头释放输入模型真实回归与界面对照报告

## 1. 执行与断言摘要

- **唯一变量核验 (`verified_single_variable`)**: `True` (`--lens-release-input-high (/machine/rtc-peer.lens-release-input-high)`)
- **未修改 focused 测试执行**: `False`（冷启动未开 VFP，在 `0x066c2620` `vpush {d0-d3}` 触发 UND `0x4`）
- **开启架构 VFP 后 focused 执行 / 断言**: `True` / `True`（实测命中 `0x06753036` 11 次、`0x06752fb0` 14 次、`0x0642c43c` 25 次）
- **Baseline 120s 执行 / 停止原因 / 退出码**: `True` / `time_bound` / `0`
- **Lens-release-high 120s 执行 / 停止原因 / 退出码**: `True` / `time_bound` / `0`
- **Baseline 是否存在 modal `0x4a` 告警**: `True`
- **Lens-release-high 是否存在 modal `0x4a` 告警**: `False`
- **原厂时区提示 CENTER 确认成功 (Baseline / Candidate)**: `True` / `True`
- **页面实际更新并保持无遮挡 `scene 0x70de` (Candidate)**: `True`
- **主菜单是否已显示 (`menu_rendered`)**: `False`（仍为时区选择页 `0x70de`，且并非镜头已连接）

## 2. RTC 元数据与 `0x044e` 对比

| 指标 | Baseline (关闭) | Candidate (`--lens-release-input-high`) |
| --- | --- | --- |
| `lens_release_input.enabled` | `False` | `True` |
| `0x044e stored_value` | `2` | `2` |
| `0x044e effective_value` | `2` | `3` |
| `affected_reads` | `0` | `6938` |
| 总 `reads` / `writes` | `73838` / `1898` | `74444` / `1918` |
| `unknown_reads` / `malformed_frames` | `41277` / `0` | `41631` / `0` |

## 3. UI 状态与源/绘制布局对比

| 字段 | Baseline | Candidate (`--lens-release-input-high`) |
| --- | --- | --- |
| `control_words_08a928b0` | `['0x0', '0x20', '0x0', '0x1']` | `['0x0', '0x20', '0x0', '0x1']` |
| `modal_id_08a92898` | `0x4a` | `0x0` |
| `source_scene_08cb10d0` | `0x70de` | `0x70de` |
| `render_scene_0b392e98` | `0x70de` | `0x70de` |
| `source_layout_08cb10c8_hex` | `000000004a000000de70000000000000` | `0000000000000000de70000000000000` |
| `render_layout_0b392e90_hex` | `000000004a000000de70000000000000` | `0000000000000000de70000000000000` |
| `graphics_draws` / `graphics_swaps` | `116` / `5` | `425` / `10` |
| `lcd_refresh.completed_requests` | `3` | `8` |
| `graphics_error` | `None` | `None` |

## 4. 实际 Swap PNG 帧索引与 SHA-256

### Baseline 帧清单
| 帧索引 | 文件 | 字节数 | SHA-256 | 非黑可见像素 | 高亮白像素 |
| --- | --- | --- | --- | --- | --- |
| 0 | `frame-0000.png` | 20624 | `441650dc9de75635c6e5009d8937594af0b4a85bb0a6f4dd93725f09cd885fc7` | 75768 | 9566 |
| 1 | `frame-0001.png` | 21421 | `4cc91201a32a93d87c7c03470860c59453861af28b03792dea514061bb14b7b8` | 77951 | 10256 |
| 2 | `frame-0002.png` | 94140 | `6f14f83b44331ea1e0e95423fa4044ad6960ee4cc48fec45bc9cfe61a3916e24` | 609454 | 10869 |
| 3 | `frame-0003.png` | 34369 | `ef76bb7730fa498f70c0978643c6ed07e2ea707a964fd8af953f52fb16c20890` | 614410 | 19428 |
| 4 | `frame-0004.png` | 34369 | `ef76bb7730fa498f70c0978643c6ed07e2ea707a964fd8af953f52fb16c20890` | 614410 | 19428 |

### Candidate (`--lens-release-input-high`) 帧清单
| 帧索引 | 文件 | 字节数 | SHA-256 | 非黑可见像素 | 高亮白像素 |
| --- | --- | --- | --- | --- | --- |
| 0 | `frame-0000.png` | 20624 | `441650dc9de75635c6e5009d8937594af0b4a85bb0a6f4dd93725f09cd885fc7` | 75768 | 9566 |
| 1 | `frame-0001.png` | 21421 | `4cc91201a32a93d87c7c03470860c59453861af28b03792dea514061bb14b7b8` | 77951 | 10256 |
| 2 | `frame-0002.png` | 69029 | `5716bb8ee9b24138a1bbb930479a8cf0efc4782b3e8a013ef32c87839295e199` | 609454 | 6756 |
| 3 | `frame-0003.png` | 68056 | `16c3b11967010d0258f0820a36d9b0a149fe995ddbaa039ea92d39b116e2175a` | 609453 | 6655 |
| 4 | `frame-0004.png` | 83244 | `e6d920c211a2132f6f9c9b8460c30ec3d0132ed77e4a7ba0471f50f0304049e3` | 609455 | 9613 |
| 5 | `frame-0005.png` | 69465 | `9668abb6fa402a8f27282dd5c085373a7efbd3f0c3497134872ed3dfa1a5c23c` | 609453 | 7523 |
| 6 | `frame-0006.png` | 81040 | `775d52d077a9786aa960ff3b02b39c503c2c9a36b034cf0c300c1cb70cf6c9bc` | 609453 | 8441 |
| 7 | `frame-0007.png` | 81040 | `775d52d077a9786aa960ff3b02b39c503c2c9a36b034cf0c300c1cb70cf6c9bc` | 609453 | 8441 |
| 8 | `frame-0008.png` | 71896 | `1df28b49741b05f99699f779ebd8a5b648a66a052395b7607afcf75002bf1adb` | 609453 | 7306 |
| 9 | `frame-0009.png` | 79689 | `edb072cbb227c61f2dbe303780d784b32725f10d6a7474364aa7c481611a7eba` | 609453 | 8069 |
