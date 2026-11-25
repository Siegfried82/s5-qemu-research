# Timezone Drift Root Cause & Opt-In ADC Bank 2 Ch 0 Verification Report

## 1. Executive Summary (Execution vs Assertions)

| Check | Execution | Assertion | Notes |
| --- | --- | --- | --- |
| Static + live audit (`serial/inspect_timezone_drift.py`) | `True` | `True` | Verified against `03_program.bin` SHA-256 `fc496d62...` + live trace `qa/task-timezone-drift/diag-live-trace/diag_hits.json` |
| Focused hardware + firmware test (`qemu/test_adc2_ch0_input.py`) | `True` | `True` | 5/5 checks passed (`qemu/adc2-ch0-input-verified/verification.json`); all 4 directional bands + idle `0x3ff` verified through `0x7f032014` & original `0x06545554` |
| RAM layout regression (`qemu/test_ram_layout.py`) | `True` | `True` | 83/83 checks passed (`qa/task-timezone-drift/ram-layout-regression/verification.json`) |
| Baseline attempt 1 (`baseline-drift-01`, 120s, `--adc2-ch0-sample` OFF) | `False` (`software_key_probe_stopped`) | `False` | Stopped on intermittent `key_probe` guard (`Interrupt nesting or dispatch-disabled guard is nonzero`) during `KEY_CENTER` release; preserved untouched |
| Baseline attempt 2 (`baseline-drift-02`, 120s, `--adc2-ch0-sample` OFF) | `True` (`time_bound`, exit `0`) | `True` | 10 swaps; 8/8 scene `0x70de` frames distinct (Tehran `GMT +3:30` -> Alaska/Anchorage `GMT -9:00`), reproducing drift |
| Candidate (`candidate-adc2-ch0-0x3ff-01`, 120s, `--adc2-ch0-sample 0x3ff`) | `True` (`time_bound`, exit `0`) | `True` | 5 swaps; 3/3 scene `0x70de` frames byte-identical (Berlin/Paris/Rome/Madrid/Casablanca `GMT +1:00`), drift completely stopped |
| Single-variable control (`verified_single_variable`) | `True` | `True` | Identical QEMU binary (`b12ab0b0...`), identical args/flags except `--adc2-ch0-sample 0x3ff` (`/machine.adc2-ch0-sample: 0 -> 1023`) |

## 2. Proven Root Cause (Original Firmware + Live Trace)

### Primary Drift Source (100% Proven)
1. **Hardware register**: **ADC Bank 2 Channel 0 (`0x7f032014`)**, logical channel `27` (`0x1b`), mapped by firmware ROM table `0x08aa7de8` (ROM `0x070c87a0`).
2. **Firmware sampling**: `0x06544990` calls `0x0659ed98(0x1b, 4, 0x08e21156)` to copy ADC bank 2 ch 0..3 into `[0x08e21130 + 0x26]`. `0x06544a36` passes `[0x08e21130 + 0x26]` to the 4-way directional resistor-ladder decoder `0x06545554`:
   - `0x0000 ± 0x38` (`0..56`) -> raw `0x20` -> mapping table `0x08aa89e4[5]` -> `bank0 0x1` (**`KEY_LEFT`**, UI key `3`)
   - `0x00b8 ± 0x38` (`128..240`) -> raw `0x04` -> `bank0 0x4` (`KEY_UP`, UI key `5`)
   - `0x0183 ± 0x38` (`331..443`) -> raw `0x10` -> `bank0 0x2` (`KEY_RIGHT`, UI key `4`)
   - `0x025a ± 0x38` (`546..658`) -> raw `0x08` -> `bank0 0x8` (`KEY_DOWN`, UI key `6`)
   - Above `658` (e.g., `0x3ff = 1023`, matching 10-bit calibration max `[0x063191c4] = 0x03ff`) -> raw `0x0` (**idle / no key pressed**)
