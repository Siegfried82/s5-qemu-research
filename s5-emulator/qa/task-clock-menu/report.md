# Clock-Wizard-to-Main-Menu & QEMU Engineering Report

## Executive Summary
Original unmodified Panasonic LUMIX S5 v2.9 firmware (`03_program.bin`, SHA-256 `fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe`) now boots cleanly in QEMU through the entire first-time setup wizard (**Time Zone -> Clock Set -> Clock Completed -> IBIS Focal Length Prompt -> Native Live View HUD**) and opens the **interactive Native LUMIX S5 Main Menu** (`state=0x2`, Photo `scene=0x1002` and Video `scene=0x10cd`) with `graphics_error=None`, **zero firmware instruction patches**, **zero guest UI/TCB/event-flag writes**, and **zero bulk GPIO pull-ups**.

---

## 1. SMP `key_probe.py` Transient Global-Guard Flake Fix
- **Root cause**: On SMP QEMU, when CPU2 hit `LOOP_ENTRY` (`0x06475dea`), another CPU could momentarily hold an RTOS nesting/dispatch guard (`0x08ab4e68 == 1`) or IRQ-disabled window, causing `_start()` to abort the entire run immediately with `guard_rejected`.
- **Fix** ([key_probe.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/key_probe.py)):
  - At `LOOP_ENTRY`, if in valid Thumb SVC context without an IT block (`(cpsr & 0x3f) == 0x33` and `not (cpsr & 0x0600fc00)`) and any guard in `GUARDS + (0x209c0 + cpu*4,)` is nonzero or `cpsr & 0x80` (IRQ disabled), `KeyProbe.handle()` single-steps the original instruction (`self._step_original(pc)`) without modifying guest memory/flags or extending `self._deadline`, increments `dispatch_guard_wait_steps`, and sets status `waiting_for_clean_dispatch_guard` (timing out distinctly as `timeout_waiting_for_clean_dispatch_guard` if the deadline expires).
  - Strict hard guards remain enforced inside `_start()` so no inferior call can ever begin while any guard is nonzero.
- **Verification**:
  - `python3 -B serial/test_key_probe.py`: **27/27 tests OK** (including new tests for transient wait, timeout status, and hard `_start()` rejection).
  - `python3 -B serial/test_key_sequence_scheduler.py`: **15/15 tests OK**.

---

## 2. Clock Set Hour Drift Diagnosis & A/B Control (`adc0-ch5-sample`)
- **Observed defect** (`qa/task-clock-menu/step3-clock-set-03`, default `--adc0-ch5-sample 0`):
  - Confirming `"Please set the clock"` (`scene=0x70de, modal=0xe`) entered **Clock Set** (`scene=0x2, modal=0x0, state=0x1f`), where the highlighted Hour field continuously drifted (`18 -> 4 -> 14 -> 10 -> 19` across `frame-0004..0015.png`), generating 16 redraw swaps.
  - `key_probe` logs showed continuous `0x00010006` (UI key 6 = DOWN repeat) events firing on every key dispatch.
- **Static firmware proof** ([audit_clock_drift.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/audit_clock_drift.py) -> [clock-drift-evidence.json](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/clock-drift-evidence.json)):
  - `0x06544988` reads ADC bank 0 ch 0..11 (`0x7f030014..0x7f030040`) into `[0x08e21130..0x08e21146]`; `0x06544ad4` passes `[0x08e2113a]` (bank 0 ch 5, MMIO `0x7f030028`) to joystick resistor-ladder decoder `0x065454d8`.
  - Default raw sample `0` falls inside band 0 (`0..56`), decoding raw `0x2` -> logical bank 2 `0x8` -> repeat `0x00010006` -> Clock Set handler `0x068b0ffc -> 0x068b0e8c -> 0x068b0eec -> 0x068b0f8e -> 0x068b0a08(1)` (decrement highlighted field).
  - Highest band is `0x0322 +/- 0x38` (`746..858`); raw `0x3ff` (`1023`) decodes idle `0x0`.
