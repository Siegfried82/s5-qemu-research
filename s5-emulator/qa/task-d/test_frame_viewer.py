#!/usr/bin/env python3
"""Automated verification suite for Task D: Read-only S5 native swap frame viewer.

Uses only the Python 3 standard library. Temporary test fixtures are created
strictly under TMPDIR (defaulting to /Volumes/SATA-1TB/Projects/s5-qemu/tmp).
Existing result directories are opened strictly read-only and verified for
byte-level invariance before and after all requests.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

EMULATOR_ROOT = Path(__file__).resolve().parents[2]
VIEWER_SCRIPT = EMULATOR_ROOT / "viewer" / "s5_frame_viewer.py"
DEFAULT_TMP_ROOT = Path("/Volumes/SATA-1TB/Projects/s5-qemu/tmp")
CORPUS_LCD_REFRESH = EMULATOR_ROOT / "menu-smp" / "results" / "local-requested-lcd-refresh"
CORPUS_TZ_CONFIRM = EMULATOR_ROOT / "qa" / "task-b" / "live-timezone-confirm-03"
OUT_VERIFICATION_JSON = Path(__file__).resolve().parent / "verification.json"

sys.path.insert(0, str(EMULATOR_ROOT / "viewer"))
import s5_frame_viewer as viewer  # noqa: E402


def make_valid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    """Create a minimal, strictly valid RGBA PNG using only stdlib zlib/struct."""
    r, g, b, a = rgba

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    row = bytes([0]) + bytes([r, g, b, a]) * width
    raw_scanlines = row * height
    idat = zlib.compress(raw_scanlines, level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def snapshot_directory(root: Path) -> dict[str, dict[str, Any]]:
    """Capture deterministic file list + byte lengths + SHA-256 hashes (read-only)."""
    snap: dict[str, dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        for fname in filenames:
            full = Path(dirpath) / fname
            rel = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            if full.is_symlink():
                snap[rel] = {"type": "symlink", "target": os.readlink(full)}
            else:
                data = full.read_bytes()
                snap[rel] = {
                    "type": "file",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
    return snap


def raw_http_request(
    host: str,
    port: int,
    method: str,
    raw_path: str,
) -> tuple[int, dict[str, str], bytes]:
    """Send an HTTP request without client-side path normalization."""
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.putrequest(method, raw_path, skip_host=False, skip_accept_encoding=False)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        conn.close()


class RunningViewer:
    """Context manager running FrameViewerHTTPServer in a background thread."""

    def __init__(self, result_dir: Path) -> None:
        self.result_dir = result_dir
        self.server = None
        self.url = ""
        self.host = ""
        self.port = 0
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "RunningViewer":
        self.server, self.url = viewer.create_server(self.result_dir, host="127.0.0.1", port=0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def get_json(self, path: str = "/api/state") -> tuple[int, dict[str, str], dict[str, Any]]:
        status, headers, body = raw_http_request(self.host, self.port, "GET", path)
        return status, headers, json.loads(body.decode("utf-8"))

    def get_raw(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        return raw_http_request(self.host, self.port, method, path)


def run_test_case(name: str, fn: Any, results: list[dict[str, Any]]) -> None:
    started = time.monotonic()
    record: dict[str, Any] = {
        "name": name,
        "executed_ok": False,
        "assertions_passed": False,
        "details": {},
        "error": None,
    }
    try:
        details = fn()
        record["executed_ok"] = True
        record["assertions_passed"] = True
        record["details"] = details or {}
    except AssertionError as exc:
        record["executed_ok"] = True
        record["assertions_passed"] = False
        record["error"] = f"AssertionError: {exc}"
    except Exception as exc:  # noqa: BLE001
        record["executed_ok"] = False
        record["assertions_passed"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    results.append(record)
    status_str = "PASS" if (record["executed_ok"] and record["assertions_passed"]) else "FAIL"
    print(f"[{status_str}] {name} ({record['duration_ms']} ms)")
    if record["error"]:
        print(f"       {record['error']}")


def main() -> int:
    tmp_base = Path(os.environ.get("TMPDIR") or str(DEFAULT_TMP_ROOT))
    tmp_base.mkdir(parents=True, exist_ok=True)
    test_root = Path(tempfile.mkdtemp(prefix="task-d-viewer-", dir=str(tmp_base)))
    case_results: list[dict[str, Any]] = []
    real_corpus_summary: dict[str, Any] = {}

    try:
        # ------------------------------------------------------------------
        # 1-5: Live incremental run simulation + chunked PNG + XSS + invariance
        # ------------------------------------------------------------------
        sim_dir = test_root / "sim-live-run"
        sim_dir.mkdir(parents=True, exist_ok=True)
        png0 = make_valid_png(16, 12, (20, 40, 200, 255))
        png1 = make_valid_png(16, 12, (220, 60, 30, 255))
        sha0 = hashlib.sha256(png0).hexdigest()
        sha1 = hashlib.sha256(png1).hexdigest()

        def case_1_start_before_files_exist() -> dict[str, Any]:
            with RunningViewer(sim_dir) as rv:
                st_html, hdr_html, body_html = rv.get_raw("/")
                assert st_html == 200, f"GET / returned {st_html}"
                html_text = body_html.decode("utf-8")
                assert "innerHTML" not in html_text, "UI JS must not use innerHTML"
                assert "outerHTML" not in html_text, "UI JS must not use outerHTML"
                assert "insertAdjacentHTML" not in html_text, "UI JS must not use insertAdjacentHTML"
                assert "document.write" not in html_text, "UI JS must not use document.write"
                assert "content-security-policy" in hdr_html, "Missing CSP header"
                assert "nosniff" in hdr_html.get("x-content-type-options", ""), "Missing nosniff"
                assert "KEY_CENTER" not in html_text and "KEY_MENU" not in html_text, "Must not have fake camera key buttons"

                st, hdr, state = rv.get_json("/api/state")
                assert st == 200, f"GET /api/state returned {st}"
                assert hdr.get("content-type", "").startswith("application/json")
                assert state["viewer"]["read_only"] is True
                assert state["viewer"]["synthetic_placeholder_frames"] is False
                assert state["viewer"]["fake_camera_controls"] is False
                assert state["native_frames"]["calls_jsonl_exists"] is False
                assert state["native_frames"]["ready_swap_count"] == 0
                assert state["native_frames"]["waiting_for_next_complete"] is True
                assert state["native_frames"]["latest_ready_swap_seq"] is None
                assert state["boot_status"]["result_present"] is False
                assert state["boot_status"]["menu_rendered"] is None
                # Ensure host filesystem path is never exposed
                state_raw = json.dumps(state)
                assert str(sim_dir) not in state_raw, "Host path leaked in /api/state"
                return {"run_state": state["boot_status"]["run_state"], "waiting_reason": state["native_frames"]["waiting_reason"]}

        def case_2_non_swap_and_truncated_tail() -> dict[str, Any]:
            gfx = sim_dir / "graphics"
            gfx.mkdir(exist_ok=True)
            calls = gfx / "calls.jsonl"
            # Non-swap events + an incomplete truncated JSONL tail line (no newline, cut mid-object)
            calls.write_bytes(
                (
                    json.dumps({"index": 0, "name": "guest_api_entry", "api": "eglGetDisplay"}) + "\n"
                    + json.dumps({"index": 1, "name": "draw_captured_path", "draw_calls": 1}) + "\n"
                    + '{"index": 2, "name": "software_swap", "path": "frame-0000'
                ).encode("utf-8")
            )
            # Also drop an unswapped incomplete-surface PNG to verify it is NOT treated as a swap frame
            (gfx / "incomplete-surface-60000002.png").write_bytes(png0)

            with RunningViewer(sim_dir) as rv:
                st, _, state = rv.get_json("/api/state")
                assert st == 200
                nf = state["native_frames"]
                assert nf["calls_jsonl_exists"] is True
                assert nf["trace_records_parsed"] == 2, f"Expected 2 parsed records, got {nf['trace_records_parsed']}"
                assert nf["truncated_tail_ignored"] is True
                assert nf["total_swap_records"] == 0
                assert nf["ready_swap_count"] == 0
                assert nf["waiting_for_next_complete"] is True
                assert "incomplete-surface-60000002.png" in nf["unswapped_incomplete_surfaces"]
                # Unswapped PNG must NOT be served
                st_inc, _, _ = rv.get_raw("/api/frames/incomplete-surface-60000002.png")
                assert st_inc in (400, 403, 404), f"Unswapped PNG should be rejected, got {st_inc}"
                return {"trace_records_parsed": nf["trace_records_parsed"], "truncated_tail_ignored": nf["truncated_tail_ignored"]}

        def case_3_swap1_chunked_png_then_complete() -> dict[str, Any]:
            gfx = sim_dir / "graphics"
            calls = gfx / "calls.jsonl"
            # Write clean non-swap lines + swap #1 referencing frame-0000.png
            calls.write_text(
                json.dumps({"index": 0, "name": "guest_api_entry", "api": "eglGetDisplay"}) + "\n"
                + json.dumps({"index": 1, "name": "draw_captured_path", "draw_calls": 1}) + "\n"
                + json.dumps({
                    "index": 10,
                    "name": "software_swap",
                    "surface": "0x60000002",
                    "path": "/moved/host/path/graphics/frame-0000.png",
                    "draw_calls": 15,
                    "pixels_sha256": "aaaa" * 16,
                    "provenance": "test",
                }) + "\n",
                encoding="utf-8",
            )
            # Chunk 1: write partial PNG (missing IEND)
            frame0_path = gfx / "frame-0000.png"
            split_at = len(png0) - 16
            frame0_path.write_bytes(png0[:split_at])

            with RunningViewer(sim_dir) as rv:
                st1, _, state1 = rv.get_json("/api/state")
                assert st1 == 200
                nf1 = state1["native_frames"]
                assert nf1["total_swap_records"] == 1
                assert nf1["ready_swap_count"] == 0, "Partial PNG must not be marked ready"
                assert nf1["latest_ready_swap_seq"] is None
                assert nf1["waiting_for_next_complete"] is True
                assert nf1["all_swap_records"][0]["png_status"] == "incomplete_png"
                st_partial, _, _ = rv.get_raw("/api/frames/frame-0000.png")
                assert st_partial == 409, f"Expected 409 for incomplete PNG, got {st_partial}"

                # Chunk 2: complete writing PNG
                frame0_path.write_bytes(png0)
                st2, _, state2 = rv.get_json("/api/state")
                assert st2 == 200
                nf2 = state2["native_frames"]
                assert nf2["ready_swap_count"] == 1
                assert nf2["latest_ready_swap_seq"] == 0
                assert nf2["latest_ready_filename"] == "frame-0000.png"
                assert nf2["waiting_for_next_complete"] is False
                assert nf2["frames"][0]["png_sha256"] == sha0
                assert nf2["frames"][0]["width"] == 16 and nf2["frames"][0]["height"] == 12
                assert nf2["frames"][0]["draw_calls"] == 15

                st_img, hdr_img, body_img = rv.get_raw("/api/frames/frame-0000.png")
                assert st_img == 200
                assert hdr_img.get("content-type") == "image/png"
                assert body_img == png0, "Served PNG bytes must be byte-identical to raw frame-0000.png"
                return {"partial_status": st_partial, "complete_sha256": sha0}

        def case_4_swap2_retains_prev_then_advances_and_downloads() -> dict[str, Any]:
            gfx = sim_dir / "graphics"
            calls = gfx / "calls.jsonl"
            with calls.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({
                        "index": 42,
                        "name": "software_swap",
                        "surface": "0x60000002",
                        "path": "/moved/host/path/graphics/frame-0001.png",
                        "draw_calls": 31,
                        "pixels_sha256": "bbbb" * 16,
                        "provenance": "test",
                    }) + "\n"
                )
            # Write partial frame-0001.png first -> must retain frame-0000.png as latest ready!
            frame1_path = gfx / "frame-0001.png"
            frame1_path.write_bytes(png1[:20])

            with RunningViewer(sim_dir) as rv:
                st_mid, _, state_mid = rv.get_json("/api/state")
                assert st_mid == 200
                nf_mid = state_mid["native_frames"]
                assert nf_mid["total_swap_records"] == 2
                assert nf_mid["ready_swap_count"] == 1
                assert nf_mid["latest_ready_swap_seq"] == 0, "Must retain previous valid swap #0 while #1 is incomplete"
                assert nf_mid["waiting_for_next_complete"] is True
                assert "frame-0001.png" in (nf_mid["waiting_reason"] or "")

                # Finish writing frame-0001.png
                frame1_path.write_bytes(png1)
                st_done, _, state_done = rv.get_json("/api/state")
                assert st_done == 200
                nf_done = state_done["native_frames"]
                assert nf_done["ready_swap_count"] == 2
                assert nf_done["latest_ready_swap_seq"] == 1
                assert [f["swap_seq"] for f in nf_done["frames"]] == [0, 1]
                assert [f["filename"] for f in nf_done["frames"]] == ["frame-0000.png", "frame-0001.png"]
                assert [f["draw_calls"] for f in nf_done["frames"]] == [15, 31]

                # Verify download endpoint
                st_dl, hdr_dl, body_dl = rv.get_raw("/api/frames/frame-0001.png/download")
                assert st_dl == 200
                assert hdr_dl.get("content-type") == "image/png"
                assert 'attachment; filename="frame-0001.png"' in hdr_dl.get("content-disposition", "")
                assert body_dl == png1, "Downloaded PNG must be byte-identical"
                return {"order": [f["swap_seq"] for f in nf_done["frames"]], "download_sha256": sha1}

        def case_5_result_json_completion_and_xss_safety() -> dict[str, Any]:
            xss = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
            result_payload = {
                "menu_rendered": False,
                "stop_reason": f"time_bound {xss}",
                "qemu_exit_code": 0,
                "graphics_draws": 31,
                "graphics_swaps": 2,
                "graphics_model": f"Opt-in software EGL {xss}",
                "storage_model": "Read-only HLE",
                "display_isolation": {"scope": f"Local experiment {xss}", "requested_ticks": 600000},
                "original_ui_state_at_stop": {
                    "modal_id_08a92898": f"0x4a {xss}",
                    "render_scene_0b392e98": "0x70de",
                },
                "original_firmware_instructions_modified": False,
                "peer_flags_manually_cleared": False,
            }
            (sim_dir / "result.json").write_text(json.dumps(result_payload, indent=2), encoding="utf-8")

            with RunningViewer(sim_dir) as rv:
                st_html, _, body_html = rv.get_raw("/")
                assert st_html == 200
                assert xss.encode("utf-8") not in body_html, "Untrusted result.json string must NEVER be interpolated into HTML"

                st_api, hdr_api, state = rv.get_json("/api/state")
                assert st_api == 200
                assert hdr_api.get("content-type", "").startswith("application/json")
                assert hdr_api.get("x-content-type-options") == "nosniff"
                bs = state["boot_status"]
                assert bs["result_present"] is True
                assert bs["menu_rendered"] is False, "menu_rendered=false MUST be preserved verbatim"
                assert bs["stop_reason"] == f"time_bound {xss}"
                assert bs["qemu_exit_code"] == 0
                assert bs["original_ui_state_at_stop"]["render_scene_0b392e98"] == "0x70de"
                assert state["experiment_scope"]["available"] is True
                assert "graphics_model" in state["experiment_scope"]["items"]
                return {"menu_rendered": bs["menu_rendered"], "xss_in_html": False}

        def case_6_failure_exit_and_missing_png() -> dict[str, Any]:
            fail_dir = test_root / "sim-failure-run"
            (fail_dir / "graphics").mkdir(parents=True, exist_ok=True)
            (fail_dir / "graphics" / "calls.jsonl").write_text(
                json.dumps({
                    "index": 5,
                    "name": "software_swap",
                    "surface": "0x60000002",
                    "path": "graphics/frame-0000.png",
                    "draw_calls": 9,
                }) + "\n",
                encoding="utf-8",
            )
            # Intentionally do NOT write frame-0000.png -> missing file!
            (fail_dir / "result.json").write_text(
                json.dumps({
                    "menu_rendered": False,
                    "stop_reason": "graphics_unsupported_or_invalid",
                    "qemu_exit_code": 1,
                    "graphics_error": {
                        "pc": "0x64d0000",
                        "cpu": 2,
                        "error": "unsupported vgTransformPath <script>bad</script>",
                        "exception": "NotImplementedError",
                    },
                }),
                encoding="utf-8",
            )
            with RunningViewer(fail_dir) as rv:
                st, _, state = rv.get_json("/api/state")
                assert st == 200
                assert state["boot_status"]["run_state"] == "failed"
                assert state["boot_status"]["menu_rendered"] is False
                assert state["boot_status"]["qemu_exit_code"] == 1
                assert state["boot_status"]["graphics_error"]["exception"] == "NotImplementedError"
                assert state["native_frames"]["ready_swap_count"] == 0
                assert state["native_frames"]["all_swap_records"][0]["png_status"] == "missing_file"
                st_missing, _, _ = rv.get_raw("/api/frames/frame-0000.png")
                assert st_missing == 404, f"Expected 404 for missing PNG, got {st_missing}"
                return {"run_state": state["boot_status"]["run_state"], "missing_png_http": st_missing}

        def case_7_security_path_traversal_symlinks_methods_and_loopback() -> dict[str, Any]:
            sec_dir = test_root / "sim-security"
            gfx = sec_dir / "graphics"
            gfx.mkdir(parents=True, exist_ok=True)
            outside_png = test_root / "outside-secret.png"
            outside_png.write_bytes(png0)
            (gfx / "frame-0000.png").write_bytes(png0)
            symlink_escape = gfx / "frame-0099.png"
            os.symlink(str(outside_png), str(symlink_escape))

            (gfx / "calls.jsonl").write_text(
                json.dumps({"index": 1, "name": "software_swap", "path": "graphics/frame-0000.png", "draw_calls": 1}) + "\n"
                + json.dumps({"index": 2, "name": "software_swap", "path": "graphics/frame-0099.png", "draw_calls": 2}) + "\n"
                + json.dumps({"index": 3, "name": "software_swap", "path": "../../outside-secret.png", "draw_calls": 3}) + "\n",
                encoding="utf-8",
            )

            # Check non-loopback host rejection
            rejected_hosts: list[str] = []
            for bad_host in ("0.0.0.0", "192.168.1.10", "::", "example.com"):
                try:
                    viewer.validate_loopback_host(bad_host)
                except ValueError:
                    rejected_hosts.append(bad_host)
            assert len(rejected_hosts) == 4, f"Expected all 4 non-loopback hosts rejected, got {rejected_hosts}"

            attacks = [
                "/api/frames/../../outside-secret.png",
                "/api/frames/../result.json",
                "/api/frames/%2e%2e/result.json",
                "/api/frames/%2e%2e%2fresult.json",
                "/api/frames/..%2fresult.json",
                "/api/frames//etc/passwd",
                "/api/frames/%2fetc%2fpasswd",
                "/api/frames/..\\result.json",
                "/../result.json",
                "/api/frames/frame-0099.png",
                "/api/frames/frame-0099.png/download",
            ]
            attack_statuses: dict[str, int] = {}
            with RunningViewer(sec_dir) as rv:
                for path in attacks:
                    st, _, _ = rv.get_raw(path)
                    attack_statuses[path] = st
                    assert st in (400, 403, 404), f"Attack {path} returned unexpected {st}"
                # Specifically verify symlink escape returns 403
                assert attack_statuses["/api/frames/frame-0099.png"] == 403
                assert attack_statuses["/api/frames/frame-0099.png/download"] == 403

                # Verify read-only HTTP write method rejection
                for method in ("POST", "PUT", "DELETE", "PATCH"):
                    st_m, _, _ = rv.get_raw("/api/state", method=method)
                    assert st_m == 405, f"Method {method} returned {st_m}, expected 405"

                # Verify state marks symlink escape and .. path as not ready
                st_s, _, state = rv.get_json("/api/state")
                assert st_s == 200
                swaps = state["native_frames"]["all_swap_records"]
                assert swaps[0]["ready"] is True
                assert swaps[1]["ready"] is False and swaps[1]["png_status"] == "symlink_escape"
                assert swaps[2]["ready"] is False and swaps[2]["png_status"] == "invalid_path"

            return {"rejected_hosts": rejected_hosts, "attack_statuses": attack_statuses}

        def case_8_readonly_byte_invariance_on_sim_dir() -> dict[str, Any]:
            before = snapshot_directory(sim_dir)
            with RunningViewer(sim_dir) as rv:
                rv.get_raw("/")
                rv.get_json("/api/state")
                rv.get_raw("/api/frames/frame-0000.png")
                rv.get_raw("/api/frames/frame-0001.png/download")
                rv.get_raw("/api/frames/../result.json")
                rv.get_raw("/api/state", method="POST")
            after = snapshot_directory(sim_dir)
            assert before == after, "Input directory bytes or file list changed during viewer interaction!"
            return {"files_checked": len(before), "byte_identical": True}

        def case_9_real_corpus_lcd_refresh_and_cli_launch() -> dict[str, Any]:
            assert CORPUS_LCD_REFRESH.is_dir(), f"Missing real corpus: {CORPUS_LCD_REFRESH}"
            before = snapshot_directory(CORPUS_LCD_REFRESH)

            # Launch via actual CLI subprocess with --port 0 to verify CLI contract
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(VIEWER_SCRIPT),
                    "--result-dir",
                    str(CORPUS_LCD_REFRESH),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline().strip()
                m = re.search(r"(http://127\.0\.0\.1:(\d+)/)", line)
                assert m is not None, f"Unexpected CLI startup line: {line!r}"
                url = m.group(1)
                port = int(m.group(2))
                assert port > 0

                st_h, _, body_h = raw_http_request("127.0.0.1", port, "GET", "/")
                assert st_h == 200 and b"LUMIX S5 Native Swap Frame Viewer" in body_h

                st_s, _, body_s = raw_http_request("127.0.0.1", port, "GET", "/api/state")
                assert st_s == 200
                state = json.loads(body_s.decode("utf-8"))
                # Verify host path is not exposed
                assert str(CORPUS_LCD_REFRESH) not in body_s.decode("utf-8")

                nf = state["native_frames"]
                bs = state["boot_status"]
                es = state["experiment_scope"]

                assert nf["total_swap_records"] == 5, f"Expected 5 swaps, got {nf['total_swap_records']}"
                assert nf["ready_swap_count"] == 5
                assert [f["swap_seq"] for f in nf["frames"]] == [0, 1, 2, 3, 4]
                assert [f["filename"] for f in nf["frames"]] == [f"frame-{i:04d}.png" for i in range(5)]
                assert [f["draw_calls"] for f in nf["frames"]] == [15, 31, 84, 98, 110]
                assert [f["trace_index"] for f in nf["frames"]] == [696, 1289, 3443, 4476, 4881]
                assert bs["menu_rendered"] is False
                assert bs["stop_reason"] == "time_bound"
                assert bs["qemu_exit_code"] == 0
                assert bs["original_ui_state_at_stop"]["modal_id_08a92898"] == "0x4a"
                assert bs["original_ui_state_at_stop"]["render_scene_0b392e98"] == "0x70de"
                assert es["available"] is True
                assert "display_isolation" in es["items"]
                assert "lcd_refresh_test_probe" in es["items"]

                frame_checks = []
                for frame in nf["frames"]:
                    fname = frame["filename"]
                    disk_bytes = (CORPUS_LCD_REFRESH / "graphics" / fname).read_bytes()
                    st_f, hdr_f, served = raw_http_request("127.0.0.1", port, "GET", f"/api/frames/{fname}")
                    assert st_f == 200 and hdr_f.get("content-type") == "image/png"
                    assert served == disk_bytes, f"{fname} served bytes differ from disk"
                    st_d, hdr_d, dl = raw_http_request("127.0.0.1", port, "GET", f"/api/frames/{fname}/download")
                    assert st_d == 200 and "attachment" in hdr_d.get("content-disposition", "")
                    assert dl == disk_bytes, f"{fname} download bytes differ from disk"
                    frame_checks.append({
                        "swap_seq": frame["swap_seq"],
                        "filename": fname,
                        "trace_index": frame["trace_index"],
                        "draw_calls": frame["draw_calls"],
                        "width": frame["width"],
                        "height": frame["height"],
                        "png_bytes": frame["png_bytes"],
                        "png_sha256": frame["png_sha256"],
                        "byte_identical_to_disk": True,
                    })
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

            after = snapshot_directory(CORPUS_LCD_REFRESH)
            assert before == after, "Real corpus local-requested-lcd-refresh was mutated!"
            real_corpus_summary["local_requested_lcd_refresh"] = {
                "cli_bound_loopback_url_example": url,
                "swap_count": 5,
                "menu_rendered": False,
                "stop_reason": "time_bound",
                "qemu_exit_code": 0,
                "modal_id_at_stop": "0x4a",
                "render_scene_at_stop": "0x70de",
                "source_dir_files_checked": len(before),
                "source_dir_byte_identical_after_run": True,
                "frames": frame_checks,
            }
            return {
                "cli_url": url,
                "frames_verified": len(frame_checks),
                "source_dir_untouched": True,
            }

        def case_10_real_corpus_live_timezone_confirm_03() -> dict[str, Any]:
            assert CORPUS_TZ_CONFIRM.is_dir(), f"Missing real corpus: {CORPUS_TZ_CONFIRM}"
            before = snapshot_directory(CORPUS_TZ_CONFIRM)
            with RunningViewer(CORPUS_TZ_CONFIRM) as rv:
                st, _, state = rv.get_json("/api/state")
                assert st == 200
                nf = state["native_frames"]
                bs = state["boot_status"]
                es = state["experiment_scope"]

                assert nf["total_swap_records"] == 13, f"Expected 13 swaps, got {nf['total_swap_records']}"
                assert nf["ready_swap_count"] == 13
                assert [f["swap_seq"] for f in nf["frames"]] == list(range(13))
                assert [f["filename"] for f in nf["frames"]] == [f"frame-{i:04d}.png" for i in range(13)]
                assert "incomplete-surface-60000002.png" in nf["unswapped_incomplete_surfaces"]
                assert bs["menu_rendered"] is False
                assert "lens_release_input" in es["items"]

                # Ensure unswapped incomplete surface is rejected even on real corpus
                st_inc, _, _ = rv.get_raw("/api/frames/incomplete-surface-60000002.png")
                assert st_inc in (400, 403, 404)

                # Verify all 13 frames byte-identical
                for frame in nf["frames"]:
                    fname = frame["filename"]
                    disk_bytes = (CORPUS_TZ_CONFIRM / "graphics" / fname).read_bytes()
                    st_f, _, served = rv.get_raw(f"/api/frames/{fname}")
                    assert st_f == 200 and served == disk_bytes

            after = snapshot_directory(CORPUS_TZ_CONFIRM)
            assert before == after, "Real corpus live-timezone-confirm-03 was mutated!"
            real_corpus_summary["live_timezone_confirm_03"] = {
                "swap_count": 13,
                "menu_rendered": False,
                "stop_reason": bs["stop_reason"],
                "qemu_exit_code": bs["qemu_exit_code"],
                "unswapped_incomplete_surface_rejected_http": st_inc,
                "lens_release_input_present": True,
                "source_dir_files_checked": len(before),
                "source_dir_byte_identical_after_run": True,
            }
            return {"frames_verified": 13, "unswapped_rejected_http": st_inc, "source_dir_untouched": True}

        run_test_case("1_start_before_result_and_calls_exist", case_1_start_before_files_exist, case_results)
        run_test_case("2_non_swap_and_truncated_jsonl_tail", case_2_non_swap_and_truncated_tail, case_results)
        run_test_case("3_swap1_chunked_png_then_complete", case_3_swap1_chunked_png_then_complete, case_results)
        run_test_case("4_swap2_retains_prev_then_advances_and_downloads", case_4_swap2_retains_prev_then_advances_and_downloads, case_results)
        run_test_case("5_result_json_completion_and_xss_safety", case_5_result_json_completion_and_xss_safety, case_results)
        run_test_case("6_failure_exit_and_missing_png", case_6_failure_exit_and_missing_png, case_results)
        run_test_case("7_security_traversal_symlinks_methods_loopback", case_7_security_path_traversal_symlinks_methods_and_loopback, case_results)
        run_test_case("8_readonly_byte_invariance_on_sim_dir", case_8_readonly_byte_invariance_on_sim_dir, case_results)
        run_test_case("9_real_corpus_local_requested_lcd_refresh_and_cli", case_9_real_corpus_lcd_refresh_and_cli_launch, case_results)
        run_test_case("10_real_corpus_live_timezone_confirm_03", case_10_real_corpus_live_timezone_confirm_03, case_results)

    finally:
        shutil.rmtree(test_root, ignore_errors=True)

    tests_executed_ok = all(r["executed_ok"] for r in case_results)
    assertions_passed = all(r["assertions_passed"] for r in case_results)
    passed_count = sum(1 for r in case_results if r["executed_ok"] and r["assertions_passed"])

    verification = {
        "task": "task-d-native-frame-viewer",
        "viewer_script": "viewer/s5_frame_viewer.py",
        "test_script": "qa/task-d/test_frame_viewer.py",
        "python_version": sys.version.split()[0],
        "stdlib_only": True,
        "tests_executed_ok": tests_executed_ok,
        "assertions_passed": assertions_passed,
        "total_cases": len(case_results),
        "passed_cases": passed_count,
        "launch_command": (
            "python3 -B viewer/s5_frame_viewer.py "
            "--result-dir menu-smp/results/local-requested-lcd-refresh"
        ),
        "real_corpus_verification": real_corpus_summary,
        "cases": case_results,
        "verified_vs_assumptions": {
            "verified": [
                "Pure Python standard library HTTP server binds loopback 127.0.0.1 on random free port (--port 0) and rejects non-loopback hosts.",
                "Starts cleanly before result.json or graphics/calls.jsonl exists; ignores non-swap events and incomplete trailing JSONL lines.",
                "Validates PNG signature, IHDR, IDAT, chunk CRCs, and IEND in memory; retains previous valid frame while next PNG is incomplete; never synthesizes placeholder frames.",
                "Serves exact byte-identical raw PNGs and download attachments strictly for authorized software_swap records; rejects unswapped incomplete-surface PNGs.",
                "Preserves menu_rendered verbatim (false) and separates native swap frames, result.json boot status, and local HLE/TEST experiment scope.",
                "Static HTML shell + textContent-only DOM rendering + strict CSP + nosniff prevents HTML/XSS injection from result.json or calls.jsonl strings.",
                "Rejects path traversal, percent-encoded traversal, symlink escapes (403), and HTTP write methods (405); verified byte-level read-only invariance on synthetic and both real corpora (5 frames + 13 frames).",
            ],
            "not_claimed_or_remaining_assumptions": [
                "Headless browser DOM rendering (CSS/JS execution inside an actual browser engine) was not driven by Playwright/Chrome in this stdlib-only test; JS safety was verified by static inspection (zero innerHTML/outerHTML/insertAdjacentHTML/document.write), CSP headers, and API JSON payload verification.",
                "Viewer does not run QEMU, does not provide camera input controls, and does not claim main menu or full camera boot succeeded (menu_rendered remains false on both real corpora).",
            ],
        },
    }

    OUT_VERIFICATION_JSON.write_text(json.dumps(verification, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSummary: {passed_count}/{len(case_results)} cases passed | tests_executed_ok={tests_executed_ok} | assertions_passed={assertions_passed}")
    print(f"Wrote {OUT_VERIFICATION_JSON}")
    return 0 if (tests_executed_ok and assertions_passed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