3. **Auto-repeat and scene `0x70de` handler**: Because QEMU's `s5-rtos` ADC model previously returned `0` for every channel except bank 0 ch 11 (`adc11-sample`), ADC bank 2 ch 0 always read `0`, permanently holding `KEY_LEFT` (`bank0 0x1`). The original repeat engine `0x06543de0` (table `0x08aa8204[0]`: `first_delay=25, repeat_period=10`) continuously emitted `bank0 repeat = 0x1` -> `0x0663ecea..0x0663ed22` -> `0x066bddc8(3, 1)` -> UI event `0x0f000002`, param `0x00010003` -> `0x068b7988` -> `0x068b7742` -> `0x068b782c` -> `0x068b759a` -> `0x068b754a`, decrementing timezone ID `[0x08cc4872]` continuously westward (`1 -> 0 -> 38 -> 37 -> ...`) across the 32 active (`kind == 4`) entries of table `0x06ee5cf0`.
4. **Why Task A visually looked like "eastward drift"**: Live breakpoints (`qa/task-timezone-drift/diag-live-trace/diag_hits.json`) proved **0** calls to `0x068b7522` (increment/eastward) and **0** calls to `0x068b7b98` (touch), and continuous calls to `0x068b759a` (decrement/westward) stepping `[0x08cc4872]` `1 -> 0 -> 38`. Because the 32-entry circular timezone table wraps westward faster than the ~9 s LCD refresh swap cadence, slow frame snapshots exhibited temporal aliasing (wagon-wheel effect), making westward wrap-arounds appear as apparent eastward jumps (e.g. Tehran `GMT +3:30` -> Kabul `GMT +4:30` -> ... -> Guam/Sydney `GMT +10:00`).

### Secondary Observed Repeat Source (Documented, Not Causing Timezone Drift)
- **ADC Bank 0 Channel 5 (`0x7f030028`)** -> `[0x08e21130 + 0x0a]` -> 5-way joystick decoder `0x065454d8`: default sample `0` decodes to raw `0x02` -> table `0x08aa8a9c[1]` -> `bank2 0x8` -> repeat table `0x08aa8684[3]` (`first_delay=25, repeat_period=10`) -> `0x0663ede6..0x0663ee68` -> `0x066bddc8(6, 1)` -> UI key `0x00010006` (DOWN repeat).
- Live trace confirmed `0x00010006` hits `0x068b7742` and falls through to default `0x0664e092` (ignored in scene `0x70de`, does not change `[0x08cc4872]`). It was intentionally left unchanged to maintain strict single-variable control; future vertical-menu navigation tasks may expose an opt-in sample for `0x7f030028` (idle above `0x0322 + 0x38 = 858`, e.g. `0x3ff`).

## 3. Minimal Opt-In QEMU & CLI Changes

### Modified Files
- `qemu/s5-rtos.c`: Added opt-in `/machine` uint32 property `adc2-ch0-sample` (default `0`, validated `0..0xffff`), wired solely to ADC bank 2 channel 0 (`0x7f032014`) during conversion in `s5_rtos_adc_write`, and reported as `adc2_ch0_sample` in `adc-state-json`.
- `menu-smp/boot.py`: Added opt-in CLI flag `--adc2-ch0-sample` (default `0`, range `0..4095`), QMP `qom-set /machine adc2-ch0-sample` when nonzero, and `adc2_ch0_sample` metadata in `report["adc_environment"]`. (`scripted_keys`, `key_sequence`, and `key_probe.py` were not touched.)

### Created Files
- `serial/inspect_timezone_drift.py`
- `serial/timezone-drift-evidence.json`
- `serial/timezone-drift.disasm.txt`
- `qemu/test_adc2_ch0_input.py` + `qemu/adc2-ch0-input-verified/verification.json`
- `qa/task-timezone-drift/compare_timezone_drift.py`
- `qa/task-timezone-drift/diag-live-trace/diag_hits.json`
- `qa/task-timezone-drift/ram-layout-regression/verification.json`
- `qa/task-timezone-drift/verification.json`
- `qa/task-timezone-drift/report.md`

## 4. Reproducible Commands

