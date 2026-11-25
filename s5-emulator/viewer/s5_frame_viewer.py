#!/usr/bin/env python3
"""Read-only local native frame viewer for Panasonic LUMIX S5 QEMU runs.

Strictly read-only on --result-dir. Serves only real PNG bytes produced by
`software_swap` events in `graphics/calls.jsonl`, plus structured status from
`result.json`. Uses only the Python 3 standard library with zero external or
network dependencies.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import html
import ipaddress
import json
import os
import re
import signal
import socketserver
import struct
import sys
import threading
import urllib.parse
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FRAME_FILENAME_RE = re.compile(r"^frame-[0-9A-Za-z_-]+\.png$")
FRAME_NUMBER_RE = re.compile(r"^frame-(\d+)\.png$")
INCOMPLETE_SURFACE_RE = re.compile(r"^incomplete-surface-[0-9A-Fa-f]+\.png$")
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024


def validate_loopback_host(host: str) -> str:
    """Validate that the bind address is strictly loopback."""
    candidate = host.strip()
    if not candidate:
        raise ValueError("Host must not be empty")
    if candidate.lower() == "localhost":
        return "127.0.0.1"
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(
            f"Refusing non-loopback or invalid host {host!r}; only 127.0.0.1, ::1, or localhost are allowed"
        ) from exc
    if not ip.is_loopback:
        raise ValueError(
            f"Refusing non-loopback host {host!r}; viewer is restricted to loopback interfaces only"
        )
    return candidate


def validate_png_bytes(data: bytes) -> dict[str, Any]:
    """Validate PNG signature, chunk layout, CRCs, IHDR, and IEND.

    Returns a dict with keys:
      valid (bool), reason (str|None), width (int|None), height (int|None),
      has_idat (bool), has_iend (bool)
    """
    if len(data) < len(PNG_SIGNATURE) + 12:
        return {
            "valid": False,
            "reason": "truncated_header",
            "width": None,
            "height": None,
            "has_idat": False,
            "has_iend": False,
        }
    if data[:8] != PNG_SIGNATURE:
        return {
            "valid": False,
            "reason": "invalid_png_signature",
            "width": None,
            "height": None,
            "has_idat": False,
            "has_iend": False,
        }

    offset = 8
    total = len(data)
    saw_ihdr = False
    saw_idat = False
    saw_iend = False
    width = None
    height = None
    chunk_index = 0

    while offset < total:
        if offset + 12 > total:
            return {
                "valid": False,
                "reason": "truncated_chunk_header",
                "width": width,
                "height": height,
                "has_idat": saw_idat,
                "has_iend": False,
            }
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > total:
            return {
                "valid": False,
                "reason": "truncated_chunk_body",
                "width": width,
                "height": height,
                "has_idat": saw_idat,
                "has_iend": False,
            }
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return {
                "valid": False,
                "reason": "chunk_crc_mismatch",
                "width": width,
                "height": height,
                "has_idat": saw_idat,
                "has_iend": False,
            }

        if chunk_index == 0:
            if chunk_type != b"IHDR" or length != 13:
                return {
                    "valid": False,
                    "reason": "invalid_ihdr",
                    "width": None,
                    "height": None,
                    "has_idat": False,
                    "has_iend": False,
                }
            width, height = struct.unpack(">II", chunk_data[:8])
            if width == 0 or height == 0:
                return {
                    "valid": False,
                    "reason": "invalid_dimensions",
                    "width": width,
                    "height": height,
                    "has_idat": False,
                    "has_iend": False,
                }
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            saw_idat = True
        elif chunk_type == b"IEND":
            if length != 0:
                return {
                    "valid": False,
                    "reason": "invalid_iend_length",
                    "width": width,
                    "height": height,
                    "has_idat": saw_idat,
                    "has_iend": False,
                }
            saw_iend = True
            offset = chunk_end
            break

        offset = chunk_end
        chunk_index += 1

    if not saw_ihdr:
        return {
            "valid": False,
            "reason": "missing_ihdr",
            "width": None,
            "height": None,
            "has_idat": saw_idat,
            "has_iend": saw_iend,
        }
    if not saw_idat:
        return {
            "valid": False,
            "reason": "missing_idat",
            "width": width,
            "height": height,
            "has_idat": False,
            "has_iend": saw_iend,
        }
    if not saw_iend:
        return {
            "valid": False,
            "reason": "missing_iend",
            "width": width,
            "height": height,
            "has_idat": saw_idat,
            "has_iend": False,
        }
    return {
        "valid": True,
        "reason": None,
        "width": width,
        "height": height,
        "has_idat": saw_idat,
        "has_iend": True,
    }


def _extract_safe_frame_basename(raw_path: Any) -> str | None:
    """Extract and strictly validate a frame basename from a swap record path.

    Note: calls.jsonl may carry an absolute path from a moved/original host
    directory (e.g. /Users/.../graphics/frame-0000.png). We NEVER open that
    host path; we extract only the final filename segment and verify it has
    no traversal semantics and matches FRAME_FILENAME_RE.
    """
    if not isinstance(raw_path, str) or not raw_path:
        return None
    if "\x00" in raw_path:
        return None
    normalized = raw_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        return None
    name = parts[-1]
    if not FRAME_FILENAME_RE.match(name):
        return None
    return name


def _format_utc_iso(ts: float) -> str:
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _is_within_directory(base_real: str, target_real: str) -> bool:
    try:
        return os.path.commonpath([base_real, target_real]) == base_real and target_real != base_real
    except ValueError:
        return False


def _trim_scope_value(value: Any) -> Any:
    """Summarize structured metadata from result.json without leaking host paths."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in ("calls", "pulses", "requests", "events", "steps") and isinstance(v, list):
                out[f"{k}_count"] = len(v)
            elif k in ("path", "output", "graphics_frames"):
                continue
            elif isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            elif isinstance(v, list):
                if len(v) <= 12 and all(isinstance(item, (str, int, float, bool)) or item is None for item in v):
                    out[k] = v
                else:
                    out[f"{k}_count"] = len(v)
            elif isinstance(v, dict):
                if len(v) <= 16:
                    out[k] = _trim_scope_value(v)
                else:
                    out[f"{k}_keys_count"] = len(v)
        return out
    if isinstance(value, list):
        return [item for item in value[:20] if isinstance(item, (str, int, float, bool)) or item is None]
    return value


