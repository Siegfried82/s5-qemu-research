# Interactive LUMIX S5 Emulator — Engineering & Verification Report

## Summary
Built a single-command, loopback-only interactive web emulator (`interactive/s5_interactive.py`) driving unmodified Panasonic LUMIX S5 v2.9 firmware in QEMU (`menu-smp/boot.py`). Every interactive button click flows strictly through the verified original `KeyProbe` software-key queue (`0x067f0dda` -> `queue 0x115` -> original input task 285 -> original dispatcher -> real `eglSwapBuffers` PNG). Zero firmware instructions were patched, zero guest UI/TCB/event flags were modified by the host, and zero synthetic/placeholder camera frames are ever rendered.

## One-Line Launch Command
```sh
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B interactive/s5_interactive.py --boot-to menu --open-browser
```
Supports `--boot-to menu` (default, 8 verified steps -> Main Menu `0x1002`), `--boot-to liveview` (7 steps -> Live View HUD `0x1001`), and `--boot-to wizard` (0 scripted keys -> Time Zone prompt).

---

## Components Built / Updated

1. **Expanded Verified Real Keys** ([key_probe.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/key_probe.py), [key_sequence_scheduler.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/key_sequence_scheduler.py), [test_key_probe.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/test_key_probe.py)):
   - Preserved all 8 existing keys and added 11 verified Bank 0 (`0x6643776`) & Bank 1 (`0x664378c`) real keys directly from static firmware table `0x06f1003c` ([key-event-evidence.json](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/key-event-evidence.json)): `KEY_DISPLAY`, `KEY_DELETE`, `KEY_ISO`, `KEY_WB`, `KEY_EXPOSE`, `CONTROL_DIAL_LEFT`, `CONTROL_DIAL_RIGHT`, `FRONT_DIAL_LEFT`, `FRONT_DIAL_RIGHT`, `REAR_DIAL_LEFT`, `REAR_DIAL_RIGHT` (19 total).
   - Added `KeyProbe.recover_pre_call_error()` so pre-call interactive timeouts can clear cleanly ONLY when `_saved is None`.
2. **Extended Interactive LCD Refresh Pulse Bound** ([lcd_refresh_probe.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/lcd_refresh_probe.py)):
   - Added opt-in `allow_extended_pulses=False` (default preserves `1 <= max_pulses <= 64` and 256 drain bound).
   - When `True`, permits `1 <= max_pulses <= 4096`, caps recorded `pulses` (64) and `events` (256) with exact `total_pulses`/`total_events`, and scales `passive_receive_observations` bound (`max(256, max_pulses * 32)`).
3. **Loopback Control Protocol** ([interactive_control.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/interactive_control.py)):
   - Pure stdlib `127.0.0.1` JSONL TCP server/client with constant-time per-session token authentication, single-client enforcement, bounded pending queue (max 8 pending clicks), and structured live event snapshots.
4. **Opt-In Interactive Mode in `boot.py`** ([boot.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/menu-smp/boot.py)):
   - Default OFF (`--interactive-control-port 0`); 100% backward compatible.
   - Adds `--interactive-control-port`, `--interactive-control-token`, and `--interactive-ready-target`.
   - Allows `--seconds` up to 3600 and `--lcd-refresh-test-pulses` up to 4096 when interactive mode is enabled, polls `emu.resume_all(timeout=0.15)` while interactive-ready, selects RTOS CPU0 on `TimeoutError` (preventing GDB `E14` faults when Linux CPU3 was interrupted), dispatches full press+release cycles through `KeyProbe`, and exits cleanly (`0`) with finalized `result.json` on `stop` or `SIGINT`/`SIGTERM`.
5. **Interactive Web Console & Launcher** ([s5_interactive.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/interactive/s5_interactive.py), [README.md](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/interactive/README.md)):
   - Loopback-only HTTP server (`127.0.0.1` / `localhost` / `::1`), per-session CSRF token (`X-S5-Session-Token`) + `Origin` check on `POST /api/key` & `POST /api/stop`, strict path containment & `validate_png_bytes` (`IEND`/CRC) on `GET /api/frame/<filename>`, `textContent`-only DOM rendering, and CSP headers.
   - Dark-mode browser UI with 1024×768 native LCD display, camera buttons/dials + keyboard shortcuts, frame scrubber & PNG download, live telemetry, key log, Stop button, and honest local HLE/TEST scope banner.