- **Minimal opt-in model**:
  - Added QOM property `adc0-ch5-sample` (default `0`) in [s5-rtos.c](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qemu/s5-rtos.c) and `--adc0-ch5-sample` in [boot.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/boot.py).
  - Verified all 5 bands + idle `0x3ff` through original `0x065454d8` in [test_adc0_ch5_input.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qemu/test_adc0_ch5_input.py) (**5/5 checks PASS**).
- **A/B Control Result**:
  - **Run A** (`step3-clock-set-03`, default `0`): 16 swaps, continuous `0x00010006` repeats, Hour drifting (`18 -> 4 -> 14 -> 10 -> 19`).
  - **Run B** (`step3-clock-set-adc0ch5-fixed-01`, `--adc0-ch5-sample 0x3ff`): 5 swaps, **zero** `0x00010006` repeats, Hour rock-solid at `0 : 00 : 00  1 . JAN . 2020`.
- **Firmware detail discovered on Clock Set**:
  - `0x068b0924` checks `[0x08cc0da0 + 0x49] == 0` before allowing `KEY_CENTER` to save; pressing `KEY_UP` (`0x068b0a3a`) increments Hour (`0 -> 1`) and clears `[0x08cc0da0 + 0x49] = 0`, enabling the `"Set"` button!

---

## 3. Live View -> Native Main Menu Shutter Gate Discovery & Fix
- **Observed gate** (`qa/task-clock-menu/step8-main-menu-01` & `step9-menu-diag-01`):
  - After confirming Clock Set (`modal=0x9c`) and selecting `"No"` on the native IBIS manual-lens prompt (`scene=0x1112`), the camera reached **Native LUMIX S5 Live View HUD** (`scene=0x1001, modal=0x0, state=0x1`).
  - Pressing `KEY_CENTER` (`MENU/SET`) routed all the way into `CENTER_LIVE_HANDLER` (`0x06646e84`), where read-only route observers proved it exited early at **`0x06646efe -> 0x0667a442() == 1`** (shutter S1/S2 active).
- **Static firmware proof** ([audit_menu_shutter_gate.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/audit_menu_shutter_gate.py) -> [menu-shutter-gate-evidence.json](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/menu-shutter-gate-evidence.json)):
  1. **Body Shutter S2 GPIO (`0x7f081038` bit 4)**: `0x06544bac` tests bit 4 active-low (`bpl` -> `r0=1` when `0`), setting raw bit `0x2` -> ROM table `0x070c93a4` logical `0x200` (`KEY_SHUTTER_FULL`) -> `0x06543ea6` forces `0x100` (`KEY_SHUTTER_HALF`).
  2. **Wired Remote Jack Sense ADC (`0x7f031024`, Bank 1 Ch 4)**: `0x06544a02 -> 0x06547882 -> 0x06547a0e` decodes sample `0..0x3a` as mode `3` (remote connected) -> `0x06547966` returns `0x24` -> `0x0654799a` asserts `0x300` (`KEY_SHUTTER_HALF | KEY_SHUTTER_FULL`); sample `0x3c5..0x439` (`0x3ff`) decodes mode `0` (unplugged).
- **Minimal opt-in models**:
  - Added QOM properties `shutter-s2-input-high` (bool, default `false`, ONLY `0x7f081038` bit 4) and `adc1-ch4-sample` (uint32, default `0`, ONLY `0x7f031024`) in [s5-rtos.c](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qemu/s5-rtos.c) + CLI flags `--shutter-s2-input-high` and `--adc1-ch4-sample` in [boot.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/boot.py).
  - Verified in [test_shutter_s2_and_remote_input.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qemu/test_shutter_s2_and_remote_input.py) (**4/4 checks PASS**), plus regressions `test_adc0_ch5_input.py` (**5/5**), `test_adc2_ch0_input.py` (**5/5**), and `test_ram_layout.py` (**83/83**).

---