class ResultDirScanner:
    """Strictly read-only inspector for an S5 emulator result directory."""

    def __init__(self, result_dir: Path) -> None:
        self._input_path = Path(result_dir)
        self._result_dir_real = os.path.realpath(str(self._input_path))
        self._result_dir_name = self._input_path.name or "result"

    @property
    def result_dir_name(self) -> str:
        return self._result_dir_name

    def _safe_child_path(self, *parts: str) -> tuple[str | None, str | None]:
        """Resolve a child path inside result_dir without following escapes.

        Returns (real_path, error_code). Never creates or mutates any filesystem entry.
        """
        if not os.path.isdir(self._result_dir_real):
            return None, "result_dir_missing"
        current = self._result_dir_real
        for part in parts:
            if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
                return None, "invalid_path_component"
            candidate = os.path.join(current, part)
            if os.path.islink(candidate):
                resolved_link = os.path.realpath(candidate)
                if not _is_within_directory(self._result_dir_real, resolved_link):
                    return None, "symlink_escape"
                return None, "symlink_rejected"
            current = candidate
        resolved = os.path.realpath(current)
        if not _is_within_directory(self._result_dir_real, resolved):
            return None, "path_escape"
        return resolved, None

    def _safe_graphics_file(self, filename: str) -> tuple[str | None, str | None]:
        """Resolve a frame file strictly inside <result_dir>/graphics/."""
        if not FRAME_FILENAME_RE.match(filename):
            return None, "invalid_filename"
        graphics_real, err = self._safe_child_path("graphics")
        if err or graphics_real is None:
            return None, err or "graphics_dir_missing"
        if not os.path.isdir(graphics_real):
            return None, "graphics_dir_missing"
        candidate = os.path.join(graphics_real, filename)
        if os.path.islink(candidate):
            resolved_link = os.path.realpath(candidate)
            if not _is_within_directory(graphics_real, resolved_link):
                return None, "symlink_escape"
            return None, "symlink_rejected"
        resolved = os.path.realpath(candidate)
        if not _is_within_directory(graphics_real, resolved):
            return None, "path_escape"
        if not os.path.exists(resolved):
            return None, "missing_file"
        if not os.path.isfile(resolved):
            return None, "not_a_file"
        return resolved, None

    def _read_and_validate_png(self, filename: str) -> tuple[bytes | None, dict[str, Any]]:
        """Read PNG bytes read-only and validate completeness in memory."""
        resolved, err = self._safe_graphics_file(filename)
        if err or resolved is None:
            return None, {
                "png_status": err or "missing_file",
                "png_bytes": None,
                "png_sha256": None,
                "width": None,
                "height": None,
                "file_mtime_epoch": None,
                "file_mtime_iso": None,
                "validation_reason": err or "missing_file",
            }
        try:
            st = os.stat(resolved)
        except OSError:
            return None, {
                "png_status": "missing_file",
                "png_bytes": None,
                "png_sha256": None,
                "width": None,
                "height": None,
                "file_mtime_epoch": None,
                "file_mtime_iso": None,
                "validation_reason": "stat_failed",
            }
        if st.st_size > MAX_PNG_BYTES:
            return None, {
                "png_status": "invalid_png",
                "png_bytes": st.st_size,
                "png_sha256": None,
                "width": None,
                "height": None,
                "file_mtime_epoch": st.st_mtime,
                "file_mtime_iso": _format_utc_iso(st.st_mtime),
                "validation_reason": "exceeds_max_png_bytes",
            }
        try:
            with open(resolved, "rb") as fh:
                data = fh.read(MAX_PNG_BYTES + 1)
        except OSError:
            return None, {
                "png_status": "missing_file",
                "png_bytes": None,
                "png_sha256": None,
                "width": None,
                "height": None,
                "file_mtime_epoch": st.st_mtime,
                "file_mtime_iso": _format_utc_iso(st.st_mtime),
                "validation_reason": "read_failed",
            }
        if len(data) > MAX_PNG_BYTES:
            return None, {
                "png_status": "invalid_png",
                "png_bytes": len(data),
                "png_sha256": None,
                "width": None,
                "height": None,
                "file_mtime_epoch": st.st_mtime,
                "file_mtime_iso": _format_utc_iso(st.st_mtime),
                "validation_reason": "exceeds_max_png_bytes",
            }

        check = validate_png_bytes(data)
        if not check["valid"]:
            return None, {
                "png_status": "incomplete_png",
                "png_bytes": len(data),
                "png_sha256": None,
                "width": check["width"],
                "height": check["height"],
                "file_mtime_epoch": st.st_mtime,
                "file_mtime_iso": _format_utc_iso(st.st_mtime),
                "validation_reason": check["reason"],
            }

        return data, {
            "png_status": "ready",
            "png_bytes": len(data),
            "png_sha256": hashlib.sha256(data).hexdigest(),
            "width": check["width"],
            "height": check["height"],
            "file_mtime_epoch": st.st_mtime,
            "file_mtime_iso": _format_utc_iso(st.st_mtime),
            "validation_reason": None,
        }

    def _scan_calls_jsonl(self) -> dict[str, Any]:
        calls_real, err = self._safe_child_path("graphics", "calls.jsonl")
        if err or calls_real is None or not os.path.isfile(calls_real):
            return {
                "exists": False,
                "total_lines": 0,
                "parsed_records": 0,
                "truncated_tail_ignored": False,
                "malformed_lines_ignored": 0,
                "swaps": [],
                "error": None if err in (None, "result_dir_missing") else err,
            }

        try:
            with open(calls_real, "rb") as fh:
                raw = fh.read()
        except OSError:
            return {
                "exists": False,
                "total_lines": 0,
                "parsed_records": 0,
                "truncated_tail_ignored": False,
                "malformed_lines_ignored": 0,
                "swaps": [],
                "error": "read_failed",
            }

        if not raw:
            return {
                "exists": True,
                "total_lines": 0,
                "parsed_records": 0,
                "truncated_tail_ignored": False,
                "malformed_lines_ignored": 0,
                "swaps": [],
                "error": None,
            }

        ends_with_newline = raw.endswith(b"\n")
        raw_lines = raw.splitlines()
        parsed_records = 0
        truncated_tail_ignored = False
        malformed_lines_ignored = 0
        swaps: list[dict[str, Any]] = []
        swap_seq = 0

        for idx, raw_line in enumerate(raw_lines):
            is_last = idx == len(raw_lines) - 1
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                text_line = stripped.decode("utf-8")
                obj = json.loads(text_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if is_last and not ends_with_newline:
                    truncated_tail_ignored = True
                elif is_last:
                    truncated_tail_ignored = True
                else:
                    malformed_lines_ignored += 1
                continue

            if not isinstance(obj, dict):
                malformed_lines_ignored += 1
                continue

            parsed_records += 1
            if obj.get("name") != "software_swap":
                continue

            filename = _extract_safe_frame_basename(obj.get("path"))
            frame_number = None
            if filename:
                m = FRAME_NUMBER_RE.match(filename)
                if m:
                    frame_number = int(m.group(1))
                _, png_meta = self._read_and_validate_png(filename)
            else:
                png_meta = {
                    "png_status": "invalid_path",
                    "png_bytes": None,
                    "png_sha256": None,
                    "width": None,
                    "height": None,
                    "file_mtime_epoch": None,
                    "file_mtime_iso": None,
                    "validation_reason": "unsafe_or_unrecognized_frame_filename",
                }

            event_ts = None
            timestamp_source = "unavailable"
            for ts_key in ("timestamp", "ts", "time"):
                if ts_key in obj and obj[ts_key] is not None:
                    event_ts = str(obj[ts_key])
                    timestamp_source = "event"
                    break
            if event_ts is None and png_meta["file_mtime_iso"] is not None:
                event_ts = png_meta["file_mtime_iso"]
                timestamp_source = "file_mtime"

            ready = png_meta["png_status"] == "ready"
            swaps.append(
                {
                    "swap_seq": swap_seq,
                    "frame_number": frame_number,
                    "filename": filename,
                    "trace_index": obj.get("index"),
                    "line_number": idx + 1,
                    "surface": obj.get("surface"),
                    "draw_calls": obj.get("draw_calls"),
                    "pixels_sha256": obj.get("pixels_sha256"),
                    "provenance": obj.get("provenance"),
                    "timestamp": event_ts,
                    "timestamp_source": timestamp_source,
                    "png_status": png_meta["png_status"],
                    "validation_reason": png_meta["validation_reason"],
                    "png_bytes": png_meta["png_bytes"],
                    "png_sha256": png_meta["png_sha256"],
                    "width": png_meta["width"],
                    "height": png_meta["height"],
                    "ready": ready,
                    "image_url": f"/api/frames/{filename}" if ready and filename else None,
                    "download_url": f"/api/frames/{filename}/download" if ready and filename else None,
                }
            )
            swap_seq += 1

        return {
            "exists": True,
            "total_lines": len(raw_lines),
            "parsed_records": parsed_records,
            "truncated_tail_ignored": truncated_tail_ignored,
            "malformed_lines_ignored": malformed_lines_ignored,
            "swaps": swaps,
            "error": None,
        }

    def _scan_incomplete_surfaces(self) -> list[str]:
        """List non-swapped incomplete-surface PNG basenames for diagnostics only."""
        graphics_real, err = self._safe_child_path("graphics")
        if err or graphics_real is None or not os.path.isdir(graphics_real):
            return []
        found: list[str] = []
        try:
            for entry in sorted(os.listdir(graphics_real)):
                if INCOMPLETE_SURFACE_RE.match(entry):
                    found.append(entry)
        except OSError:
            return []
        return found

    def _scan_result_json(self) -> tuple[dict[str, Any] | None, str]:
        result_real, err = self._safe_child_path("result.json")
        if err or result_real is None or not os.path.exists(result_real):
            return None, "missing"
        if not os.path.isfile(result_real):
            return None, "not_a_file"
        try:
            st = os.stat(result_real)
            if st.st_size > MAX_JSON_BYTES:
                return None, "too_large"
            with open(result_real, "rb") as fh:
                raw = fh.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                return None, "too_large"
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, "incomplete_or_invalid_json"
        if not isinstance(parsed, dict):
            return None, "invalid_root_type"
        return parsed, "ok"

    def _extract_experiment_scope(self, result_data: dict[str, Any] | None) -> dict[str, Any]:
        if result_data is None:
            return {
                "available": False,
                "note": "result.json has not been written yet; local HLE/TEST experiment scope will populate on completion.",
                "items": {},
            }

        items: dict[str, Any] = {}
        scalar_keys = [
            "original_firmware_instructions_modified",
            "peer_flags_manually_cleared",
            "graphics_model",
            "graphics_frame_scope",
            "storage_model",
            "spi0_model",
            "spi6_model",
            "tx_serial_model",
            "linux_peer_model",
        ]
        for key in scalar_keys:
            if key in result_data:
                items[key] = result_data[key]

        structured_keys = [
            "synthetic_state",
            "display_isolation",
            "ram_model",
            "adc_environment",
            "display_clock_test_probe",
            "display_test_inputs",
            "frame_irq_test_probe",
            "frame_test_inputs",
            "lcd_refresh_test_probe",
            "lens_release_input",
            "software_key_probe",
            "software_key_sequence",
            "original_ui_service_probe",
            "original_ui_service_loop_probe",
        ]
        for key in structured_keys:
            if key in result_data:
                items[key] = _trim_scope_value(result_data[key])

        return {
            "available": True,
            "note": (
                "Local HLE/TEST conditions and device models used for this run. "
                "Local experiments do not represent full physical camera boot."
            ),
            "items": items,
        }

    def _build_boot_status(
        self,
        result_data: dict[str, Any] | None,
        result_parse_status: str,
        calls_info: dict[str, Any],
    ) -> dict[str, Any]:
        if result_data is None:
            run_state = "running_waiting_result" if calls_info["exists"] else "waiting_for_run"
            if result_parse_status == "incomplete_or_invalid_json":
                run_state = "writing_result"
            return {
                "result_present": False,
                "result_parse_status": result_parse_status,
                "run_state": run_state,
                "menu_rendered": None,
                "menu_rendered_verbatim_note": (
                    "result.json not yet present; menu_rendered is unknown and never inferred from swap count."
                ),
                "stop_reason": None,
                "qemu_exit_code": None,
                "graphics_error": None,
                "original_ui_state_at_stop": None,
                "result_graphics_draws": None,
                "result_graphics_swaps": None,
            }

        # Preserve menu_rendered strictly verbatim. NEVER upgrade based on swaps or text.
        menu_rendered = result_data.get("menu_rendered")
        stop_reason = result_data.get("stop_reason")
        qemu_exit_code = result_data.get("qemu_exit_code")
        graphics_error = result_data.get("graphics_error")
        ui_state = result_data.get("original_ui_state_at_stop")

        failed = False
        if graphics_error is not None:
            failed = True
        if isinstance(qemu_exit_code, int) and qemu_exit_code != 0:
            failed = True
        if isinstance(stop_reason, str) and any(
            token in stop_reason.lower()
            for token in ("error", "invalid", "unsupported", "rejected", "failed", "fault", "unexpected")
        ):
            failed = True

        run_state = "failed" if failed else "completed"
        if menu_rendered is False:
            note = (
                "menu_rendered is verbatim false in result.json. "
                "Swap count or visible prompt text does NOT mean main menu or full boot succeeded."
            )
        elif menu_rendered is True:
            note = "menu_rendered is verbatim true in result.json."
        else:
            note = "menu_rendered is absent or null in result.json; never upgraded by viewer."

        return {
            "result_present": True,
            "result_parse_status": result_parse_status,
            "run_state": run_state,
            "menu_rendered": menu_rendered,
            "menu_rendered_verbatim_note": note,
            "stop_reason": stop_reason,
            "qemu_exit_code": qemu_exit_code,
            "graphics_error": graphics_error,
            "original_ui_state_at_stop": ui_state if isinstance(ui_state, dict) else None,
            "result_graphics_draws": result_data.get("graphics_draws"),
            "result_graphics_swaps": result_data.get("graphics_swaps"),
        }

    def get_state(self) -> dict[str, Any]:
        """Compute a complete, read-only JSON-serializable state snapshot."""
        dir_exists = os.path.isdir(self._result_dir_real)
        calls_info = self._scan_calls_jsonl()
        result_data, result_parse_status = self._scan_result_json()
        incomplete_surfaces = self._scan_incomplete_surfaces()

        swaps = calls_info["swaps"]
        ready_frames = [frame for frame in swaps if frame["ready"]]
        pending_frames = [frame for frame in swaps if not frame["ready"]]
        latest_ready = ready_frames[-1] if ready_frames else None
        latest_swap_record = swaps[-1] if swaps else None

        waiting_for_next_complete = False
        waiting_reason = None
        if not dir_exists:
            waiting_for_next_complete = True
            waiting_reason = "Waiting for result directory to become available."
        elif not calls_info["exists"]:
            waiting_for_next_complete = True
            waiting_reason = "Waiting for graphics/calls.jsonl and first complete software_swap PNG."
        elif latest_swap_record is not None and not latest_swap_record["ready"]:
            waiting_for_next_complete = True
            fname = latest_swap_record.get("filename") or "(invalid filename)"
            st = latest_swap_record.get("png_status")
            if st == "incomplete_png":
                waiting_reason = (
                    f"Waiting for next complete swap frame: {fname} is still writing or incomplete."
                )
            elif st == "missing_file":
                waiting_reason = (
                    f"Referenced swap frame {fname} is missing from graphics/."
                )
            else:
                waiting_reason = (
                    f"Referenced swap frame {fname} is unavailable ({st})."
                )
        elif not ready_frames:
            waiting_for_next_complete = True
            waiting_reason = "No complete software_swap PNG recorded yet; waiting for next complete swap frame."

        warnings: list[str] = []
        if calls_info["truncated_tail_ignored"]:
            warnings.append("Ignored incomplete tail line in graphics/calls.jsonl (still writing).")
        if calls_info["malformed_lines_ignored"] > 0:
            warnings.append(
                f"Ignored {calls_info['malformed_lines_ignored']} malformed JSONL line(s) in graphics/calls.jsonl."
            )
        for frame in pending_frames:
            fname = frame.get("filename") or "<invalid>"
            warnings.append(
                f"Swap #{frame['swap_seq']} ({fname}): {frame['png_status']}"
                + (f" ({frame['validation_reason']})" if frame.get("validation_reason") else "")
            )
        if result_parse_status == "incomplete_or_invalid_json":
            warnings.append("result.json exists but is currently incomplete or invalid JSON.")

        boot_status = self._build_boot_status(result_data, result_parse_status, calls_info)
        experiment_scope = self._extract_experiment_scope(result_data)

        return {
            "viewer": {
                "read_only": True,
                "synthetic_placeholder_frames": False,
                "fake_camera_controls": False,
                "result_dir_name": self._result_dir_name,
                "result_dir_exists": dir_exists,
                "polled_at_utc": _format_utc_iso(datetime.datetime.now(datetime.timezone.utc).timestamp()),
            },
            "native_frames": {
                "scope_note": (
                    "Only raw unmodified PNG frames committed by actual software_swap events in "
                    "graphics/calls.jsonl are displayed. Unswapped surfaces and placeholder screens are never shown."
                ),
                "calls_jsonl_exists": calls_info["exists"],
                "trace_records_parsed": calls_info["parsed_records"],
                "truncated_tail_ignored": calls_info["truncated_tail_ignored"],
                "malformed_lines_ignored": calls_info["malformed_lines_ignored"],
                "total_swap_records": len(swaps),
                "ready_swap_count": len(ready_frames),
                "pending_or_failed_swap_count": len(pending_frames),
                "latest_ready_swap_seq": latest_ready["swap_seq"] if latest_ready else None,
                "latest_ready_filename": latest_ready["filename"] if latest_ready else None,
                "waiting_for_next_complete": waiting_for_next_complete,
                "waiting_reason": waiting_reason,
                "frames": ready_frames,
                "all_swap_records": swaps,
                "unswapped_incomplete_surfaces": incomplete_surfaces,
            },
            "boot_status": boot_status,
            "experiment_scope": experiment_scope,
            "warnings": warnings,
        }

    def get_frame_bytes(self, filename: str) -> tuple[bytes | None, HTTPStatus, str]:
        """Return exact raw PNG bytes for an authorized, complete software_swap frame."""
        if not FRAME_FILENAME_RE.match(filename):
            return None, HTTPStatus.BAD_REQUEST, "Invalid frame filename"

        # Verify file path containment BEFORE checking whitelist so symlink escapes are always 403.
        resolved, err = self._safe_graphics_file(filename)
        if err in ("symlink_escape", "symlink_rejected", "path_escape", "invalid_path_component"):
            return None, HTTPStatus.FORBIDDEN, f"Path rejected: {err}"

        # Enforce whitelist: only filenames referenced by an actual software_swap record.
        calls_info = self._scan_calls_jsonl()
        whitelisted = {
            swap["filename"]
            for swap in calls_info["swaps"]
            if swap.get("filename") is not None
        }
        if filename not in whitelisted:
            return None, HTTPStatus.NOT_FOUND, "Frame is not an authorized software_swap output"

        if err == "missing_file" or resolved is None:
            return None, HTTPStatus.NOT_FOUND, "Frame file not found"

        data, meta = self._read_and_validate_png(filename)
        if data is None:
            if meta["png_status"] == "incomplete_png":
                return None, HTTPStatus.CONFLICT, f"Frame PNG is incomplete ({meta['validation_reason']})"
            if meta["png_status"] in ("symlink_escape", "symlink_rejected", "path_escape"):
                return None, HTTPStatus.FORBIDDEN, f"Path rejected: {meta['png_status']}"
            return None, HTTPStatus.NOT_FOUND, f"Frame unavailable: {meta['png_status']}"

        return data, HTTPStatus.OK, "OK"


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S5 Native Swap Frame Viewer (Read-Only)</title>
<style>
  :root {
    --bg: #0f1318;
    --panel: #171d24;
    --panel-alt: #1d252f;
    --border: #2b3645;
    --text: #e6edf5;
    --muted: #93a4b8;
    --accent: #4aa3ff;
    --ok: #38c172;
    --warn: #f5b942;
    --err: #f25f5c;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.45;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 18px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  .title-block h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
  }
  .title-block .sub {
    color: var(--muted);
    font-size: 12px;
    margin-top: 2px;
  }
  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .badge {
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    font-family: var(--mono);
    border: 1px solid var(--border);
    background: var(--panel-alt);
  }
  .badge.ok { border-color: var(--ok); color: var(--ok); }
  .badge.warn { border-color: var(--warn); color: var(--warn); }
  .badge.err { border-color: var(--err); color: var(--err); }
  .badge.info { border-color: var(--accent); color: var(--accent); }
  main {
    display: grid;
    grid-template-columns: minmax(0, 1.85fr) minmax(320px, 1fr);
    gap: 16px;
    padding: 16px 18px 24px;
    max-width: 1680px;
    margin: 0 auto;
  }
  @media (max-width: 1020px) {
    main { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 14px;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 14px;
    background: var(--panel-alt);
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 13px;
  }
  .card-body {
    padding: 12px 14px;
  }
  .banner {
    padding: 9px 12px;
    border-radius: 6px;
    margin-bottom: 12px;
    font-size: 13px;
    border: 1px solid var(--border);
    background: var(--panel-alt);
  }
  .banner.warn { border-color: var(--warn); color: #ffd98a; background: rgba(245, 185, 66, 0.1); }
  .banner.err { border-color: var(--err); color: #ffb3b1; background: rgba(242, 95, 92, 0.12); }
  .hidden { display: none !important; }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin-bottom: 12px;
  }
  button, a.btn {
    appearance: none;
    border: 1px solid var(--border);
    background: var(--panel-alt);
    color: var(--text);
    padding: 6px 11px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  button:hover:not(:disabled), a.btn:hover:not(.disabled) {
    border-color: var(--accent);
  }
  button:disabled, a.btn.disabled {
    opacity: 0.45;
    cursor: not-allowed;
    pointer-events: none;
  }
  button.active {
    border-color: var(--warn);
    color: var(--warn);
  }
  .stage {
    background: #07090c;
    border: 1px solid var(--border);
    border-radius: 6px;
    min-height: 420px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 12px;
    overflow: auto;
  }
  .stage img {
    display: block;
    max-width: 100%;
    max-height: 72vh;
    width: auto;
    height: auto;
    border: 1px solid #263140;
    background: repeating-conic-gradient(#1b2028 0% 25%, #12161c 0% 50%) 50% / 16px 16px;
  }
  .empty-stage {
    color: var(--muted);
    text-align: center;
    max-width: 480px;
    padding: 24px;
    font-family: var(--mono);
    font-size: 13px;
  }
  .meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
    gap: 8px;
    margin-top: 12px;
  }
  .meta-item {
    background: var(--panel-alt);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 10px;
  }
  .meta-label {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .meta-value {
    font-family: var(--mono);
    font-size: 13px;
    margin-top: 2px;
    word-break: break-all;
  }
  .history-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    max-height: 280px;
    overflow-y: auto;
  }
  .history-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    width: 100%;
    text-align: left;
    font-family: var(--mono);
    font-size: 12px;
    padding: 7px 10px;
  }
  .history-row.selected {
    border-color: var(--accent);
    background: rgba(74, 163, 255, 0.14);
  }
  .history-row.pending {
    border-style: dashed;
    border-color: var(--warn);
    color: var(--muted);
  }
  table.kv {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  table.kv th, table.kv td {
    text-align: left;
    vertical-align: top;
    padding: 5px 6px;
    border-bottom: 1px solid #222b36;
  }
  table.kv th {
    color: var(--muted);
    font-weight: 500;
    width: 38%;
    font-family: var(--mono);
    word-break: break-word;
  }
  table.kv td {
    font-family: var(--mono);
    word-break: break-word;
    white-space: pre-wrap;
  }
  .scope-note {
    font-size: 12px;
    color: var(--muted);
    margin: 0 0 10px;
  }
  .warn-list {
    margin: 0;
    padding-left: 18px;
    font-family: var(--mono);
    font-size: 12px;
    color: #ffd98a;
  }
</style>
</head>
<body>
<header>
  <div class="title-block">
    <h1>LUMIX S5 Native Swap Frame Viewer</h1>
    <div class="sub" id="header-sub">Read-only local viewer — raw software_swap PNGs only (no synthetic camera UI, no input controls)</div>
  </div>
  <div class="badges">
    <span class="badge info" id="badge-dir">dir: ...</span>
    <span class="badge" id="badge-run">state: ...</span>
    <span class="badge" id="badge-menu">menu_rendered: ...</span>
    <span class="badge" id="badge-follow">auto-follow: ON</span>
  </div>
</header>

<main>
  <section>
    <div class="card">
      <div class="card-header">
        <span>1. Native UI Swap Frame (Raw Unmodified PNG)</span>
        <span id="frame-counter" style="font-family: var(--mono); color: var(--muted);">0 / 0</span>
      </div>
      <div class="card-body">
        <div id="waiting-banner" class="banner warn hidden"></div>
        <div id="error-banner" class="banner err hidden"></div>

        <div class="controls" aria-label="Viewer navigation controls">
          <button type="button" id="btn-first" disabled>|&lt; First</button>
          <button type="button" id="btn-prev" disabled>&lt; Prev</button>
          <button type="button" id="btn-next" disabled>Next &gt;</button>
          <button type="button" id="btn-latest" disabled>Latest &gt;|</button>
          <button type="button" id="btn-follow">Pause Auto-Follow</button>
          <button type="button" id="btn-refresh">Refresh</button>
          <a id="btn-download" class="btn disabled" href="#" download>Download Raw PNG</a>
        </div>

        <div class="stage" id="frame-stage">
          <img id="frame-img" class="hidden" alt="Native firmware software_swap PNG">
          <div id="empty-stage" class="empty-stage">Waiting for first complete software_swap PNG...</div>
        </div>

        <div class="meta-grid" id="frame-meta">
          <div class="meta-item"><div class="meta-label">Filename</div><div class="meta-value" id="meta-filename">—</div></div>
          <div class="meta-item"><div class="meta-label">Swap Seq / Frame #</div><div class="meta-value" id="meta-seq">—</div></div>
          <div class="meta-item"><div class="meta-label">Trace Index</div><div class="meta-value" id="meta-index">—</div></div>
          <div class="meta-item"><div class="meta-label">Draw Calls</div><div class="meta-value" id="meta-draws">—</div></div>
          <div class="meta-item"><div class="meta-label">Dimensions / Bytes</div><div class="meta-value" id="meta-size">—</div></div>
          <div class="meta-item"><div class="meta-label">Timestamp</div><div class="meta-value" id="meta-time">—</div></div>
          <div class="meta-item"><div class="meta-label">PNG SHA-256 (prefix)</div><div class="meta-value" id="meta-sha">—</div></div>
          <div class="meta-item"><div class="meta-label">Surface</div><div class="meta-value" id="meta-surface">—</div></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span>Swap Frame History (Chronological)</span>
        <span id="trace-stats" style="font-family: var(--mono); color: var(--muted);">—</span>
      </div>
      <div class="card-body">
        <p class="scope-note" id="native-scope-note"></p>
        <div class="history-list" id="history-list"></div>
      </div>
    </div>
  </section>

  <aside>
    <div class="card">
      <div class="card-header">
        <span>2. Run &amp; Full-Boot Status (result.json)</span>
        <span id="result-presence" style="font-family: var(--mono); color: var(--muted);">—</span>
      </div>
      <div class="card-body">
        <p class="scope-note" id="menu-verbatim-note"></p>
        <table class="kv"><tbody id="boot-status-body"></tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span>Original UI State at Stop</span>
      </div>
      <div class="card-body">
        <table class="kv"><tbody id="ui-state-body"></tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span>3. Local HLE / TEST Experiment Scope</span>
      </div>
      <div class="card-body">
        <p class="scope-note" id="experiment-scope-note"></p>
        <table class="kv"><tbody id="experiment-scope-body"></tbody></table>
      </div>
    </div>

    <div class="card hidden" id="warnings-card">
      <div class="card-header">
        <span>Parser &amp; File Diagnostics</span>
      </div>
      <div class="card-body">
        <ul class="warn-list" id="warnings-list"></ul>
      </div>
    </div>
  </aside>
</main>

<script>
(function () {
  "use strict";

  const state = {
    autoFollow: true,
    selectedSwapSeq: null,
    lastData: null,
    renderedImageKey: null,
    pollTimer: null
  };

  const el = {
    badgeDir: document.getElementById("badge-dir"),
    badgeRun: document.getElementById("badge-run"),
    badgeMenu: document.getElementById("badge-menu"),
    badgeFollow: document.getElementById("badge-follow"),
    frameCounter: document.getElementById("frame-counter"),
    waitingBanner: document.getElementById("waiting-banner"),
    errorBanner: document.getElementById("error-banner"),
    btnFirst: document.getElementById("btn-first"),
    btnPrev: document.getElementById("btn-prev"),
    btnNext: document.getElementById("btn-next"),
    btnLatest: document.getElementById("btn-latest"),
    btnFollow: document.getElementById("btn-follow"),
    btnRefresh: document.getElementById("btn-refresh"),
    btnDownload: document.getElementById("btn-download"),
    frameImg: document.getElementById("frame-img"),
    emptyStage: document.getElementById("empty-stage"),
    metaFilename: document.getElementById("meta-filename"),
    metaSeq: document.getElementById("meta-seq"),
    metaIndex: document.getElementById("meta-index"),
    metaDraws: document.getElementById("meta-draws"),
    metaSize: document.getElementById("meta-size"),
    metaTime: document.getElementById("meta-time"),
    metaSha: document.getElementById("meta-sha"),
    metaSurface: document.getElementById("meta-surface"),
    traceStats: document.getElementById("trace-stats"),
    nativeScopeNote: document.getElementById("native-scope-note"),
    historyList: document.getElementById("history-list"),
    resultPresence: document.getElementById("result-presence"),
    menuVerbatimNote: document.getElementById("menu-verbatim-note"),
    bootStatusBody: document.getElementById("boot-status-body"),
    uiStateBody: document.getElementById("ui-state-body"),
    experimentScopeNote: document.getElementById("experiment-scope-note"),
    experimentScopeBody: document.getElementById("experiment-scope-body"),
    warningsCard: document.getElementById("warnings-card"),
    warningsList: document.getElementById("warnings-list")
  };

  function clearNode(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function formatValue(val) {
    if (val === null || val === undefined) return "null";
    if (typeof val === "object") {
      try { return JSON.stringify(val, null, 2); } catch (_) { return String(val); }
    }
    return String(val);
  }

  function renderKvTable(tbody, rows) {
    clearNode(tbody);
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 2;
      td.textContent = "—";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const [k, v] of rows) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      const td = document.createElement("td");
      th.textContent = String(k);
      td.textContent = formatValue(v);
      tr.appendChild(th);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  function getReadyFrames() {
    if (!state.lastData || !state.lastData.native_frames) return [];
    return state.lastData.native_frames.frames || [];
  }

  function getSelectedIndex(readyFrames) {
    if (!readyFrames.length) return -1;
    if (state.autoFollow) {
      state.selectedSwapSeq = readyFrames[readyFrames.length - 1].swap_seq;
      return readyFrames.length - 1;
    }
    if (state.selectedSwapSeq !== null) {
      const idx = readyFrames.findIndex((f) => f.swap_seq === state.selectedSwapSeq);
      if (idx >= 0) return idx;
    }
    state.selectedSwapSeq = readyFrames[readyFrames.length - 1].swap_seq;
    return readyFrames.length - 1;
  }

  function setFollow(enabled) {
    state.autoFollow = enabled;
    el.badgeFollow.textContent = enabled ? "auto-follow: ON" : "auto-follow: PAUSED";
    el.badgeFollow.className = enabled ? "badge ok" : "badge warn";
    el.btnFollow.textContent = enabled ? "Pause Auto-Follow" : "Resume Auto-Follow";
    el.btnFollow.className = enabled ? "" : "active";
    if (state.lastData) render(state.lastData);
  }

  function selectByIndex(idx) {
    const readyFrames = getReadyFrames();
    if (!readyFrames.length) return;
    const clamped = Math.max(0, Math.min(readyFrames.length - 1, idx));
    state.selectedSwapSeq = readyFrames[clamped].swap_seq;
    if (clamped < readyFrames.length - 1 && state.autoFollow) {
      setFollow(false);
    } else if (state.lastData) {
      render(state.lastData);
    }
  }

  function render(data) {
    state.lastData = data;
    const nf = data.native_frames || {};
    const bs = data.boot_status || {};
    const es = data.experiment_scope || {};
    const readyFrames = nf.frames || [];
    const allSwaps = nf.all_swap_records || [];
    const selIdx = getSelectedIndex(readyFrames);
    const current = selIdx >= 0 ? readyFrames[selIdx] : null;

    el.badgeDir.textContent = "dir: " + (data.viewer ? data.viewer.result_dir_name : "?");
    el.badgeRun.textContent = "state: " + (bs.run_state || "unknown");
    el.badgeRun.className = "badge " + (
      bs.run_state === "failed" ? "err" :
      bs.run_state === "completed" ? "ok" : "warn"
    );

    const mr = bs.menu_rendered;
    el.badgeMenu.textContent = "menu_rendered: " + (mr === null || mr === undefined ? "pending" : String(mr));
    el.badgeMenu.className = "badge " + (mr === true ? "ok" : mr === false ? "warn" : "");

    if (nf.waiting_for_next_complete && nf.waiting_reason) {
      el.waitingBanner.textContent = nf.waiting_reason;
      el.waitingBanner.classList.remove("hidden");
    } else {
      el.waitingBanner.classList.add("hidden");
    }

    if (bs.graphics_error || (typeof bs.qemu_exit_code === "number" && bs.qemu_exit_code !== 0)) {
      const parts = [];
      if (typeof bs.qemu_exit_code === "number" && bs.qemu_exit_code !== 0) {
        parts.push("qemu_exit_code=" + bs.qemu_exit_code);
      }
      if (bs.stop_reason) parts.push("stop_reason=" + bs.stop_reason);
      if (bs.graphics_error) parts.push("graphics_error=" + formatValue(bs.graphics_error));
      el.errorBanner.textContent = "Run reported error: " + parts.join(" | ");
      el.errorBanner.classList.remove("hidden");
    } else {
      el.errorBanner.classList.add("hidden");
    }

    el.frameCounter.textContent = current
      ? ("Showing #" + (selIdx + 1) + " of " + readyFrames.length + " complete swap(s)")
      : ("0 complete / " + allSwaps.length + " swap record(s)");

    el.btnFirst.disabled = !current || selIdx <= 0;
    el.btnPrev.disabled = !current || selIdx <= 0;
    el.btnNext.disabled = !current || selIdx >= readyFrames.length - 1;
    el.btnLatest.disabled = !current || selIdx >= readyFrames.length - 1;

    if (current && typeof current.image_url === "string" && current.image_url.startsWith("/api/frames/")) {
      const imgKey = current.filename + ":" + (current.png_sha256 || "");
      if (state.renderedImageKey !== imgKey) {
        state.renderedImageKey = imgKey;
        el.frameImg.src = current.image_url + "?v=" + encodeURIComponent(current.png_sha256 || String(current.swap_seq));
      }
      el.frameImg.classList.remove("hidden");
      el.emptyStage.classList.add("hidden");

      if (typeof current.download_url === "string" && current.download_url.startsWith("/api/frames/")) {
        el.btnDownload.href = current.download_url;
        el.btnDownload.setAttribute("download", current.filename || "frame.png");
        el.btnDownload.classList.remove("disabled");
      }

      el.metaFilename.textContent = current.filename || "—";
      el.metaSeq.textContent = "#" + current.swap_seq + " / frame-" + (current.frame_number !== null ? current.frame_number : "?");
      el.metaIndex.textContent = current.trace_index !== null && current.trace_index !== undefined ? String(current.trace_index) : "—";
      el.metaDraws.textContent = current.draw_calls !== null && current.draw_calls !== undefined ? String(current.draw_calls) : "—";
      el.metaSize.textContent = (current.width && current.height ? (current.width + "x" + current.height + " • ") : "") + (current.png_bytes !== null ? (current.png_bytes + " B") : "—");
      el.metaTime.textContent = (current.timestamp || "—") + (current.timestamp_source ? (" (" + current.timestamp_source + ")") : "");
      el.metaSha.textContent = current.png_sha256 ? current.png_sha256.slice(0, 16) : "—";
      el.metaSurface.textContent = current.surface || "—";
    } else {
      state.renderedImageKey = null;
      el.frameImg.classList.add("hidden");
      el.frameImg.removeAttribute("src");
      el.emptyStage.classList.remove("hidden");
      el.emptyStage.textContent = nf.waiting_reason || "Waiting for first complete software_swap PNG...";
      el.btnDownload.href = "#";
      el.btnDownload.classList.add("disabled");
      el.metaFilename.textContent = "—";
      el.metaSeq.textContent = "—";
      el.metaIndex.textContent = "—";
      el.metaDraws.textContent = "—";
      el.metaSize.textContent = "—";
      el.metaTime.textContent = "—";
      el.metaSha.textContent = "—";
      el.metaSurface.textContent = "—";
    }

    el.nativeScopeNote.textContent = nf.scope_note || "";
    el.traceStats.textContent = "trace records: " + (nf.trace_records_parsed || 0) + " | swaps: " + (nf.ready_swap_count || 0) + "/" + (nf.total_swap_records || 0);

    clearNode(el.historyList);
    if (!allSwaps.length) {
      const emptyRow = document.createElement("div");
      emptyRow.className = "scope-note";
      emptyRow.textContent = "No software_swap records yet.";
      el.historyList.appendChild(emptyRow);
    } else {
      for (const item of allSwaps) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "history-row" +
          (current && item.swap_seq === current.swap_seq ? " selected" : "") +
          (!item.ready ? " pending" : "");
        btn.disabled = !item.ready;

        const left = document.createElement("span");
        left.textContent = "#" + item.swap_seq + " " + (item.filename || "<invalid>") +
          " | idx=" + (item.trace_index !== null && item.trace_index !== undefined ? item.trace_index : "?") +
          " | draws=" + (item.draw_calls !== null && item.draw_calls !== undefined ? item.draw_calls : "?");
        const right = document.createElement("span");
        right.textContent = item.ready
          ? ((item.png_bytes || 0) + " B")
          : ("[" + item.png_status + "]");

        btn.appendChild(left);
        btn.appendChild(right);
        if (item.ready) {
          btn.addEventListener("click", function () {
            const idx = readyFrames.findIndex((f) => f.swap_seq === item.swap_seq);
            if (idx >= 0) selectByIndex(idx);
          });
        }
        el.historyList.appendChild(btn);
      }
    }

    el.resultPresence.textContent = bs.result_present ? "result.json loaded" : ("result.json: " + (bs.result_parse_status || "missing"));
    el.menuVerbatimNote.textContent = bs.menu_rendered_verbatim_note || "";
    renderKvTable(el.bootStatusBody, [
      ["run_state", bs.run_state],
      ["menu_rendered", bs.menu_rendered],
      ["stop_reason", bs.stop_reason],
      ["qemu_exit_code", bs.qemu_exit_code],
      ["graphics_error", bs.graphics_error],
      ["result_graphics_draws", bs.result_graphics_draws],
      ["result_graphics_swaps", bs.result_graphics_swaps],
      ["unswapped_incomplete_surfaces", nf.unswapped_incomplete_surfaces]
    ]);

    const uiObj = bs.original_ui_state_at_stop;
    renderKvTable(el.uiStateBody, uiObj ? Object.entries(uiObj) : []);

    el.experimentScopeNote.textContent = es.note || "";
    const esItems = es.items || {};
    renderKvTable(el.experimentScopeBody, Object.entries(esItems));

    const warnings = data.warnings || [];
    clearNode(el.warningsList);
    if (warnings.length) {
      el.warningsCard.classList.remove("hidden");
      for (const w of warnings) {
        const li = document.createElement("li");
        li.textContent = String(w);
        el.warningsList.appendChild(li);
      }
    } else {
      el.warningsCard.classList.add("hidden");
    }
  }

  async function pollState() {
    try {
      const resp = await fetch("/api/state", { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      render(data);
    } catch (_) {
      // Ignore transient local polling errors.
    }
  }

  el.btnFirst.addEventListener("click", function () { selectByIndex(0); });
  el.btnPrev.addEventListener("click", function () {
    const rf = getReadyFrames();
    selectByIndex(getSelectedIndex(rf) - 1);
  });
  el.btnNext.addEventListener("click", function () {
    const rf = getReadyFrames();
    selectByIndex(getSelectedIndex(rf) + 1);
  });
  el.btnLatest.addEventListener("click", function () {
    const rf = getReadyFrames();
    state.selectedSwapSeq = rf.length ? rf[rf.length - 1].swap_seq : null;
    setFollow(true);
  });
  el.btnFollow.addEventListener("click", function () { setFollow(!state.autoFollow); });
  el.btnRefresh.addEventListener("click", function () { pollState(); });

  document.addEventListener("keydown", function (ev) {
    if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA")) return;
    if (ev.key === "ArrowLeft") {
      const rf = getReadyFrames();
      selectByIndex(getSelectedIndex(rf) - 1);
    } else if (ev.key === "ArrowRight") {
      const rf = getReadyFrames();
      selectByIndex(getSelectedIndex(rf) + 1);
    } else if (ev.key === " ") {
      ev.preventDefault();
      setFollow(!state.autoFollow);
    }
  });

  pollState();
  state.pollTimer = window.setInterval(pollState, 1000);
})();
</script>
</body>
</html>
"""

INDEX_HTML_BYTES = INDEX_HTML.encode("utf-8")


def _contains_traversal(raw_path: str) -> bool:
    """Detect path traversal attempts in both raw and percent-decoded forms."""
    raw_only = raw_path.split("?", 1)[0].split("#", 1)[0]
    lowered = raw_only.lower()
    if any(tok in lowered for tok in ("..", "%2e", "%2f", "%5c", "\\", "\x00")):
        return True
    decoded = urllib.parse.unquote(raw_only)
    if any(tok in decoded for tok in ("..", "\\", "\x00", "//")):
        return True
    return False


class FrameViewerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        scanner: ResultDirScanner,
    ) -> None:
        self.scanner = scanner
        super().__init__(server_address, FrameViewerRequestHandler)


class FrameViewerRequestHandler(BaseHTTPRequestHandler):
    server: FrameViewerHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy per-poll access logs by default unless verbose is enabled.
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def _send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, body: bytes, head_only: bool = False) -> None:
        self.send_response(int(status))
        self._send_security_headers()
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_png(
        self,
        data: bytes,
        filename: str,
        as_attachment: bool = False,
        head_only: bool = False,
    ) -> None:
        self.send_response(int(HTTPStatus.OK))
        self._send_security_headers()
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        if as_attachment:
            safe_name = html.escape(filename, quote=True)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _handle_read(self, head_only: bool = False) -> None:
        raw_target = self.path
        if _contains_traversal(raw_target):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "Path traversal or forbidden encoding rejected"},
                head_only=head_only,
            )
            return

        parsed = urllib.parse.urlsplit(raw_target)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(HTTPStatus.OK, INDEX_HTML_BYTES, head_only=head_only)
            return

        if path == "/api/state":
            snapshot = self.server.scanner.get_state()
            self._send_json(HTTPStatus.OK, snapshot, head_only=head_only)
            return

        if path.startswith("/api/frames/"):
            rest = path[len("/api/frames/") :]
            if not rest:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing frame filename"}, head_only=head_only)
                return

            parts = rest.split("/")
            if len(parts) == 1:
                filename = parts[0]
                as_attachment = False
            elif len(parts) == 2 and parts[1] == "download":
                filename = parts[0]
                as_attachment = True
            else:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid frame path"}, head_only=head_only)
                return

            if not FRAME_FILENAME_RE.match(filename):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid frame filename"}, head_only=head_only)
                return

            data, status, msg = self.server.scanner.get_frame_bytes(filename)
            if status != HTTPStatus.OK or data is None:
                self._send_json(status, {"error": msg}, head_only=head_only)
                return

            self._send_png(data, filename, as_attachment=as_attachment, head_only=head_only)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}, head_only=head_only)

    def do_GET(self) -> None:
        self._handle_read(head_only=False)

    def do_HEAD(self) -> None:
        self._handle_read(head_only=True)

    def _reject_write(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Viewer is strictly read-only"})

    def do_POST(self) -> None:
        self._reject_write()

    def do_PUT(self) -> None:
        self._reject_write()

    def do_DELETE(self) -> None:
        self._reject_write()

    def do_PATCH(self) -> None:
        self._reject_write()


def create_server(
    result_dir: Path | str,
    host: str = "127.0.0.1",
    port: int = 0,
    verbose: bool = False,
) -> tuple[FrameViewerHTTPServer, str]:
    """Create a loopback-only read-only frame viewer HTTP server."""
    safe_host = validate_loopback_host(host)
    if not (0 <= port <= 65535):
        raise ValueError(f"Port must be in 0..65535, got {port}")
    dir_path = Path(result_dir)
    if dir_path.exists() and not dir_path.is_dir():
        raise ValueError(f"--result-dir is not a directory: {dir_path}")
    scanner = ResultDirScanner(dir_path)
    server = FrameViewerHTTPServer((safe_host, port), scanner)
    server.verbose = verbose
    bound_host, bound_port = server.server_address[:2]
    url_host = f"[{bound_host}]" if ":" in bound_host else bound_host
    url = f"http://{url_host}:{bound_port}/"
    return server, url


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only local native swap frame viewer for Panasonic LUMIX S5 QEMU result directories."
        )
    )
    parser.add_argument(
        "--result-dir",
        required=True,
        help="Path to a running or completed boot.py result directory (opened strictly read-only).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback bind host (default: 127.0.0.1; non-loopback hosts are rejected).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port to bind (default: 0 for random free loopback port).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log HTTP requests to stderr.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        server, url = create_server(
            result_dir=args.result_dir,
            host=args.host,
            port=args.port,
            verbose=args.verbose,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: failed to bind viewer server: {exc}", file=sys.stderr)
        return 1

    stop_event = threading.Event()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    print(f"S5 Native Frame Viewer (read-only) listening on {url}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
