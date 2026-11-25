# 指定 UI 实验 BIN 的实际试跑

本次用户指定文件为：

`s5-v29-offline/lut-firmware-ui-experiment/S5_V29_LUT_UI_EXPERIMENT_UNVERIFIED.bin`

SHA-256：`5727be9315ee229291783a483090efb88be9fd75fcdc032f19314152738564de`。

## 实测结果

1. 从这份 BIN 重新解出 `postboot3` 和 `osdover`，与包内 SHA-256 及既有构建文件逐字节匹配。其余 46 个组件密文字节未变，改动范围仅限两个组件、各自摘要和外层 CRC。
2. 验证解出的中英繁三份菜单资源确实包含 FilmLook 试验名称。这是资源字节核验，不是菜单显示测试。
3. 用交接材料原有 Unicorn 脚本运行四种情况，全部通过。
4. 独立用 QEMU 9.2.4 / Cortex-A7 执行同样的真实代码。关闭状态、HLG 状态和尺寸不符均正常返回且不改像素；开启时首块像素由 Y/Cb/Cr `100/128/128` 变成 `108/125/131`。
5. **在 QEMU 中处理完整 6000×4000 合成帧，2400 万像素全部通过。** 两个 24,064,000 字节平面（含行尾填充）的 SHA-256 均与预期一致；原厂调用正常返回、r0=0、栈与 r4–r11 恢复。该次主机测试总耗时约 4.15 秒，包含模拟器启动和读写校验，不能换算为机身执行速度。

测试输入是常量 NV16 合成图像：Y=100、Cb=Cr=128，stride=6016。预期输出为有效像素 Y=108、Cb=125、Cr=131，行尾填充不变。完整帧通过不代表其他像素格式或真实相机输入都已验证。

## 再运行

双击项目的 `启动S5模拟器.command`，选 **5**。或从 `s5-emulator` 目录运行：

```sh
# 四个分支测试
python3 handoff/run_ui_qemu.py

# 完整合成帧
python3 handoff/run_ui_qemu.py --case full-frame
```

工具使用 `results/handoff-ui-check/` 中已经从原包核验提取的输入，每次再次核对源 BIN、程序转储、组件和载荷摘要。输出自动保存到新的 `results/ui-qemu-.../`，不会覆盖原包。

如要重新独立提取，指定一个新的输出目录：

```sh
python3 handoff/prepare_ui.py --output results/ui-inputs-new
python3 handoff/run_ui_qemu.py --inputs results/ui-inputs-new --case full-frame
```

提取沿用现有本地解密工具和配置，不在报告中保存密钥。

## 证据文件

- `results/handoff-ui-check/package-verification.json`：原 BIN 和实际解密组件校验。
- `results/handoff-ui-check/emulation.json`：重新运行原有 Unicorn 测试的结果。
- `results/handoff-ui-qemu/verification.json`：QEMU 四分支测试、真实断点轨迹和寄存器检查。
- `results/handoff-ui-full-frame/verification.json`：QEMU 完整帧检查、两个完整平面的实际/预期 SHA-256。
- `results/handoff-ui-full-frame/output-Y.bin`、`output-C.bin`：实际模拟输出。

## 执行范围

使用 QEMU `none` 平坦 RAM 实验机，加载已验证的主程序转储，以及从指定 BIN 核验提取的补丁；手动设置输入描述符、开关变量、栈和寄存器，从真实原厂调用点开始执行。断点实际命中原厂 getter、memcpy 和调用返回位置。

这不是从相机 Reset/BootROM 启动整个 BIN。尚未模拟机身菜单绘制与操作、实际照片输入、JPEG 编码器、DMA 所有权或真实缓存一致性，也没有验证更新接受、签名、刷入和整机启动。整个测试不连接相机。