```bash
cd /Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator

# 1. Static + live root-cause audit
lab/.venv/bin/python serial/inspect_timezone_drift.py

# 2. Rebuild QEMU (all temp/build artifacts on SATA)
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp S5_BUILD_JOBS=8 bash toolchain/build.sh

# 3. Focused hardware + original decoder test & RAM layout regression
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B qemu/test_adc2_ch0_input.py --output qemu/adc2-ch0-input-verified
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B qemu/test_ram_layout.py --output qa/task-timezone-drift/ram-layout-regression

# 4. Single-variable 120s A/B runs
COMMON_ARGS=(
  --ram 2G --storage-hle --capture-gfx --graphics-hle
  --spi0-open-bus --spi6-open-bus --tx-serial-hle
  --adc11-sample 0x300
  --isolate-display --isolate-display-ticks 600000
  --linux-peer --service-gui-once --probe-ui-mode35
  --display-clock-test --ui-service-loop --ui-service-postinit
  --frame-irq-test-pairs 128 --frame-test-stage-seconds 10
  --lcd-refresh-test-pulses 8
  --key-event KEY_CENTER --key-click
  --lens-release-input-high --seconds 120
)
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B menu-smp/boot.py "${COMMON_ARGS[@]}" --output qa/task-timezone-drift/baseline-drift-02
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B menu-smp/boot.py "${COMMON_ARGS[@]}" --adc2-ch0-sample 0x3ff --output qa/task-timezone-drift/candidate-adc2-ch0-0x3ff-01

# 5. Automated comparison
python3 -B qa/task-timezone-drift/compare_timezone_drift.py \
  --baseline qa/task-timezone-drift/baseline-drift-02 \
  --candidate qa/task-timezone-drift/candidate-adc2-ch0-0x3ff-01 \
  --focused qemu/adc2-ch0-input-verified \
  --ram-regression qa/task-timezone-drift/ram-layout-regression \
  --output-json qa/task-timezone-drift/verification.json
```

## 5. Single-Variable A/B Comparison

| Metric | Baseline (`baseline-drift-02`, OFF) | Candidate (`candidate-adc2-ch0-0x3ff-01`, `0x3ff`) |
| --- | --- | --- |
| `qemu_sha256` | `b12ab0b0bead16c0e4d5d954f745c829db49d155ae67640bf730eeed4450b702` | `b12ab0b0bead16c0e4d5d954f745c829db49d155ae67640bf730eeed4450b702` |
| `stop_reason` / `qemu_exit_code` | `time_bound` / `0` | `time_bound` / `0` |
| `adc.adc11_sample` | `768` (`0x300`) | `768` (`0x300`) |
| `adc.adc2_ch0_sample` | `0` | `1023` (`0x3ff`) |
| `adc.banks[2].results` | `[0, 0, 0, 0]` (8018 conversions) | `[1023, 0, 0, 0]` (6999 conversions) |
| `lens_release_input.enabled` | `True` (8018 affected reads) | `True` (6999 affected reads) |
| `control_words_08a928b0` | `["0x0", "0x20", "0x0", "0x1"]` | `["0x0", "0x20", "0x0", "0x1"]` |
| `modal_id_08a92898` | `0x0` | `0x0` |
| `source_scene` / `render_scene` | `0x70de` / `0x70de` | `0x70de` / `0x70de` |
| `graphics_error` | `None` | `None` |
| `graphics_draws` / `graphics_swaps` | `423` / `10` | `204` / `5` |
| `lcd_refresh_completed_requests` | `8` | `4` |
| Scene `0x70de` frames (`frame-0002+`) | `8` frames, **8 unique SHA-256s** (continuous drift) | `3` frames, **1 unique SHA-256** (zero drift) |
| Visually verified start -> end timezone | Tehran `GMT +3:30` -> Alaska/Anchorage `GMT -9:00` | Berlin/Paris/Rome/Madrid/Casablanca `GMT +1:00` -> unchanged |

