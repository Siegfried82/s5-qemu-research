#!/usr/bin/env python3
"""Live end-to-end QEMU interactive verification for interactive/s5_interactive.py.

Launches the real interactive launcher with --boot-to menu, waits until
interactive_ready == True on Main Menu (scene 0x1002), sends 4 real live
interactive HTTP POST /api/key clicks (KEY_RIGHT -> KEY_CENTER -> KEY_DELETE ->
KEY_DOWN), validates every served PNG byte-for-byte against disk, stops cleanly
via POST /api/stop, and verifies result.json integrity.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EMULATOR_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_SCRIPT = EMULATOR_ROOT / "interactive" / "s5_interactive.py"
VIEWER_DIR = EMULATOR_ROOT / "viewer"
DEFAULT_TMPDIR = Path("/Volumes/SATA-1TB/Projects/s5-qemu/tmp")
OUT_DIR = Path(__file__).resolve().parent / "live-session-01"
SUMMARY_JSON = Path(__file__).resolve().parent / "live-test-summary.json"

if str(VIEWER_DIR) not in sys.path:
    sys.path.insert(0, str(VIEWER_DIR))
from s5_frame_viewer import validate_png_bytes  # noqa: E402


def http_json(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        headers: dict[str, str] = {}
        body: bytes | None = None
        if token is not None:
            headers["X-S5-Session-Token"] = token
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def http_raw(host: str, port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        data = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, data
    finally:
        conn.close()


def fetch_and_verify_frame(host: str, port: int, session_dir: Path, filename: str) -> dict[str, Any]:
    st, hdrs, body = http_raw(host, port, f"/api/frame/{filename}")
    assert st == 200, f"GET /api/frame/{filename} returned HTTP {st}"
    assert hdrs.get("content-type") == "image/png", f"Unexpected content-type: {hdrs.get('content-type')}"
    png_check = validate_png_bytes(body)
    assert png_check["valid"], f"Invalid PNG for {filename}: {png_check}"
    disk_path = session_dir / "graphics" / filename
    assert disk_path.is_file(), f"Missing frame on disk: {disk_path}"
    disk_bytes = disk_path.read_bytes()
    assert body == disk_bytes, f"HTTP bytes differ from disk bytes for {filename}"
    return {
        "filename": filename,
        "http_status": st,
        "width": png_check["width"],
        "height": png_check["height"],
        "png_bytes": len(body),
        "png_sha256": hashlib.sha256(body).hexdigest(),
        "byte_identical_to_disk": True,
    }


def main() -> int:
    if OUT_DIR.exists():
        raise SystemExit(f"Output directory already exists (remove or use clean path): {OUT_DIR}")

    tmp_dir = Path(os.environ.get("TMPDIR") or str(DEFAULT_TMPDIR))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_dir)
    env["PYTHONUNBUFFERED"] = "1"

    cmd = [
        sys.executable,
        "-B",
        str(LAUNCHER_SCRIPT),
        "--boot-to",
        "menu",
        "--port",
        "0",
        "--max-seconds",
        "180",
        "--output",
        str(OUT_DIR),
    ]
    print(f"[live-test] Launching: {' '.join(cmd)}", flush=True)
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(EMULATOR_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    summary: dict[str, Any] = {
        "executed_ok": False,
        "assertions_passed": False,
        "output_dir": str(OUT_DIR),
        "boot_to": "menu",
        "initial_ready_frame": None,
        "interactive_keys": [],
        "shutdown": {},
        "error": None,
    }

    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        m = re.search(r"http://(127\.0\.0\.1):(\d+)/", line)
        assert m is not None, f"Failed to parse bound URL from launcher stdout: {line!r}"
        host, port = m.group(1), int(m.group(2))
        print(f"[live-test] Bound at http://{host}:{port}/", flush=True)
        summary["bound_url"] = f"http://{host}:{port}/"

        # 1. Wait for interactive_ready == True on Main Menu (scene 0x1002)
        csrf_token = ""
        ready_status: dict[str, Any] | None = None
        ready_deadline = time.monotonic() + 140.0
        last_stage = None
        while time.monotonic() < ready_deadline:
            st, data = http_json(host, port, "GET", "/api/status")
            assert st == 200, f"GET /api/status returned {st}"
            csrf_token = data["session"]["csrf_token"]
            ctrl = data["control"]
            stage = ctrl.get("current_stage")
            if stage != last_stage:
                print(
                    f"[live-test] +{time.monotonic()-started:.1f}s stage={stage} swaps={data['ready_frame_count']}",
                    flush=True,
                )
                last_stage = stage
            if ctrl.get("stopped"):
                raise AssertionError(
                    f"Emulator stopped before interactive_ready: stop_reason={ctrl.get('stop_reason')}, "
                    f"errors={ctrl.get('errors')}, stderr={data['session'].get('stderr_tail')}"
                )
            if ctrl.get("interactive_ready") and data.get("latest_ready_frame"):
                ready_status = data
                break
            time.sleep(0.35)

        assert ready_status is not None, "Timed out waiting for interactive_ready == True"
        ready_frame = ready_status["latest_ready_frame"]
        print(
            f"[live-test] READY in {time.monotonic()-started:.1f}s: "
            f"frame={ready_frame['filename']} scene={ready_frame['render_scene']} modal={ready_frame['render_modal']}",
            flush=True,
        )
        assert ready_frame["render_scene"] == "0x1002", f"Expected Main Menu scene 0x1002, got {ready_frame}"
        assert ready_frame["render_modal"] == "0x0", f"Expected modal 0x0, got {ready_frame}"
        init_png_info = fetch_and_verify_frame(host, port, OUT_DIR, ready_frame["filename"])
        summary["boot_to_ready_seconds"] = round(time.monotonic() - started, 2)
        summary["initial_ready_frame"] = {
            **init_png_info,
            "swap_count": ready_frame["swap_count"],
            "draw_calls": ready_frame["draw_calls"],
            "render_scene": ready_frame["render_scene"],
            "render_modal": ready_frame["render_modal"],
            "ui_state": ready_frame.get("ui_state"),
        }

        # 2. Send 4 live interactive clicks sequentially
        sequence = [
            ("KEY_RIGHT", "Enter right-hand Photo / Image Quality 1 list items"),
            ("KEY_CENTER", "Enter Photo Style submenu"),
            ("KEY_DELETE", "BACK / Return out of Photo Style submenu"),
            ("KEY_DOWN", "Navigate downward to next item"),
        ]
        prev_swap_count = int(ready_frame["swap_count"])
        prev_sha = init_png_info["png_sha256"]

        for idx, (key_name, desc) in enumerate(sequence, start=1):
            t_send = time.monotonic()
            st_post, post_resp = http_json(
                host,
                port,
                "POST",
                "/api/key",
                token=csrf_token,
                payload={"key": key_name},
            )
            assert st_post == 202 and post_resp.get("ok"), f"POST /api/key {key_name} failed: {st_post} {post_resp}"
            req_id = post_resp["id"]
            print(f"[live-test] Step {idx}: sent {key_name} (id={req_id}) — {desc}", flush=True)

            # Wait until consumed AND a new validated swap frame arrives
            step_deadline = time.monotonic() + 30.0
            consumed_entry = None
            new_frame = None
            while time.monotonic() < step_deadline:
                st_poll, poll_data = http_json(host, port, "GET", "/api/status")
                assert st_poll == 200
                ctrl = poll_data["control"]
                if ctrl.get("stopped"):
                    raise AssertionError(f"Emulator stopped during {key_name}: {ctrl.get('stop_reason')}")
                for entry in ctrl.get("key_history", []):
                    if entry.get("id") == req_id:
                        if entry.get("status") in ("key_rejected", "key_error", "send_failed"):
                            raise AssertionError(f"Key {key_name} failed: {entry}")
                        if entry.get("status") == "consumed":
                            consumed_entry = entry
                latest = poll_data.get("latest_ready_frame")
                if consumed_entry is not None and latest and int(latest["swap_count"]) > prev_swap_count:
                    # Wait briefly (0.6s) in case the UI commits a second redraw swap for the transition
                    time.sleep(0.6)
                    _, settled = http_json(host, port, "GET", "/api/status")
                    new_frame = settled.get("latest_ready_frame") or latest
                    break
                time.sleep(0.2)

            assert consumed_entry is not None, f"Timed out waiting for {key_name} to be consumed"
            assert new_frame is not None, f"Timed out waiting for post-{key_name} EGL swap (prev={prev_swap_count})"

            png_info = fetch_and_verify_frame(host, port, OUT_DIR, new_frame["filename"])
            changed_pixels = png_info["png_sha256"] != prev_sha
            assert changed_pixels, f"Frame pixels did not change after {key_name} ({new_frame['filename']})"
            step_record = {
                "step": idx,
                "key": key_name,
                "description": desc,
                "request_id": req_id,
                "consume_status": consumed_entry["status"],
                "key_duration_ms": consumed_entry.get("duration_ms"),
                "wall_to_swap_seconds": round(time.monotonic() - t_send, 2),
                "swap_count": new_frame["swap_count"],
                "draw_calls": new_frame["draw_calls"],
                "render_scene": new_frame["render_scene"],
                "render_modal": new_frame["render_modal"],
                "source_scene": new_frame.get("source_scene"),
                "source_modal": new_frame.get("source_modal"),
                "ui_state": new_frame.get("ui_state"),
                "pixels_changed_from_prev": changed_pixels,
                **png_info,
            }
            summary["interactive_keys"].append(step_record)
            print(
                f"[live-test] Step {idx} OK: {key_name} consumed ({step_record['key_duration_ms']}ms) -> "
                f"{png_info['filename']} scene={new_frame['render_scene']} modal={new_frame['render_modal']} "
                f"sha256={png_info['png_sha256'][:12]}",
                flush=True,
            )
            prev_swap_count = int(new_frame["swap_count"])
            prev_sha = png_info["png_sha256"]

        # 3. Clean shutdown via POST /api/stop
        st_stop, stop_resp = http_json(host, port, "POST", "/api/stop", token=csrf_token, payload={})
        assert st_stop == 202 and stop_resp.get("ok"), f"POST /api/stop failed: {st_stop} {stop_resp}"
        print("[live-test] Sent POST /api/stop; waiting for clean boot.py exit & result.json...", flush=True)

        stop_deadline = time.monotonic() + 25.0
        final_status = None
        while time.monotonic() < stop_deadline:
            _, status_data = http_json(host, port, "GET", "/api/status")
            sess = status_data["session"]
            ctrl = status_data["control"]
            if ctrl.get("stopped") and not sess.get("process_running") and sess.get("result", {}).get("present"):
                final_status = status_data
                break
            time.sleep(0.25)

        assert final_status is not None, "Timed out waiting for clean stop & result.json"
        assert final_status["session"]["process_exit_code"] == 0, f"boot.py exit code: {final_status['session']['process_exit_code']}"

        # Terminate launcher cleanly with SIGINT
        proc.send_signal(signal.SIGINT)
        launcher_rc = proc.wait(timeout=15)
        assert launcher_rc == 0, f"Launcher exited with {launcher_rc}"

        result_path = OUT_DIR / "result.json"
        assert result_path.is_file(), "result.json missing"
        result_json = json.loads(result_path.read_text(encoding="utf-8"))
        assert result_json.get("qemu_exit_code") == 0, f"qemu_exit_code={result_json.get('qemu_exit_code')}"
        assert result_json.get("graphics_error") is None, f"graphics_error={result_json.get('graphics_error')}"
        assert result_json.get("original_firmware_instructions_modified") is False
        assert result_json.get("peer_flags_manually_cleared") is False
        assert result_json.get("interactive_session", {}).get("clicks_count") == 4

        summary["shutdown"] = {
            "stop_reason": result_json.get("stop_reason"),
            "boot_exit_code": final_status["session"]["process_exit_code"],
            "launcher_exit_code": launcher_rc,
            "qemu_exit_code": result_json.get("qemu_exit_code"),
            "graphics_error": result_json.get("graphics_error"),
            "total_graphics_swaps": result_json.get("graphics_swaps"),
            "total_graphics_draws": result_json.get("graphics_draws"),
            "interactive_clicks_recorded": result_json.get("interactive_session", {}).get("clicks_count"),
            "lcd_refresh_total_pulses": result_json.get("lcd_refresh_test_probe", {}).get("total_pulses"),
            "result_json_present": True,
        }
        summary["total_wall_seconds"] = round(time.monotonic() - started, 2)
        summary["executed_ok"] = True
        summary["assertions_passed"] = True
        print(f"[live-test] ALL LIVE INTERACTIVE ASSERTIONS PASSED in {summary['total_wall_seconds']}s!", flush=True)
        return 0
    except AssertionError as exc:
        summary["executed_ok"] = True
        summary["assertions_passed"] = False
        summary["error"] = f"AssertionError: {exc}"
        print(f"[live-test] ASSERTION FAILED: {exc}", flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        summary["executed_ok"] = False
        summary["assertions_passed"] = False
        summary["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[live-test] ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        SUMMARY_JSON.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