---

## Verification Results

### 1. Host Unit & Regression Test Suites
| Suite | Executed OK | Assertions Passed | Count |
|---|---|---|---|
| [test_interactive_unit.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/test_interactive_unit.py) | `true` | `true` | 9 / 9 |
| [test_key_probe.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/test_key_probe.py) | `true` | `true` | 28 / 28 |
| [test_key_sequence_scheduler.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/test_key_sequence_scheduler.py) | `true` | `true` | 15 / 15 |
| [test_lcd_refresh_probe.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/test_lcd_refresh_probe.py) | `true` | `true` | 9 / 9 |
| [test_frame_viewer.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-d/test_frame_viewer.py) | `true` | `true` | 10 / 10 |

### 2. Live End-to-End QEMU Interactive Verification ([test_live_interactive.py](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/test_live_interactive.py))
- Session output: [live-session-01](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/live-session-01/result.json)
- Reached `interactive_ready == true` on Native Main Menu (`scene 0x1002`, `modal 0x0`) in **109.73s**; completed full 4-key live test + clean `POST /api/stop` shutdown in **143.28s**.
- Final `result.json`: `qemu_exit_code=0`, `graphics_error=null`, `stop_reason="user_requested"`, `original_firmware_instructions_modified=false`, `peer_flags_manually_cleared=false`, `graphics_swaps=15`, `graphics_draws=1056`, `interactive_clicks=4`.

| Stage / Key | Key Consume | Swap Frame | Scene / Modal / State | PNG SHA-256 (prefix) | Visual Observation (verified via `read_file`) |
|---|---|---|---|---|---|
| Ready (`--boot-to menu`) | — | [frame-0010.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/live-session-01/graphics/frame-0010.png) | `0x1002` / `0x0` / `0x2` | `e697bfa7d7e8` | Native Main Menu (Photo / Image Quality 1): yellow focus border on Column 1 red Camera category icon |
| 1. `KEY_RIGHT` | `167.3 ms` | [frame-0011.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/live-session-01/graphics/frame-0011.png) | `0x1002` / `0x0` / `0x2` | `810f442ad973` | Focus moved to Column 2: Image Quality 1 icon highlighted solid yellow and header turned yellow |
| 2. `KEY_CENTER` | `120.0 ms` | [frame-0012.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/live-session-01/graphics/frame-0012.png) | `0x1002` / `0x0` / `0x2` | `a018b47b83d4` | Focus entered Column 3 item list: **`Photo Style STD.`** row highlighted with solid yellow bar |
| 3. `KEY_DELETE` (BACK) | `139.6 ms` | [frame-0013.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/live-session-01/graphics/frame-0013.png) | `0x1001` / `0x0` / `0x1` | `f2ecaebf1c70` | Newly added verified `KEY_DELETE` (`index 10, bank 0, 0x80`) exited Main Menu cleanly back to Native Live View HUD |
| 4. `KEY_DOWN` | `159.0 ms` | [frame-0014.png](file:///Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/qa/task-interactive/live-session-01/graphics/frame-0014.png) | `0x1001` / `0x0` / `0x1` | `0601dd9295a1` | Live View HUD redrew cleanly (`868 -> 1056` draws; SD `[1][2]` indicator blinked on HUD) |

### Engineering Issue Found & Fixed During Live E2E Testing
- During interactive boot with `--linux-peer`, interrupting `emu.resume_all(timeout=0.25)` could leave QEMU GDB's implicit active thread on CPU3 (Linux MMU), causing subsequent RTOS RAM reads (`0x08aa1103`) to fail with `GDB RSP error: E14`. Fixed cleanly in `menu-smp/boot.py` by restoring `emu.select_cpu(0)` immediately on `TimeoutError` (and guarding RTOS reads whenever `emu.selected_cpu == 3`).