### Baseline (`baseline-drift-02`) PNG Inventory
| Frame | Bytes | SHA-256 | Visual Content |
| --- | --- | --- | --- |
| `frame-0000.png` | 20624 | `441650dc9de75635c6e5009d8937594af0b4a85bb0a6f4dd93725f09cd885fc7` | Timezone prompt (`0x1001` + modal `0x23c`) |
| `frame-0001.png` | 21421 | `4cc91201a32a93d87c7c03470860c59453861af28b03792dea514061bb14b7b8` | Timezone prompt after first redraw |
| `frame-0002.png` | 69029 | `5716bb8ee9b24138a1bbb930479a8cf0efc4782b3e8a013ef32c87839295e199` | Scene `0x70de`: Tehran `GMT +3:30` |
| `frame-0003.png` | 68056 | `16c3b11967010d0258f0820a36d9b0a149fe995ddbaa039ea92d39b116e2175a` | Scene `0x70de`: drifted |
| `frame-0004.png` | 69465 | `9668abb6fa402a8f27282dd5c085373a7efbd3f0c3497134872ed3dfa1a5c23c` | Scene `0x70de`: drifted |
| `frame-0005.png` | 81040 | `775d52d077a9786aa960ff3b02b39c503c2c9a36b034cf0c300c1cb70cf6c9bc` | Scene `0x70de`: drifted |
| `frame-0006.png` | 80849 | `26eb19e49cdaff74ff66b32ed6bb5f7cb2f568dca63efc676b360d3ca8a11919` | Scene `0x70de`: drifted |
| `frame-0007.png` | 84047 | `e87ede96af7ea1e050b962d4df671e22b40ebced435da1ef3675e80deaa6125f` | Scene `0x70de`: drifted |
| `frame-0008.png` | 72847 | `68a8024b6be8ca6e8ec73d02c40fa72f1bd15682928bfd61698d581e731a1596` | Scene `0x70de`: drifted |
| `frame-0009.png` | 78775 | `39b97540488cd8051343d73ece01569ce26bb3af11639eef0664c0c43995fc29` | Scene `0x70de`: Alaska/Anchorage `GMT -9:00` |

### Candidate (`candidate-adc2-ch0-0x3ff-01`) PNG Inventory
| Frame | Bytes | SHA-256 | Visual Content |
| --- | --- | --- | --- |
| `frame-0000.png` | 20624 | `441650dc9de75635c6e5009d8937594af0b4a85bb0a6f4dd93725f09cd885fc7` | Timezone prompt (`0x1001` + modal `0x23c`) |
| `frame-0001.png` | 21421 | `4cc91201a32a93d87c7c03470860c59453861af28b03792dea514061bb14b7b8` | Timezone prompt after first redraw |
| `frame-0002.png` | 94939 | `55447019af512b18fa899f4b5d3103f212d4bed17af48ff51b4f373b25845b0b` | Scene `0x70de`: Berlin/Paris/Rome/Madrid/Casablanca `GMT +1:00` |
| `frame-0003.png` | 94939 | `55447019af512b18fa899f4b5d3103f212d4bed17af48ff51b4f373b25845b0b` | Scene `0x70de`: Berlin/Paris/Rome/Madrid/Casablanca `GMT +1:00` (identical) |
| `frame-0004.png` | 94939 | `55447019af512b18fa899f4b5d3103f212d4bed17af48ff51b4f373b25845b0b` | Scene `0x70de`: Berlin/Paris/Rome/Madrid/Casablanca `GMT +1:00` (identical) |

## 6. Proven vs Unverified Boundaries

### Proven
- ADC bank 2 channel 0 (`0x7f032014`) returning `0` is the exact hardware source of the `scene 0x70de` timezone drift via `0x06545554` -> `raw 0x20` -> `bank0 0x1` (`KEY_LEFT`) -> repeat `0x00010003` -> `0x068b7742` -> `0x068b759a` -> `0x068b754a`.
- Setting opt-in `--adc2-ch0-sample 0x3ff` alone stops the timezone drift completely while preserving original firmware integrity (`original_firmware_instructions_modified == false`), `scene 0x70de` (`modal_id == 0x0`, `graphics_error == None`), and all RAM/MMIO layout invariants.

### Unverified / Out of Scope
- Physical resistor-ladder voltage levels and analog pull-up values on real LUMIX S5 hardware are not measured; `0x3ff` is chosen solely from firmware decoder bands in `0x06545554` and calibration constant `[0x063191c4] = 0x03ff`.
- ADC bank 0 channel 5 (`0x7f030028`) still returns `0` by default (emitting `bank2 0x8` / UI key `0x00010006` repeat, ignored in scene `0x70de`), and other unrelated GPIOs/ADC channels remain at their existing defaults.
- Main menu entry (`menu_rendered == False`) was not tested in this single-variable run (only single `KEY_CENTER` confirmation into scene `0x70de`).
