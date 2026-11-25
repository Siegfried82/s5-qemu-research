#!/usr/bin/env python3
"""Interactive loopback web launcher for the Panasonic LUMIX S5 QEMU emulator.

Starts `menu-smp/boot.py` in opt-in interactive control mode, serves ONLY real
validated PNG frames committed by `eglSwapBuffers`, and forwards user camera
button clicks strictly through the verified original `KeyProbe` software-key
queue (`0x067f0dda -> queue 0x115 -> task285`).

Pure Python 3 standard library. Zero external web/network/CDN dependencies.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

EMULATOR_ROOT = Path(__file__).resolve().parents[1]
MENU_SMP_DIR = EMULATOR_ROOT / "menu-smp"
VIEWER_DIR = EMULATOR_ROOT / "viewer"
SESSIONS_ROOT = EMULATOR_ROOT / "interactive" / "sessions"
DEFAULT_TMPDIR = Path("/Volumes/SATA-1TB/Projects/s5-qemu/tmp")

for _p in (str(MENU_SMP_DIR), str(VIEWER_DIR), str(EMULATOR_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from interactive_control import InteractiveControlServer, VALID_KEYS  # noqa: E402
from s5_frame_viewer import validate_loopback_host, validate_png_bytes  # noqa: E402

FRAME_FILENAME_RE = re.compile(r"^frame-[0-9A-Za-z_-]+\.png$")
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_POST_BYTES = 4096

BOOT_PROFILES: dict[str, dict[str, Any]] = {
    "menu": {
        "description": "Boot through first-time setup wizard into Native Main Menu (scene 0x1002)",
        "key_sequence": [
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_UP",
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_CENTER",
        ],
        "key_targets": [
            "scene=0x70de,modal=0x0",
            "scene=0x70de,modal=0xe",
            "scene=0x2,modal=0x0",
            "same_scene,scene=0x2,modal=0x0,pixels_change",
            "scene=0x2,modal=0x9c",
            "scene=0x1112,modal=0x0",
            "scene=0x1001,modal=0x0",
        ],
        "ready_target": "scene=0x1002,modal=0x0",
    },
    "liveview": {
        "description": "Boot through first-time setup wizard into Native Live View HUD (scene 0x1001)",
        "key_sequence": [
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_UP",
            "KEY_CENTER",
            "KEY_CENTER",
            "KEY_CENTER",
        ],
        "key_targets": [
            "scene=0x70de,modal=0x0",
            "scene=0x70de,modal=0xe",
            "scene=0x2,modal=0x0",
            "same_scene,scene=0x2,modal=0x0,pixels_change",
            "scene=0x2,modal=0x9c",
            "scene=0x1112,modal=0x0",
        ],
        "ready_target": "scene=0x1001,modal=0x0",
    },
    "wizard": {
        "description": "Stop at the first native setup screen (Time Zone prompt) with zero scripted keys",
        "key_sequence": [],
        "key_targets": [],
        "ready_target": "",
    },
}

SUPPORTED_KEY_SPEC: list[dict[str, str]] = [
    {"key": "KEY_UP", "label": "UP", "group": "dpad", "shortcut": "ArrowUp / W"},
    {"key": "KEY_DOWN", "label": "DOWN", "group": "dpad", "shortcut": "ArrowDown / S"},
    {"key": "KEY_LEFT", "label": "LEFT", "group": "dpad", "shortcut": "ArrowLeft / A"},
    {"key": "KEY_RIGHT", "label": "RIGHT", "group": "dpad", "shortcut": "ArrowRight / D"},
    {"key": "KEY_CENTER", "label": "MENU / SET", "group": "dpad", "shortcut": "Enter / Space"},
    {"key": "KEY_DELETE", "label": "BACK / Trash", "group": "buttons", "shortcut": "Escape / Backspace"},
    {"key": "KEY_DISPLAY", "label": "DISP.", "group": "buttons", "shortcut": "I"},
    {"key": "KEY_Q_MENU", "label": "Q.MENU", "group": "buttons", "shortcut": "Q"},
    {"key": "KEY_PLAY", "label": "PLAY", "group": "buttons", "shortcut": "P"},
    {"key": "KEY_ISO", "label": "ISO", "group": "buttons", "shortcut": ""},
    {"key": "KEY_WB", "label": "WB", "group": "buttons", "shortcut": ""},
    {"key": "KEY_EXPOSE", "label": "+/-", "group": "buttons", "shortcut": ""},
    {"key": "KEY_SET", "label": "SET", "group": "buttons", "shortcut": ""},
    {"key": "FRONT_DIAL_LEFT", "label": "Front -", "group": "dials", "shortcut": ""},
    {"key": "FRONT_DIAL_RIGHT", "label": "Front +", "group": "dials", "shortcut": ""},
    {"key": "REAR_DIAL_LEFT", "label": "Rear -", "group": "dials", "shortcut": ""},
    {"key": "REAR_DIAL_RIGHT", "label": "Rear +", "group": "dials", "shortcut": ""},
    {"key": "CONTROL_DIAL_LEFT", "label": "Ctrl -", "group": "dials", "shortcut": ""},
    {"key": "CONTROL_DIAL_RIGHT", "label": "Ctrl +", "group": "dials", "shortcut": ""},
]

SCOPE_INFO = {
    "summary": (
        "Local HLE/TEST interactive session running unmodified Panasonic LUMIX S5 v2.9 firmware "
        "in QEMU with software EGL/OpenVG rendering and verified original KeyProbe queue injection "
        "(0x067f0dda -> queue 0x115 -> task285)."
    ),
    "conditions": [
        "Original firmware instructions modified: false",
        "Guest UI/TCB/event flags patched by host: false",
        "Placeholder/synthetic camera screens: never generated",
        "Hardware inputs modeled opt-in: lens-release high, shutter-S2 high, idle resistor-ladder ADCs (0x3ff)",
        "Unmodeled hardware: image sensor, DSP/ISP, physical LCD panel clock, SD card storage, Wi-Fi/BT",
    ],
}


def build_boot_command(
    *,
    boot_to: str,
    output_dir: Path,
    max_seconds: int,
    control_port: int,
    control_token: str,
    lcd_pulses: int = 2048,
) -> list[str]:
    if boot_to not in BOOT_PROFILES:
        raise ValueError(f"Unsupported --boot-to profile: {boot_to!r}")
    profile = BOOT_PROFILES[boot_to]
    cmd = [
        sys.executable,
        "-B",
        str(MENU_SMP_DIR / "boot.py"),
        "--ram",
        "2G",
        "--storage-hle",
        "--capture-gfx",
        "--graphics-hle",
        "--spi0-open-bus",
        "--spi6-open-bus",
        "--tx-serial-hle",
        "--lens-release-input-high",
        "--shutter-s2-input-high",
        "--adc11-sample",
        "768",
        "--adc2-ch0-sample",
        "0x3ff",
        "--adc0-ch5-sample",
        "0x3ff",
        "--adc1-ch4-sample",
        "0x3ff",
        "--linux-peer",
        "--isolate-display",
        "--isolate-display-ticks",
        "600000",
        "--service-gui-once",
        "--probe-ui-mode35",
        "--ui-service-loop",
        "--ui-service-postinit",
        "--display-clock-test",
        "--frame-irq-test-pairs",
        "128",
        "--frame-test-stage-seconds",
        "10",
        "--lcd-refresh-test-pulses",
        str(lcd_pulses),
        "--key-step-timeout",
        "90",
        "--seconds",
        str(max_seconds),
        "--interactive-control-port",
        str(control_port),
        "--interactive-control-token",
        control_token,
        "--output",
        str(output_dir),
    ]
    if profile["key_sequence"]:
        cmd.append("--key-sequence")
        cmd.extend(profile["key_sequence"])
    for target in profile["key_targets"]:
        cmd.extend(["--key-target", target])
    if profile["ready_target"]:
        cmd.extend(["--interactive-ready-target", profile["ready_target"]])
    return cmd


def _is_within_directory(base_real: str, target_real: str) -> bool:
    try:
        return os.path.commonpath([base_real, target_real]) == base_real and target_real != base_real
    except ValueError:
        return False


class SessionFrameStore:
    """Strict read-only PNG validator and frame index inside <session_dir>/graphics/."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = Path(session_dir)
        self._lock = threading.Lock()
        self._png_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}

    def _graphics_dir_real(self) -> str | None:
        session_real = os.path.realpath(str(self.session_dir))
        if not os.path.isdir(session_real):
            return None
        candidate = os.path.join(session_real, "graphics")
        if os.path.islink(candidate) or not os.path.isdir(candidate):
            return None
        graphics_real = os.path.realpath(candidate)
        if not _is_within_directory(session_real, graphics_real):
            return None
        return graphics_real

    def resolve_frame_file(self, filename: str) -> tuple[str | None, int, str | None]:
        """Resolve a frame PNG strictly inside <session_dir>/graphics/.

        Returns (real_path, http_status_if_error, error_code).
        """
        if not isinstance(filename, str) or not filename:
            return None, 400, "invalid_filename"
        if "\x00" in filename or "/" in filename or "\\" in filename or ".." in filename:
            return None, 400, "path_traversal_rejected"
        if not FRAME_FILENAME_RE.match(filename):
            return None, 400, "invalid_filename"

        graphics_real = self._graphics_dir_real()
        if graphics_real is None:
            return None, 404, "graphics_dir_missing"

        candidate = os.path.join(graphics_real, filename)
        if os.path.islink(candidate):
            return None, 403, "symlink_rejected"
        resolved = os.path.realpath(candidate)
        if not _is_within_directory(graphics_real, resolved):
            return None, 403, "path_escape_rejected"
        if not os.path.exists(resolved):
            return None, 404, "missing_file"
        if not os.path.isfile(resolved):
            return None, 403, "not_a_file"
        return resolved, 200, None

    def read_validated_png(self, filename: str) -> tuple[bytes | None, int, dict[str, Any]]:
        resolved, status, err = self.resolve_frame_file(filename)
        if err or resolved is None:
            return None, status, {"png_status": err, "valid": False}
        try:
            st = os.stat(resolved)
        except OSError:
            return None, 404, {"png_status": "missing_file", "valid": False}
        if st.st_size > MAX_PNG_BYTES:
            return None, 409, {"png_status": "exceeds_max_png_bytes", "valid": False}
        try:
            with open(resolved, "rb") as fh:
                data = fh.read(MAX_PNG_BYTES + 1)
        except OSError:
            return None, 404, {"png_status": "read_failed", "valid": False}
        if len(data) > MAX_PNG_BYTES:
            return None, 409, {"png_status": "exceeds_max_png_bytes", "valid": False}
        check = validate_png_bytes(data)
        if not check["valid"]:
            return None, 409, {
                "png_status": "incomplete_png",
                "validation_reason": check["reason"],
                "valid": False,
                "png_bytes": len(data),
            }
        meta = {
            "png_status": "ready",
            "valid": True,
            "png_bytes": len(data),
            "png_sha256": hashlib.sha256(data).hexdigest(),
            "width": check["width"],
            "height": check["height"],
        }
        return data, 200, meta

    def inspect_png_meta(self, filename: str) -> dict[str, Any]:
        resolved, _status, err = self.resolve_frame_file(filename)
        if err or resolved is None:
            return {"png_status": err or "missing_file", "ready": False}
        try:
            st = os.stat(resolved)
        except OSError:
            return {"png_status": "missing_file", "ready": False}
        cache_key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            cached = self._png_cache.get(filename)
            if cached and cached[0] == cache_key[0] and cached[1] == cache_key[1]:
                return dict(cached[2])
        _, _http, meta = self.read_validated_png(filename)
        out = {
            "png_status": meta.get("png_status"),
            "ready": bool(meta.get("valid")),
            "png_bytes": meta.get("png_bytes"),
            "png_sha256": meta.get("png_sha256"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "validation_reason": meta.get("validation_reason"),
        }
        if out["ready"]:
            with self._lock:
                self._png_cache[filename] = (cache_key[0], cache_key[1], dict(out))
        return out

    def read_result_summary(self) -> dict[str, Any]:
        session_real = os.path.realpath(str(self.session_dir))
        if not os.path.isdir(session_real):
            return {"present": False}
        result_path = os.path.join(session_real, "result.json")
        if os.path.islink(result_path) or not os.path.isfile(result_path):
            return {"present": False}
        try:
            data = json.loads(Path(result_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"present": False, "parse_error": True}
        if not isinstance(data, dict):
            return {"present": False, "parse_error": True}
        return {
            "present": True,
            "stop_reason": data.get("stop_reason"),
            "qemu_exit_code": data.get("qemu_exit_code"),
            "graphics_draws": data.get("graphics_draws"),
            "graphics_swaps": data.get("graphics_swaps"),
            "graphics_error": data.get("graphics_error"),
            "menu_rendered": data.get("menu_rendered"),
            "original_ui_state_at_stop": data.get("original_ui_state_at_stop"),
        }


class InteractiveSessionController:
    """Coordinates InteractiveControlServer, SessionFrameStore, and optional boot.py child."""

    def __init__(
        self,
        *,
        session_dir: Path,
        boot_to: str = "menu",
        max_seconds: int = 1800,
        control_server: InteractiveControlServer | None = None,
        csrf_token: str | None = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.boot_to = boot_to
        self.max_seconds = max_seconds
        self.started_at = time.monotonic()
        self.csrf_token = csrf_token if csrf_token is not None else secrets.token_hex(24)
        self.control = control_server if control_server is not None else InteractiveControlServer()
        self._owns_control = control_server is None
        self.frames = SessionFrameStore(self.session_dir)

        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._proc_exit_code: int | None = None
        self._stderr_tail: list[str] = []
        self._stdout_tail: list[str] = []
        self._stop_initiated = False

    def start_emulator(self) -> None:
        self.session_dir.parent.mkdir(parents=True, exist_ok=True)
        if self.session_dir.exists():
            raise FileExistsError(f"Session output directory already exists: {self.session_dir}")
        tmp_dir = Path(os.environ.get("TMPDIR") or str(DEFAULT_TMPDIR))
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["TMPDIR"] = str(tmp_dir)
        env["PYTHONUNBUFFERED"] = "1"
        cmd = build_boot_command(
            boot_to=self.boot_to,
            output_dir=self.session_dir,
            max_seconds=self.max_seconds,
            control_port=self.control.port,
            control_token=self.control.token,
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(EMULATOR_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._proc_lock:
            self._proc = proc
        threading.Thread(target=self._drain_pipe, args=(proc.stdout, self._stdout_tail), daemon=True).start()
        threading.Thread(target=self._drain_pipe, args=(proc.stderr, self._stderr_tail), daemon=True).start()
        threading.Thread(target=self._wait_child, args=(proc,), daemon=True).start()

    def _drain_pipe(self, pipe: Any, target: list[str]) -> None:
        if pipe is None:
            return
        try:
            for line in pipe:
                text = line.rstrip("\r\n")
                if not text:
                    continue
                with self._proc_lock:
                    target.append(text)
                    if len(target) > 64:
                        del target[:-64]
        except OSError:
            pass

    def _wait_child(self, proc: subprocess.Popen[str]) -> None:
        rc = proc.wait()
        with self._proc_lock:
            self._proc_exit_code = rc

    def request_stop(self, reason: str = "user_requested") -> dict[str, Any]:
        with self._proc_lock:
            self._stop_initiated = True
            proc = self._proc
        sent = self.control.request_stop(reason=reason)
        if not sent and proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
        return {"ok": True, "status": "stopping", "control_notified": sent}

    def shutdown_and_wait(self, timeout: float = 15.0) -> int | None:
        self.request_stop(reason="launcher_shutdown")
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
            with self._proc_lock:
                self._proc_exit_code = proc.returncode
        if self._owns_control:
            self.control.close()
        with self._proc_lock:
            return self._proc_exit_code

    def build_status_payload(self) -> dict[str, Any]:
        ctrl = self.control.snapshot()
        with self._proc_lock:
            proc = self._proc
            if proc is not None and self._proc_exit_code is None:
                polled = proc.poll()
                if polled is not None:
                    self._proc_exit_code = polled
            running = proc is not None and self._proc_exit_code is None
            exit_code = self._proc_exit_code
            stderr_tail = list(self._stderr_tail[-10:])
            stop_initiated = self._stop_initiated

        frame_list: list[dict[str, Any]] = []
        for idx, sw in enumerate(ctrl["swaps"]):
            fname = sw.get("frame_id")
            if fname and FRAME_FILENAME_RE.match(fname):
                png_meta = self.frames.inspect_png_meta(fname)
            else:
                png_meta = {"png_status": "invalid_filename", "ready": False}
            ready = bool(png_meta.get("ready"))
            frame_list.append(
                {
                    "swap_seq": idx,
                    "swap_count": sw.get("swap_count"),
                    "filename": fname,
                    "draw_calls": sw.get("draw_calls"),
                    "render_scene": sw.get("render_scene"),
                    "render_modal": sw.get("render_modal"),
                    "source_scene": sw.get("source_scene"),
                    "source_modal": sw.get("source_modal"),
                    "ui_state": sw.get("ui_state"),
                    "pixels_sha256": sw.get("pixels_sha256"),
                    "ts": sw.get("ts"),
                    "png_status": png_meta.get("png_status"),
                    "png_bytes": png_meta.get("png_bytes"),
                    "png_sha256": png_meta.get("png_sha256"),
                    "width": png_meta.get("width"),
                    "height": png_meta.get("height"),
                    "ready": ready,
                    "url": f"/api/frame/{fname}" if ready and fname else None,
                    "download_url": f"/api/frame/{fname}?download=1" if ready and fname else None,
                }
            )
        ready_frames = [f for f in frame_list if f["ready"]]
        latest_ready = ready_frames[-1] if ready_frames else None
        result_summary = self.frames.read_result_summary()

        return {
            "session": {
                "session_name": self.session_dir.name,
                "boot_to": self.boot_to,
                "boot_profile_description": BOOT_PROFILES.get(self.boot_to, {}).get("description", ""),
                "max_seconds": self.max_seconds,
                "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
                "csrf_token": self.csrf_token,
                "process_running": running,
                "process_exit_code": exit_code,
                "stop_initiated": stop_initiated,
                "stderr_tail": stderr_tail,
                "result": result_summary,
            },
            "control": {
                "connected": ctrl["connected"],
                "authenticated": ctrl["authenticated"],
                "interactive_ready": ctrl["interactive_ready"],
                "stopped": ctrl["stopped"],
                "stop_reason": ctrl["stop_reason"],
                "current_stage": ctrl["current_stage"],
                "boot_stages": ctrl["boot_stages"],
                "ui_state": ctrl["ui_state"],
                "pending_keys": ctrl["pending_keys"],
                "pending_count": ctrl["pending_count"],
                "max_pending": ctrl["max_pending"],
                "active_key": ctrl["active_key"],
                "key_history": ctrl["key_history"],
                "errors": ctrl["errors"],
            },
            "frames": frame_list,
            "ready_frame_count": len(ready_frames),
            "latest_ready_frame": latest_ready,
            "supported_keys": SUPPORTED_KEY_SPEC,
            "scope": SCOPE_INFO,
        }


INDEX_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="s5-session-token" content="__CSRF_TOKEN__">
<title>LUMIX S5 Interactive Emulator</title>
<style>
  :root {
    --bg: #0e1014;
    --panel: #161a21;
    --panel2: #1d232d;
    --border: #2c3442;
    --text: #e8edf5;
    --muted: #9aa5b6;
    --accent: #f0b429;
    --accent-dim: #b58316;
    --green: #38c172;
    --red: #e55353;
    --blue: #4ea1ff;
    --btn-bg: #232a36;
    --btn-hover: #2f3848;
    --btn-active: #3d485d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    line-height: 1.4;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 16px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 600;
    font-size: 15px;
  }
  .brand-tag {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: #111;
    background: var(--accent);
    padding: 2px 7px;
    border-radius: 3px;
  }
  .header-right {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    background: var(--panel2);
    border: 1px solid var(--border);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--muted);
  }
  .pill.ready .dot { background: var(--green); }
  .pill.booting .dot { background: var(--accent); }
  .pill.stopped .dot { background: var(--muted); }
  .pill.error .dot { background: var(--red); }
  .stop-btn {
    background: #3a1c20;
    color: #ffb8bf;
    border: 1px solid #6b2b33;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .stop-btn:hover:not(:disabled) { background: #52232a; }
  .stop-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .scope-banner {
    background: #141922;
    border-bottom: 1px solid var(--border);
    padding: 7px 16px;
    font-size: 11.5px;
    color: var(--muted);
    display: flex;
    flex-wrap: wrap;
    gap: 8px 18px;
    align-items: center;
  }
  .scope-banner strong { color: #cbd5e1; }
  main {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 360px;
    gap: 14px;
    padding: 14px 16px 20px;
    max-width: 1460px;
    margin: 0 auto;
  }
  @media (max-width: 980px) {
    main { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .lcd-bezel {
    background: #060709;
    padding: 12px;
    border-bottom: 1px solid var(--border);
  }
  .lcd-stage {
    position: relative;
    width: 100%;
    aspect-ratio: 4 / 3;
    background: #000;
    border: 2px solid #222833;
    border-radius: 4px;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  #lcd-img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: none;
    image-rendering: auto;
  }
  .boot-overlay {
    position: absolute;
    inset: 0;
    background: rgba(8, 10, 14, 0.94);
    padding: 20px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: flex-start;
    gap: 10px;
    overflow: auto;
  }
  .boot-overlay h2 {
    margin: 0;
    font-size: 15px;
    color: var(--accent);
  }
  .boot-overlay p {
    margin: 0;
    color: var(--muted);
    font-size: 12px;
  }
  .boot-log-pre {
    width: 100%;
    max-height: 280px;
    overflow: auto;
    background: #0e1218;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11.5px;
    color: #c8d3e3;
    white-space: pre-wrap;
    margin: 0;
  }
  .boot-strip {
    position: absolute;
    left: 10px;
    bottom: 10px;
    background: rgba(14, 16, 20, 0.88);
    border: 1px solid var(--accent-dim);
    color: var(--accent);
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    display: none;
  }
  .scrubber {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    background: var(--panel);
  }
  .scrubber-group {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  button.ctrl-btn, a.ctrl-link {
    background: var(--btn-bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.08s ease, border-color 0.08s ease;
  }
  button.ctrl-btn:hover:not(:disabled), a.ctrl-link:hover {
    background: var(--btn-hover);
    border-color: #445066;
  }
  button.ctrl-btn:active:not(:disabled) {
    background: var(--btn-active);
  }
  button.ctrl-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .side-col {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .section {
    padding: 12px;
  }
  .section-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin: 0 0 10px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .dpad-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    max-width: 240px;
    margin: 0 auto 12px;
  }
  .dpad-grid button {
    height: 44px;
    font-size: 13px;
  }
  .btn-center {
    background: #2b2415 !important;
    border-color: var(--accent-dim) !important;
    color: var(--accent) !important;
    font-size: 11px !important;
    font-weight: 700 !important;
  }
  .btn-center:hover:not(:disabled) {
    background: #3d321a !important;
  }
  .cam-btn-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 6px;
    margin-bottom: 10px;
  }
  .cam-btn-grid button {
    height: 36px;
    font-size: 11.5px;
  }
  .dial-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 6px;
  }
  .dial-grid button {
    height: 32px;
    font-size: 11px;
  }
  .shortcuts-hint {
    margin-top: 8px;
    font-size: 11px;
    color: var(--muted);
    border-top: 1px solid var(--border);
    padding-top: 8px;
  }
  .telemetry-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 6px 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11.5px;
  }
  .kv-label { color: var(--muted); font-size: 10.5px; display: block; }
  .kv-val { color: var(--text); font-weight: 600; word-break: break-all; }
  .event-list {
    max-height: 210px;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
  }
  .event-row {
    background: var(--panel2);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 4px 7px;
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }
  .ev-ok { color: var(--green); }
  .ev-pending { color: var(--accent); }
  .ev-err { color: var(--red); }
  .muted { color: var(--muted); }
  .checkbox-lbl {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-tag">LUMIX S5</span>
    <span>Interactive Native Firmware Emulator</span>
    <span id="boot-profile-pill" class="pill">boot: --</span>
  </div>
  <div class="header-right">
    <span id="status-pill" class="pill booting"><span class="dot"></span><span id="status-text">Starting...</span></span>
    <button id="stop-btn" class="stop-btn" type="button">Stop Emulator</button>
  </div>
</header>

<div class="scope-banner">
  <strong>Local HLE/TEST Scope:</strong>
  <span>Unmodified S5 v2.9 firmware + software EGL/OpenVG + verified <code>KeyProbe</code> queue (<code>0x067f0dda</code>). Raw swap PNGs only — never synthetic screens. Unmodeled: DSP/ISP/sensor/SD card.</span>
</div>

<main>
  <section class="card">
    <div class="lcd-bezel">
      <div class="lcd-stage">
        <img id="lcd-img" alt="LUMIX S5 native EGL swap frame">
        <div id="boot-overlay" class="boot-overlay">
          <h2 id="boot-overlay-title">Booting Original LUMIX S5 Firmware...</h2>
          <p id="boot-overlay-sub">Waiting for real eglSwapBuffers PNG frames from QEMU. No placeholder UI is ever rendered.</p>
          <pre id="boot-log-pre" class="boot-log-pre">Connecting to emulator control channel...</pre>
        </div>
        <div id="boot-strip" class="boot-strip"></div>
      </div>
    </div>
    <div class="scrubber">
      <div class="scrubber-group">
        <button id="btn-prev" class="ctrl-btn" type="button" disabled>Prev</button>
        <button id="btn-next" class="ctrl-btn" type="button" disabled>Next</button>
        <button id="btn-latest" class="ctrl-btn" type="button" disabled>Latest</button>
        <span id="frame-counter" class="muted">No swap frames yet</span>
      </div>
      <div class="scrubber-group">
        <label class="checkbox-lbl"><input id="auto-follow" type="checkbox" checked> Auto-Follow</label>
        <a id="download-link" class="ctrl-link" href="#" style="display:none;">Download PNG</a>
      </div>
    </div>
  </section>

  <aside class="side-col">
    <section class="card section">
      <div class="section-title">
        <span>Camera Controls</span>
        <span id="queue-badge" class="muted">Queue: 0 / 8</span>
      </div>

      <div class="dpad-grid">
        <div></div>
        <button class="ctrl-btn key-btn" data-key="KEY_UP" type="button" title="ArrowUp / W">&#8593;</button>
        <div></div>
        <button class="ctrl-btn key-btn" data-key="KEY_LEFT" type="button" title="ArrowLeft / A">&#8592;</button>
        <button class="ctrl-btn key-btn btn-center" data-key="KEY_CENTER" type="button" title="Enter / Space">MENU/SET</button>
        <button class="ctrl-btn key-btn" data-key="KEY_RIGHT" type="button" title="ArrowRight / D">&#8594;</button>
        <div></div>
        <button class="ctrl-btn key-btn" data-key="KEY_DOWN" type="button" title="ArrowDown / S">&#8595;</button>
        <div></div>
      </div>

      <div class="cam-btn-grid">
        <button class="ctrl-btn key-btn" data-key="KEY_DELETE" type="button" title="Escape / Backspace">BACK / Trash</button>
        <button class="ctrl-btn key-btn" data-key="KEY_DISPLAY" type="button" title="I">DISP.</button>
        <button class="ctrl-btn key-btn" data-key="KEY_Q_MENU" type="button" title="Q">Q.MENU</button>
        <button class="ctrl-btn key-btn" data-key="KEY_PLAY" type="button" title="P">PLAY</button>
        <button class="ctrl-btn key-btn" data-key="KEY_ISO" type="button">ISO</button>
        <button class="ctrl-btn key-btn" data-key="KEY_WB" type="button">WB</button>
        <button class="ctrl-btn key-btn" data-key="KEY_EXPOSE" type="button">+/-</button>
        <button class="ctrl-btn key-btn" data-key="KEY_SET" type="button">SET</button>
      </div>

      <div class="dial-grid">
        <button class="ctrl-btn key-btn" data-key="FRONT_DIAL_LEFT" type="button">Front Dial -</button>
        <button class="ctrl-btn key-btn" data-key="FRONT_DIAL_RIGHT" type="button">Front Dial +</button>
        <button class="ctrl-btn key-btn" data-key="REAR_DIAL_LEFT" type="button">Rear Dial -</button>
        <button class="ctrl-btn key-btn" data-key="REAR_DIAL_RIGHT" type="button">Rear Dial +</button>
        <button class="ctrl-btn key-btn" data-key="CONTROL_DIAL_LEFT" type="button">Ctrl Dial -</button>
        <button class="ctrl-btn key-btn" data-key="CONTROL_DIAL_RIGHT" type="button">Ctrl Dial +</button>
      </div>

      <div class="shortcuts-hint">
        Keys: Arrows/WASD = D-pad, Enter/Space = MENU/SET, Esc/Backspace = BACK, I = DISP., Q = Q.MENU, P = PLAY.
      </div>
    </section>

    <section class="card section">
      <div class="section-title"><span>Live Firmware Telemetry</span><span id="elapsed-txt" class="muted">0s</span></div>
      <div class="telemetry-grid">
        <div><span class="kv-label">RENDER SCENE</span><span id="kv-scene" class="kv-val">--</span></div>
        <div><span class="kv-label">RENDER MODAL</span><span id="kv-modal" class="kv-val">--</span></div>
        <div><span class="kv-label">UI STATE / MODE</span><span id="kv-state" class="kv-val">--</span></div>
        <div><span class="kv-label">SWAPS / DRAWS</span><span id="kv-swaps" class="kv-val">0 / 0</span></div>
        <div><span class="kv-label">STAGE</span><span id="kv-stage" class="kv-val">starting</span></div>
        <div><span class="kv-label">ACTIVE KEY</span><span id="kv-active-key" class="kv-val">idle</span></div>
      </div>
    </section>

    <section class="card section">
      <div class="section-title"><span>Key &amp; Event Log</span><span id="feedback-txt" class="muted"></span></div>
      <div id="event-list" class="event-list"></div>
    </section>
  </aside>
</main>

<script>
(function () {
  "use strict";
  const tokenMeta = document.querySelector('meta[name="s5-session-token"]');
  let csrfToken = tokenMeta ? tokenMeta.getAttribute("content") : "";

  const elStatusPill = document.getElementById("status-pill");
  const elStatusText = document.getElementById("status-text");
  const elBootProfile = document.getElementById("boot-profile-pill");
  const elStopBtn = document.getElementById("stop-btn");
  const elLcdImg = document.getElementById("lcd-img");
  const elBootOverlay = document.getElementById("boot-overlay");
  const elBootLogPre = document.getElementById("boot-log-pre");
  const elBootStrip = document.getElementById("boot-strip");
  const elBtnPrev = document.getElementById("btn-prev");
  const elBtnNext = document.getElementById("btn-next");
  const elBtnLatest = document.getElementById("btn-latest");
  const elFrameCounter = document.getElementById("frame-counter");
  const elAutoFollow = document.getElementById("auto-follow");
  const elDownloadLink = document.getElementById("download-link");
  const elQueueBadge = document.getElementById("queue-badge");
  const elElapsed = document.getElementById("elapsed-txt");
  const elKvScene = document.getElementById("kv-scene");
  const elKvModal = document.getElementById("kv-modal");
  const elKvState = document.getElementById("kv-state");
  const elKvSwaps = document.getElementById("kv-swaps");
  const elKvStage = document.getElementById("kv-stage");
  const elKvActiveKey = document.getElementById("kv-active-key");
  const elEventList = document.getElementById("event-list");
  const elFeedback = document.getElementById("feedback-txt");
  const keyButtons = Array.from(document.querySelectorAll(".key-btn"));

  let readyFrames = [];
  let selectedIdx = -1;
  let shownFilename = null;
  let canSendKeys = false;

  const SHORTCUT_MAP = {
    "ArrowUp": "KEY_UP",
    "KeyW": "KEY_UP",
    "ArrowDown": "KEY_DOWN",
    "KeyS": "KEY_DOWN",
    "ArrowLeft": "KEY_LEFT",
    "KeyA": "KEY_LEFT",
    "ArrowRight": "KEY_RIGHT",
    "KeyD": "KEY_RIGHT",
    "Enter": "KEY_CENTER",
    "Space": "KEY_CENTER",
    "Escape": "KEY_DELETE",
    "Backspace": "KEY_DELETE",
    "KeyI": "KEY_DISPLAY",
    "KeyQ": "KEY_Q_MENU",
    "KeyP": "KEY_PLAY"
  };

  function setControlsEnabled(enabled) {
    canSendKeys = enabled;
    for (const btn of keyButtons) {
      btn.disabled = !enabled;
    }
  }

  function showFrameAt(index) {
    if (readyFrames.length === 0) {
      elLcdImg.style.display = "none";
      elBootOverlay.style.display = "flex";
      elFrameCounter.textContent = "No swap frames yet";
      elDownloadLink.style.display = "none";
      elBtnPrev.disabled = true;
      elBtnNext.disabled = true;
      elBtnLatest.disabled = true;
      return;
    }
    if (index < 0) index = 0;
    if (index >= readyFrames.length) index = readyFrames.length - 1;
    selectedIdx = index;
    const frame = readyFrames[selectedIdx];
    elBootOverlay.style.display = "none";
    elLcdImg.style.display = "block";
    if (shownFilename !== frame.filename && frame.url) {
      shownFilename = frame.filename;
      const pre = new Image();
      pre.onload = function () {
        if (shownFilename === frame.filename) {
          elLcdImg.src = pre.src;
        }
      };
      pre.src = frame.url + "?t=" + encodeURIComponent(String(frame.png_sha256 || frame.swap_count || Date.now()));
    }
    elFrameCounter.textContent =
      "Frame " + (selectedIdx + 1) + " / " + readyFrames.length + " (" + frame.filename + ")";
    elBtnPrev.disabled = selectedIdx <= 0;
    elBtnNext.disabled = selectedIdx >= readyFrames.length - 1;
    elBtnLatest.disabled = selectedIdx >= readyFrames.length - 1;
    if (frame.download_url) {
      elDownloadLink.href = frame.download_url;
      elDownloadLink.style.display = "inline-block";
    } else {
      elDownloadLink.style.display = "none";
    }
  }

  function renderLog(control) {
    while (elEventList.firstChild) {
      elEventList.removeChild(elEventList.firstChild);
    }
    const items = (control.key_history || []).slice(-18).reverse();
    if (items.length === 0) {
      const row = document.createElement("div");
      row.className = "event-row muted";
      row.textContent = "No interactive keys sent yet.";
      elEventList.appendChild(row);
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "event-row";
      const left = document.createElement("span");
      left.textContent = String(item.key || "?");
      const right = document.createElement("span");
      const st = String(item.status || "");
      if (st === "consumed") {
        right.className = "ev-ok";
        right.textContent = "consumed" + (item.duration_ms != null ? " (" + item.duration_ms + "ms)" : "");
      } else if (st === "key_rejected" || st === "key_error" || st === "send_failed") {
        right.className = "ev-err";
        right.textContent = st + (item.error ? ": " + item.error : "");
      } else {
        right.className = "ev-pending";
        right.textContent = st;
      }
      row.appendChild(left);
      row.appendChild(right);
      elEventList.appendChild(row);
    }
  }

  function applyStatus(data) {
    const sess = data.session || {};
    const ctrl = data.control || {};
    if (sess.csrf_token && !csrfToken) {
      csrfToken = sess.csrf_token;
    }
    elBootProfile.textContent = "boot-to: " + (sess.boot_to || "menu");
    elElapsed.textContent = String(sess.elapsed_seconds != null ? sess.elapsed_seconds : 0) + "s";

    const isStopped = Boolean(ctrl.stopped || (sess.process_running === false && sess.process_exit_code !== null));
    const isReady = Boolean(ctrl.interactive_ready && !isStopped);
    const hasFatalErr = Boolean(sess.result && sess.result.graphics_error);

    elStatusPill.className = "pill " + (hasFatalErr ? "error" : isStopped ? "stopped" : isReady ? "ready" : "booting");
    if (hasFatalErr) {
      elStatusText.textContent = "Graphics Error";
    } else if (isStopped) {
      elStatusText.textContent = "Stopped (" + (ctrl.stop_reason || (sess.result && sess.result.stop_reason) || "done") + ")";
    } else if (isReady) {
      elStatusText.textContent = ctrl.active_key ? "Dispatching " + ctrl.active_key.key : "Interactive Ready";
    } else {
      elStatusText.textContent = "Booting: " + (ctrl.current_stage || "starting");
    }

    setControlsEnabled(isReady);
    elStopBtn.disabled = isStopped || Boolean(sess.stop_initiated);

    const stages = ctrl.boot_stages || [];
    if (stages.length > 0) {
      elBootLogPre.textContent = stages.map(function (s) {
        return "[" + s.stage + "]" + (s.detail ? " " + s.detail : "") + (s.pc ? " @" + s.pc : "");
      }).join("\n");
      elBootLogPre.scrollTop = elBootLogPre.scrollHeight;
    }

    readyFrames = (data.frames || []).filter(function (f) { return f.ready; });
    if (readyFrames.length > 0) {
      if (elAutoFollow.checked || selectedIdx < 0 || selectedIdx >= readyFrames.length) {
        showFrameAt(readyFrames.length - 1);
      } else {
        showFrameAt(selectedIdx);
      }
    } else {
      showFrameAt(-1);
    }

    if (!isReady && !isStopped && readyFrames.length > 0) {
      elBootStrip.style.display = "block";
      elBootStrip.textContent = "Booting (" + (ctrl.current_stage || "...") + ") - controls unlock when ready";
    } else {
      elBootStrip.style.display = "none";
    }

    const latest = data.latest_ready_frame;
    const ui = ctrl.ui_state || (latest && latest.ui_state) || {};
    elKvScene.textContent = (latest && latest.render_scene) || ui.render_scene || "--";
    elKvModal.textContent = (latest && latest.render_modal) || ui.modal_id || "--";
    elKvState.textContent = (ui.state || "--") + " / " + (ui.mode || "--");
    elKvSwaps.textContent = String(data.ready_frame_count || 0) + " / " + String((latest && latest.draw_calls) || 0);
    elKvStage.textContent = ctrl.current_stage || "--";
    elKvActiveKey.textContent = ctrl.active_key ? (ctrl.active_key.key + " (" + ctrl.active_key.status + ")") : "idle";
    elQueueBadge.textContent = "Queue: " + String(ctrl.pending_count || 0) + " / " + String(ctrl.max_pending || 8);

    renderLog(ctrl);
  }

  async function sendKey(key) {
    if (!canSendKeys) return;
    elFeedback.textContent = "Sending " + key + "...";
    try {
      const resp = await fetch("/api/key", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-S5-Session-Token": csrfToken
        },
        body: JSON.stringify({ key: key })
      });
      const body = await resp.json();
      if (resp.ok) {
        elFeedback.textContent = "Queued " + key;
      } else {
        elFeedback.textContent = "Rejected: " + (body.error || resp.status);
      }
      pollStatus();
    } catch (err) {
      elFeedback.textContent = "Request failed";
    }
  }

  async function requestStop() {
    elStopBtn.disabled = true;
    elFeedback.textContent = "Stopping emulator...";
    try {
      await fetch("/api/stop", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-S5-Session-Token": csrfToken
        },
        body: "{}"
      });
      pollStatus();
    } catch (err) {
      elFeedback.textContent = "Stop request failed";
    }
  }

  async function pollStatus() {
    try {
      const resp = await fetch("/api/status", { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      applyStatus(data);
    } catch (err) {
      elStatusPill.className = "pill error";
      elStatusText.textContent = "Disconnected";
      setControlsEnabled(false);
    }
  }

  for (const btn of keyButtons) {
    btn.addEventListener("click", function () {
      const key = btn.getAttribute("data-key");
      if (key) sendKey(key);
    });
  }
  elStopBtn.addEventListener("click", requestStop);
  elBtnPrev.addEventListener("click", function () {
    elAutoFollow.checked = false;
    showFrameAt(selectedIdx - 1);
  });
  elBtnNext.addEventListener("click", function () {
    elAutoFollow.checked = false;
    showFrameAt(selectedIdx + 1);
  });
  elBtnLatest.addEventListener("click", function () {
    elAutoFollow.checked = true;
    showFrameAt(readyFrames.length - 1);
  });

  document.addEventListener("keydown", function (ev) {
    if (!canSendKeys) return;
    const tag = ev.target && ev.target.tagName ? ev.target.tagName.toUpperCase() : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const mapped = SHORTCUT_MAP[ev.code];
    if (mapped) {
      ev.preventDefault();
      sendKey(mapped);
    }
  });

  setControlsEnabled(false);
  pollStatus();
  setInterval(pollStatus, 250);
})();
</script>
</body>
</html>
"""


class InteractiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        controller: InteractiveSessionController,
    ) -> None:
        self.controller = controller
        super().__init__(server_address, handler_cls)


class InteractiveRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def controller(self) -> InteractiveSessionController:
        return self.server.controller  # type: ignore[attr-defined]

    def _send_security_headers(self, *, is_html: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if is_html:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(is_html=False)
        self.end_headers()
        self.wfile.write(body)

    def _check_csrf(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None:
            parsed = urllib.parse.urlparse(origin)
            host, port = self.server.server_address[:2]
            allowed_hosts = {host, "127.0.0.1", "localhost", "::1"}
            if parsed.scheme != "http" or parsed.hostname not in allowed_hosts or parsed.port != port:
                return False
        supplied = self.headers.get("X-S5-Session-Token") or ""
        return bool(supplied) and hmac.compare_digest(supplied, self.controller.csrf_token)

    def do_GET(self) -> None:
        raw_target = self.path
        parsed = urllib.parse.urlsplit(raw_target)
        raw_path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if raw_path == "/":
            html_body = INDEX_HTML_TEMPLATE.replace("__CSRF_TOKEN__", self.controller.csrf_token).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_body)))
            self._send_security_headers(is_html=True)
            self.end_headers()
            self.wfile.write(html_body)
            return

        if raw_path == "/api/status":
            self._send_json(HTTPStatus.OK, self.controller.build_status_payload())
            return

        if raw_path.startswith("/api/frame/"):
            raw_suffix = raw_path[len("/api/frame/"):]
            if "%" in raw_suffix or "/" in raw_suffix or "\\" in raw_suffix or ".." in raw_suffix:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_frame_path"})
                return
            decoded = urllib.parse.unquote(raw_suffix)
            data, status, meta = self.controller.frames.read_validated_png(decoded)
            if data is None:
                self._send_json(status, {"error": meta.get("png_status", "unavailable"), **meta})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            if query.get("download", ["0"])[0] == "1":
                self.send_header("Content-Disposition", f'attachment; filename="{decoded}"')
            self._send_security_headers(is_html=False)
            self.end_headers()
            self.wfile.write(data)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        raw_path = parsed.path

        if raw_path not in ("/api/key", "/api/stop"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        if not self._check_csrf():
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "invalid_or_missing_session_token"})
            return

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_content_length"})
            return
        if content_len < 0 or content_len > MAX_POST_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "body_too_large"})
            return
        raw_body = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "json_object_required"})
            return

        if raw_path == "/api/key":
            key = body.get("key")
            if not isinstance(key, str) or key not in VALID_KEYS:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_key", "key": key})
                return
            _ok, status, resp = self.controller.control.enqueue_key(key)
            self._send_json(status, resp)
            return

        if raw_path == "/api/stop":
            resp = self.controller.request_stop(reason="user_requested")
            self._send_json(HTTPStatus.ACCEPTED, resp)
            return

    def do_PUT(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

    def do_DELETE(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

    def do_PATCH(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})


def create_interactive_server(
    controller: InteractiveSessionController,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[InteractiveHTTPServer, str]:
    validated_host = validate_loopback_host(host)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer in 0..65535")
    if ":" in validated_host:
        class _IPv6Server(InteractiveHTTPServer):
            address_family = socketserver.socket.AF_INET6
        srv = _IPv6Server((validated_host, port), InteractiveRequestHandler, controller)
        bound_port = srv.server_address[1]
        url = f"http://[{validated_host}]:{bound_port}/"
    else:
        srv = InteractiveHTTPServer((validated_host, port), InteractiveRequestHandler, controller)
        bound_port = srv.server_address[1]
        url = f"http://{validated_host}:{bound_port}/"
    return srv, url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--boot-to",
        choices=("menu", "liveview", "wizard"),
        default="menu",
        help="target screen to boot to before unlocking live interactive controls (default: menu)",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="strictly loopback bind address: 127.0.0.1, ::1, or localhost (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="HTTP listen port (0 chooses an ephemeral free loopback port)",
    )
    p.add_argument(
        "--max-seconds",
        type=int,
        default=1800,
        help="maximum guest execution seconds before clean time-bound exit (1..3600, default: 1800)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="session output directory (default: interactive/sessions/session-YYYYMMDD-HHMMSS-<hex>)",
    )
    p.add_argument(
        "--open-browser",
        action="store_true",
        help="open the local web UI in the default browser after startup",
    )
    args = p.parse_args(argv)
    if not 1 <= args.max_seconds <= 3600:
        p.error("--max-seconds must be within 1..3600")
    try:
        validate_loopback_host(args.host)
    except ValueError as exc:
        p.error(str(exc))
    if not 0 <= args.port <= 65535:
        p.error("--port must be within 0..65535")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session_dir = (
        args.output.resolve()
        if args.output is not None
        else (SESSIONS_ROOT / f"session-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}").resolve()
    )

    controller = InteractiveSessionController(
        session_dir=session_dir,
        boot_to=args.boot_to,
        max_seconds=args.max_seconds,
    )
    server, url = create_interactive_server(controller, host=args.host, port=args.port)

    shutdown_event = threading.Event()

    def _on_signal(_signum: int, _frame: Any) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    http_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="s5-interactive-http",
        daemon=True,
    )
    http_thread.start()

    try:
        controller.start_emulator()
    except Exception:
        server.shutdown()
        server.server_close()
        controller.control.close()
        raise

    print(
        f"LUMIX S5 Interactive Emulator listening at {url} "
        f"(boot-to={args.boot_to}, session={session_dir})",
        flush=True,
    )
    if args.open_browser:
        webbrowser.open(url)

    try:
        while not shutdown_event.is_set():
            time.sleep(0.2)
    finally:
        controller.shutdown_and_wait(timeout=15.0)
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
