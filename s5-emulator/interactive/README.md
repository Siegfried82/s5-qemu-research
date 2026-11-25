# LUMIX S5 Interactive Emulator (`interactive/s5_interactive.py`)

Single-command loopback web console that boots the unmodified Panasonic LUMIX S5 v2.9 firmware in QEMU (`menu-smp/boot.py`), displays only real validated `eglSwapBuffers` PNG frames, and dispatches live camera button presses strictly through the verified original `KeyProbe` software-key queue (`0x067f0dda` -> `queue 0x115` -> original input task 285 -> original dispatcher).

## Quick Start

```sh
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B interactive/s5_interactive.py --boot-to menu --open-browser
```

### Boot Modes (`--boot-to`)

| Mode | Scripted Boot Steps | Initial Screen When Interactive Controls Unlock |
|---|---|---|
| `menu` *(default)* | 8 verified clicks | Native LUMIX S5 Main Menu (`scene 0x1002`, Photo / Image Quality 1) |
| `liveview` | 7 verified clicks | Native LUMIX S5 Live View HUD (`scene 0x1001`) |
| `wizard` | 0 scripted clicks | First-time setup screen (`Please set the time zone`) |

### CLI Options

```sh
python3 -B interactive/s5_interactive.py \
  [--boot-to menu|liveview|wizard] \
  [--host 127.0.0.1] \
  [--port 0] \
  [--max-seconds 1800] \
  [--output /Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/interactive/sessions/<name>] \
  [--open-browser]
```

- All session outputs default to `/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/interactive/sessions/session-YYYYMMDD-HHMMSS-<hex>`.
- Pure Python 3 standard library + Pillow/CoreGraphics already used by `boot.py`; zero external web or CDN dependencies.

## Verified Real Keys (`0x06f1003c` table)

- **D-pad**: `KEY_UP` (ArrowUp / W), `KEY_DOWN` (ArrowDown / S), `KEY_LEFT` (ArrowLeft / A), `KEY_RIGHT` (ArrowRight / D), `KEY_CENTER` (MENU/SET, Enter / Space)
- **Camera Buttons**: `KEY_DELETE` (BACK / Trash, Esc / Backspace), `KEY_DISPLAY` (DISP., I), `KEY_Q_MENU` (Q.MENU, Q), `KEY_PLAY` (PLAY, P), `KEY_ISO`, `KEY_WB`, `KEY_EXPOSE` (+/-), `KEY_SET`
- **Dials**: `FRONT_DIAL_LEFT` / `FRONT_DIAL_RIGHT`, `REAR_DIAL_LEFT` / `REAR_DIAL_RIGHT`, `CONTROL_DIAL_LEFT` / `CONTROL_DIAL_RIGHT`

## Security & Integrity Boundaries

- **Loopback only**: refuses non-loopback bind addresses (`0.0.0.0`, LAN IPs, hostnames).
- **Per-session CSRF token**: `POST /api/key` and `POST /api/stop` require `X-S5-Session-Token`.
- **Control channel auth**: `boot.py` authenticates to the local `127.0.0.1` TCP control socket with a per-session secret token.
- **Validated real PNGs only**: `/api/frame/<filename>` enforces strict path containment inside `<session>/graphics/` and validates PNG signature, IHDR, IDAT, CRCs, and IEND before returning exact raw bytes (`409` if incomplete).
- **Local HLE/TEST scope**: unmodified firmware bytes; no guest UI/TCB/event-flag writes; DSP/ISP/sensor/SD card remain unmodeled.
