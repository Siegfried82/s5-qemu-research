#!/usr/bin/env python3
"""Host unit tests for interactive control protocol, extended LCD pulses, and HTTP server security."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

EMULATOR_ROOT = Path(__file__).resolve().parents[2]
MENU_SMP_DIR = EMULATOR_ROOT / "menu-smp"
INTERACTIVE_DIR = EMULATOR_ROOT / "interactive"
DEFAULT_TMPDIR = Path("/Volumes/SATA-1TB/Projects/s5-qemu/tmp")

for _p in (str(MENU_SMP_DIR), str(INTERACTIVE_DIR), str(EMULATOR_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import interactive_control as ic  # noqa: E402
import lcd_refresh_probe as lrp  # noqa: E402
import s5_interactive as s5i  # noqa: E402


def make_valid_png(width: int = 8, height: int = 6, rgba: tuple[int, int, int, int] = (30, 80, 190, 255)) -> bytes:
    r, g, b, a = rgba

    def chunk(ctype: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(ctype)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = (bytes([0]) + bytes([r, g, b, a]) * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def raw_http(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=False, skip_accept_encoding=False)
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        if body is not None:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, data
    finally:
        conn.close()


def wait_until(predicate, timeout: float = 3.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


class InteractiveControlProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ic.InteractiveControlServer()

    def tearDown(self) -> None:
        self.server.close()

    def test_auth_rejects_invalid_token_and_accepts_valid_token(self) -> None:
        with self.assertRaises(ic.InteractiveControlError):
            ic.InteractiveControlClient(self.server.port, "wrong-token")
        self.assertTrue(wait_until(lambda: self.server.snapshot()["rejected_auth_attempts"] >= 1))

        client = ic.InteractiveControlClient(self.server.port, self.server.token)
        try:
            self.assertTrue(client.connected)
            self.assertTrue(wait_until(lambda: self.server.snapshot()["authenticated"]))
            # Second client must be rejected while first client is connected
            with self.assertRaises(ic.InteractiveControlError):
                ic.InteractiveControlClient(self.server.port, self.server.token)
        finally:
            client.close()

    def test_events_queue_overflow_dispatch_consume_and_stop(self) -> None:
        client = ic.InteractiveControlClient(self.server.port, self.server.token)
        try:
            client.send_event("hello", seconds=1800)
            client.send_event("boot_stage", stage="primary_shared_barrier", pc="0x63b0124", cpu=0)
            client.send_event(
                "swap",
                frame_id="frame-0000.png",
                swap_count=1,
                draw_calls=15,
                render_scene="0x1002",
                render_modal="0x0",
                source_scene="0x1002",
                source_modal="0x0",
                ui_state={"state": "0x2", "mode": "0x1"},
            )
            self.assertTrue(wait_until(lambda: len(self.server.snapshot()["swaps"]) == 1))

            # Before ready, enqueue_key returns 409
            ok, st, resp = self.server.enqueue_key("KEY_RIGHT")
            self.assertFalse(ok)
            self.assertEqual(st, 409)
            self.assertEqual(resp["error"], "not_ready_for_input")

            # Unknown key returns 400
            ok, st, resp = self.server.enqueue_key("POWER_OFF")
            self.assertFalse(ok)
            self.assertEqual(st, 400)

            client.send_event("ready", ui_state={"state": "0x2", "mode": "0x1"})
            self.assertTrue(wait_until(lambda: self.server.snapshot()["interactive_ready"]))

            # Fill bounded queue to max_pending (8)
            ids = []
            for i in range(ic.MAX_PENDING_KEYS):
                ok, st, resp = self.server.enqueue_key("KEY_DOWN", request_id=f"req-{i}")
                self.assertTrue(ok)
                self.assertEqual(st, 202)
                ids.append(resp["id"])

            # 9th pending click must be rejected cleanly with 429
            ok, st, resp = self.server.enqueue_key("KEY_UP", request_id="req-overflow")
            self.assertFalse(ok)
            self.assertEqual(st, 429)
            self.assertEqual(resp["error"], "key_queue_full")

            # Wait for client to receive all 8 clicks, then dispatch & consume them
            self.assertTrue(wait_until(lambda: client.pending_count() == ic.MAX_PENDING_KEYS))
            for req_id in ids:
                item = client.pop_next_key()
                self.assertIsNotNone(item)
                self.assertEqual(item["id"], req_id)
                client.send_event("key_dispatched", id=req_id, key=item["key"], action=1, swap_count=1)
                client.send_event("key_dispatched", id=req_id, key=item["key"], action=0, swap_count=1)
                client.send_event("key_consumed", id=req_id, key=item["key"], duration_ms=12.5, swap_count=2)

            self.assertTrue(wait_until(lambda: self.server.snapshot()["pending_count"] == 0))
            snap = self.server.snapshot()
            self.assertEqual(len(snap["key_history"]), ic.MAX_PENDING_KEYS)
            self.assertTrue(all(h["status"] == "consumed" for h in snap["key_history"]))

            # Stop signal
            self.assertTrue(self.server.request_stop("unit_test_stop"))
            self.assertTrue(wait_until(lambda: client.stop_requested))
            self.assertEqual(client.stop_reason, "unit_test_stop")
            client.send_event("stopped", stop_reason="unit_test_stop", qemu_exit_code=0)
            self.assertTrue(self.server.wait_stopped(timeout=3.0))
            self.assertTrue(self.server.snapshot()["stopped"])
            self.assertFalse(self.server.snapshot()["interactive_ready"])
        finally:
            client.close()

    def test_client_side_queue_overflow_emits_key_rejected(self) -> None:
        # Directly test client-side bounded queue rejection with max_pending=2
        client = ic.InteractiveControlClient(self.server.port, self.server.token, max_pending=2)
        try:
            client.send_event("ready")
            self.assertTrue(wait_until(lambda: self.server.snapshot()["interactive_ready"]))
            for i in range(3):
                ok, st, _ = self.server.enqueue_key("KEY_DELETE", request_id=f"c-{i}")
                self.assertTrue(ok)
                self.assertEqual(st, 202)
            self.assertTrue(
                wait_until(
                    lambda: any(
                        e["kind"] == "key_rejected" and e["reason"] == "queue_full"
                        for e in self.server.snapshot()["errors"]
                    )
                )
            )
            self.assertEqual(client.pending_count(), 2)
        finally:
            client.close()


class ExtendedLCDRefreshProbeTests(unittest.TestCase):
    def test_default_bound_preserved_and_extended_bound_capped(self) -> None:
        class DummyEmu:
            pass

        emu = DummyEmu()
        # Default mode still caps at 64
        p64 = lrp.LCDRefreshProbe(emu, max_pulses=64)
        self.assertEqual(p64.max_pulses, 64)
        with self.assertRaises(ValueError):
            lrp.LCDRefreshProbe(emu, max_pulses=65)

        # Extended mode allows up to 4096 and rejects > 4096
        p_ext = lrp.LCDRefreshProbe(emu, max_pulses=2048, allow_extended_pulses=True)
        self.assertEqual(p_ext.max_pulses, 2048)
        with self.assertRaises(ValueError):
            lrp.LCDRefreshProbe(emu, max_pulses=4097, allow_extended_pulses=True)

        # Recording >64 pulses and >256 events keeps report lists bounded while tracking exact totals
        for i in range(100):
            p_ext._record_pulse({"irq": 232, "i": i})
        for i in range(400):
            p_ext._record_event({"kind": "ev", "i": i})
        self.assertEqual(len(p_ext.report["pulses"]), 64)
        self.assertEqual(p_ext.report["total_pulses"], 100)
        self.assertEqual(p_ext._pulse_count, 100)
        self.assertEqual(len(p_ext.report["events"]), 256)
        self.assertEqual(p_ext.report["total_events"], 400)


class InteractiveHTTPServerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_base = Path(os.environ.get("TMPDIR") or str(DEFAULT_TMPDIR))
        tmp_base.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="s5-interactive-unit-", dir=str(tmp_base)))
        self.session_dir = self.tmp_dir / "session-test"
        self.gfx_dir = self.session_dir / "graphics"
        self.gfx_dir.mkdir(parents=True, exist_ok=True)

        self.valid_png = make_valid_png(16, 12, (40, 120, 210, 255))
        (self.gfx_dir / "frame-0000.png").write_bytes(self.valid_png)
        # Incomplete PNG missing IEND
        (self.gfx_dir / "frame-0001.png").write_bytes(self.valid_png[:-16])
        # Unswapped incomplete surface
        (self.gfx_dir / "incomplete-surface-60000002.png").write_bytes(self.valid_png)
        # Symlink escape outside graphics/
        self.outside_png = self.tmp_dir / "outside-secret.png"
        self.outside_png.write_bytes(self.valid_png)
        os.symlink(str(self.outside_png), str(self.gfx_dir / "frame-0099.png"))

        self.controller = s5i.InteractiveSessionController(
            session_dir=self.session_dir,
            boot_to="menu",
            max_seconds=300,
        )
        self.server, self.url = s5i.create_interactive_server(self.controller, host="127.0.0.1", port=0)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

        self.client = ic.InteractiveControlClient(self.controller.control.port, self.controller.control.token)
        self.client.send_event(
            "swap",
            frame_id="frame-0000.png",
            swap_count=1,
            draw_calls=15,
            render_scene="0x1002",
            render_modal="0x0",
        )
        self.client.send_event("ready", ui_state={"state": "0x2", "mode": "0x1"})
        self.assertTrue(wait_until(lambda: self.controller.control.snapshot()["interactive_ready"]))

    def tearDown(self) -> None:
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3.0)
        self.controller.control.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_non_loopback_hosts_rejected(self) -> None:
        for bad in ("0.0.0.0", "192.168.1.50", "::", "example.com", ""):
            with self.assertRaises(ValueError):
                s5i.create_interactive_server(self.controller, host=bad, port=0)

    def test_index_security_headers_and_no_unsafe_dom_sinks(self) -> None:
        st, hdrs, body = raw_http(self.host, self.port, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("content-security-policy", hdrs)
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
        self.assertEqual(hdrs.get("x-frame-options"), "DENY")
        text = body.decode("utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(forbidden, text)

    def test_csrf_and_origin_enforcement_on_post_endpoints(self) -> None:
        payload = json.dumps({"key": "KEY_RIGHT"}).encode("utf-8")

        # Missing CSRF token -> 403
        st, _, _ = raw_http(self.host, self.port, "POST", "/api/key", headers={"Content-Type": "application/json"}, body=payload)
        self.assertEqual(st, 403)
        st, _, _ = raw_http(self.host, self.port, "POST", "/api/stop", headers={"Content-Type": "application/json"}, body=b"{}")
        self.assertEqual(st, 403)

        # Wrong CSRF token -> 403
        st, _, _ = raw_http(
            self.host,
            self.port,
            "POST",
            "/api/key",
            headers={"Content-Type": "application/json", "X-S5-Session-Token": "bad-token"},
            body=payload,
        )
        self.assertEqual(st, 403)

        # Cross-origin Origin header even with valid token -> 403
        st, _, _ = raw_http(
            self.host,
            self.port,
            "POST",
            "/api/key",
            headers={
                "Content-Type": "application/json",
                "X-S5-Session-Token": self.controller.csrf_token,
                "Origin": "http://evil.example:8080",
            },
            body=payload,
        )
        self.assertEqual(st, 403)

        # Valid token + unknown key -> 400
        st, _, body = raw_http(
            self.host,
            self.port,
            "POST",
            "/api/key",
            headers={
                "Content-Type": "application/json",
                "X-S5-Session-Token": self.controller.csrf_token,
            },
            body=json.dumps({"key": "POWER_OFF"}).encode("utf-8"),
        )
        self.assertEqual(st, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_key")

        # Valid token + valid key -> 202 and arrives at control client
        st, _, body = raw_http(
            self.host,
            self.port,
            "POST",
            "/api/key",
            headers={
                "Content-Type": "application/json",
                "X-S5-Session-Token": self.controller.csrf_token,
            },
            body=payload,
        )
        self.assertEqual(st, 202)
        parsed = json.loads(body)
        self.assertTrue(parsed["ok"])
        self.assertTrue(wait_until(lambda: self.client.has_pending_key()))
        cmd = self.client.pop_next_key()
        self.assertEqual(cmd["key"], "KEY_RIGHT")

        # Valid token on POST /api/stop -> 202 and triggers stop_requested
        st, _, _ = raw_http(
            self.host,
            self.port,
            "POST",
            "/api/stop",
            headers={
                "Content-Type": "application/json",
                "X-S5-Session-Token": self.controller.csrf_token,
            },
            body=b"{}",
        )
        self.assertEqual(st, 202)
        self.assertTrue(wait_until(lambda: self.client.stop_requested))

    def test_frame_serving_validation_and_traversal_rejection(self) -> None:
        # Valid PNG served byte-identically
        st, hdrs, body = raw_http(self.host, self.port, "GET", "/api/frame/frame-0000.png")
        self.assertEqual(st, 200)
        self.assertEqual(hdrs.get("content-type"), "image/png")
        self.assertEqual(body, self.valid_png)

        # Download mode
        st, hdrs, body = raw_http(self.host, self.port, "GET", "/api/frame/frame-0000.png?download=1")
        self.assertEqual(st, 200)
        self.assertIn('attachment; filename="frame-0000.png"', hdrs.get("content-disposition", ""))
        self.assertEqual(body, self.valid_png)

        # Incomplete PNG -> 409
        st, _, _ = raw_http(self.host, self.port, "GET", "/api/frame/frame-0001.png")
        self.assertEqual(st, 409)

        # Missing PNG -> 404
        st, _, _ = raw_http(self.host, self.port, "GET", "/api/frame/frame-0002.png")
        self.assertEqual(st, 404)

        # Path traversal, percent-encoding, unswapped incomplete surface, symlink escape -> 400 or 403
        for attack in (
            "/api/frame/../../outside-secret.png",
            "/api/frame/%2e%2e/outside-secret.png",
            "/api/frame/..%2fresult.json",
            "/api/frame/incomplete-surface-60000002.png",
            "/api/frame/frame-0099.png",
        ):
            st, _, _ = raw_http(self.host, self.port, "GET", attack)
            self.assertIn(st, (400, 403), f"Expected 400/403 for {attack}, got {st}")

    def test_boot_command_profiles(self) -> None:
        cmd_menu = s5i.build_boot_command(
            boot_to="menu",
            output_dir=self.session_dir,
            max_seconds=1800,
            control_port=12345,
            control_token="tok",
        )
        self.assertIn("--key-sequence", cmd_menu)
        self.assertIn("--interactive-ready-target", cmd_menu)
        self.assertIn("scene=0x1002,modal=0x0", cmd_menu)
        self.assertIn("--shutter-s2-input-high", cmd_menu)
        self.assertIn("--lens-release-input-high", cmd_menu)

        cmd_lv = s5i.build_boot_command(
            boot_to="liveview",
            output_dir=self.session_dir,
            max_seconds=600,
            control_port=12345,
            control_token="tok",
        )
        self.assertIn("scene=0x1001,modal=0x0", cmd_lv)

        cmd_wiz = s5i.build_boot_command(
            boot_to="wizard",
            output_dir=self.session_dir,
            max_seconds=600,
            control_port=12345,
            control_token="tok",
        )
        self.assertNotIn("--key-sequence", cmd_wiz)
        self.assertNotIn("--interactive-ready-target", cmd_wiz)


if __name__ == "__main__":
    unittest.main(verbosity=2)