## 4. Complete Verified Native UI Progression (`step9-main-menu-nav-01`)
Reproducible command:
```bash
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B menu-smp/boot.py \
  --ram 2G --storage-hle --capture-gfx --graphics-hle \
  --spi0-open-bus --spi6-open-bus --tx-serial-hle \
  --lens-release-input-high --shutter-s2-input-high \
  --adc11-sample 768 --adc2-ch0-sample 0x3ff --adc0-ch5-sample 0x3ff --adc1-ch4-sample 0x3ff \
  --linux-peer --isolate-display --isolate-display-ticks 600000 \
  --service-gui-once --probe-ui-mode35 --ui-service-loop --ui-service-postinit \
  --display-clock-test --frame-irq-test-pairs 128 --frame-test-stage-seconds 10 \
  --lcd-refresh-test-pulses 20 --key-step-timeout 90 \
  --key-sequence KEY_CENTER KEY_CENTER KEY_CENTER KEY_UP KEY_CENTER KEY_CENTER KEY_CENTER KEY_CENTER KEY_DOWN \
  --key-target scene=0x70de,modal=0x0 \
  --key-target scene=0x70de,modal=0xe \
  --key-target scene=0x2,modal=0x0 \
  --key-target same_scene,scene=0x2,modal=0x0,pixels_change \
  --key-target scene=0x2,modal=0x9c \
  --key-target scene=0x1112,modal=0x0 \
  --key-target scene=0x1001,modal=0x0 \
  --key-target scene=0x1002,modal=0x0 \
  --seconds 140 \
  --output /Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01
```

| Frame | Scene / Modal / State | Visual Observation | SHA-256 (prefix) |
|---|---|---|---|
| [frame-0000.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0000.png) | `0xffff` / `0x23c` / `0x1` | `"Please set the time zone"` | `441650dc9de75635` |
| [frame-0002.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0002.png) | `0x70de` / `0x0` / `0x20` | Time Zone: **Berlin GMT+1:00** (stable) | `55447019af512b18` |
| [frame-0003.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0003.png) | `0x70de` / `0xe` / `0x20` | `"Please set the clock"` / `"Clock Set"` | `c988d50615ee7350` |
| [frame-0004.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0004.png) | `0x2` / `0x0` / `0x1f` | **Clock Set**: `0 : 00 : 00  1 . JAN . 2020` (zero drift) | `23b536428f86e307` |
| [frame-0005.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0005.png) | `0x2` / `0x0` / `0x1f` | **Clock Set**: `1 : 00 : 00  1 . JAN . 2020` + `"Set"` button enabled | `8bcb428e77635fe9` |
| [frame-0006.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0006.png) | `0x2` / `0x9c` / `0x1f` | `"The clock setting has been completed."` (`1:00 1.JAN.2020`) | `8033e16ff70d3b03` |
| [frame-0007.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0007.png) | `0x1112` / `0x0` / `0x8d` | `"Current focal length setting: 35mm / Change the setting? [Yes] [No]"` | `59dbb6b0334e7e50` |
| [frame-0008.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0008.png) | `0x1001` | `"No memory card"` (`[1][2]`) toast | `d189531ed8f51bf5` |
| [frame-0009.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0009.png) | `0x1001` / `0x0` / `0x1` | **Native Live View HUD** (`STD.`, `FINE L`, `P`, `10s`, `BODY`, `CH1/CH2`, `ISO AUTO`, Battery) | `f2ecaebf1c704089` |
| [frame-0010.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0010.png) | `0x1002` / `0x0` / `0x2` | **Native Main Menu — Photo / Image Quality 1** (`Photo Style STD.`, `Metering Mode`, `Aspect Ratio 3:2`, `Picture Quality FINE`, `Picture Size L 24M`, `HLG Photo OFF`, `High Resolution Mode`, `Long Exposure NR ON`) | `e697bfa7d7e8b7d0` |
| [frame-0011.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-clock-menu/step9-main-menu-nav-01/graphics/frame-0011.png) | `0x10cd` / `0x0` / `0x2` | **Native Main Menu — Video / Image Quality 1** (`Exposure Mode`, `Photo Style STD.`, `Metering Mode`, `ISO Sensitivity (video)`, `Flicker Decrease (Video) OFF`, `Master Pedestal Level 0`, `SS/Gain Operation SEC/ISO`, `i.Dynamic Range OFF`) | `8a793a7bdad3b415` |

All 11 frames `frame-0000.png`..`frame-0010.png` matched bit-for-bit across `step8-main-menu-unlocked-01` and `step9-main-menu-nav-01`.
