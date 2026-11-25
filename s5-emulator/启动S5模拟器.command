#!/bin/bash
set -u
S5_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$S5_DIR" || exit 1
while true; do
    printf '\nS5 固件研究工具\n\n'
    printf '1  运行原厂 Linux 演示\n'
    printf '2  执行一条原厂 Linux 命令\n'
    printf '3  测试真实固件函数和 LUT 补丁\n'
    printf '4  校验全部 48 个固件组件\n'
    printf '5  测试带菜单开关的实验 BIN（完整合成帧）\n'
    printf '6  合成场景 → 实际 LUT 补丁 → JPEG（电脑辅助编码）\n'
    printf '7  完整离线图像实验（真实补丁/驱动 + 软件 JPU，输出 JPEG）\n'
    printf '8  S5 6K／5.9K 视频工具（离线验证／USB 菜单扩展）\n'
    printf '9  原厂多核启动／菜单诊断（仍在开发，保存实际启动记录）\n'
    printf '10 原厂四核＋GUI 初始化实验（含 Linux；尚无可操作菜单）\n'
    printf '11 最新局部菜单启动实验（模式切换／串行发送／测试中断）\n'
    printf '0  退出\n\n'
    printf '请选择 [1]：'
    IFS= read -r choice || exit 0
    case "${choice:-1}" in
        1) python3 "$S5_DIR/s5.py" run ;;
        2)
            printf '输入命令（例如 /bin/cat /etc/inittab）：\n> '
            IFS= read -r guest_command || exit 0
            if [[ -n "$guest_command" ]]; then
                python3 "$S5_DIR/s5.py" run --command "$guest_command" --seconds 5
            fi
            ;;
        3) "$S5_DIR/lab/.venv/bin/python" "$S5_DIR/lab/s5_lab.py" run ;;
        4) python3 "$S5_DIR/s5.py" verify ;;
        5) python3 "$S5_DIR/handoff/run_ui_qemu.py" --case full-frame ;;
        6) python3 "$S5_DIR/capture/capture.py" ;;
        7) python3 "$S5_DIR/capture/simulate_photo.py" ;;
        8) python3 "$S5_DIR/s5.py" video menu ;;
        9) python3 "$S5_DIR/menu-smp/boot.py" --storage-hle --capture-gfx --seconds 15 ;;
        10) python3 "$S5_DIR/menu-smp/boot.py" --storage-hle --capture-gfx --graphics-hle --spi0-open-bus --spi6-open-bus --adc11-sample 0x300 --isolate-display --isolate-display-ticks 600000 --linux-peer --key-event KEY_CENTER --seconds 45 ;;
        11) python3 "$S5_DIR/menu-smp/boot.py" --ram 2G --storage-hle --capture-gfx --graphics-hle --spi0-open-bus --spi6-open-bus --tx-serial-hle --adc11-sample 0x300 --isolate-display --isolate-display-ticks 600000 --linux-peer --service-gui-once --probe-ui-mode35 --display-clock-test --ui-service-loop --ui-service-postinit --frame-irq-test-pairs 128 --frame-test-stage-seconds 10 --key-event KEY_CENTER --key-click --seconds 90 ;;
        0) exit 0 ;;
        *) printf '请输入 0 至 11。\n' ;;
    esac
done
