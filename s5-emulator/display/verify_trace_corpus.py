#!/usr/bin/env python3
"""Batch replay & regression verifier for captured S5 OpenVG graphics traces.

Replays real captured host-renderer call traces into an isolated temporary/work
directory without touching source trace directories, without launching QEMU, and
without inventing any firmware UI data.

Scope disclaimer:
- Replay verifies OpenVGBackend fidelity on previously captured calls only.
- It does NOT prove new firmware execution, QEMU boot success, or menu opening.
- Real swap frames, intermediate/incomplete surfaces, and synthetic self-test
  images are strictly separated.
- Traces that stopped mid-run at an unsupported call are honestly classified as
  `completed_prefix_only` (NEVER `completed_full`).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import tempfile

from PIL import Image, ImageChops

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from openvg_backend import OpenVGBackend, UnsupportedVG  # noqa: E402


DEFAULT_CORPUS = (
    {
        "name": "local-frame-query",
        "relative_dir": "menu-smp/results/local-frame-query/graphics",
        "expected_completion_status": "completed_prefix_only",
        "targeted_roi_check": None,
    },
    {
        "name": "local-shared-image-stride",
        "relative_dir": "menu-smp/results/local-shared-image-stride/graphics",
        "expected_completion_status": "completed_full",
        "targeted_roi_check": "shared_image_stride_two_frame",
    },
    {
        "name": "local-requested-lcd-refresh",
        "relative_dir": "menu-smp/results/local-requested-lcd-refresh/graphics",
        "expected_completion_status": "completed_full",
        "targeted_roi_check": None,
    },
)

# Targeted regression constants from display/render-wait-analysis/two-frame-pixel-verification.json
SHARED_IMAGE_STRIDE_CENTER_ROI = (160, 250, 800, 390)
SHARED_IMAGE_STRIDE_BATTERY_BBOX = (866, 21, 929, 59)
SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED = 9696

KNOWN_EVENTS = frozenset(
    {
        "guest_api_entry",
        "guest_api_return",
        "unsupported_or_invalid_call",
        "original_context_resource_requests",
        "observed_native_window",
        "create_surface",
        "original_linear_buffer_mode",
        "make_current",
        "set_state",
        "vendor_resource_configuration",
        "vendor_yuv_configuration",
        "vendor_yuv_coefficients",
        "set_matrix",
        "paint_parameter",
        "destroy_paint",
        "clear",
        "draw_captured_path",
        "transform_path",
        "captured_image",
        "original_standard_image_layout",
        "draw_captured_image",
        "bind_client_image_surface",
        "destroy_surface",
        "destroy_image",
        "original_frame_mode",
        "original_frame_status_read_clear",
        "software_swap",
    }
)

SAFE_IMAGE_NAME_RE = re.compile(r"^image-[0-9a-fA-F]+\.bin$")
SAFE_TRANSFORM_NAME_RE = re.compile(r"^transform-path-([0-9a-f]{64})\.json$")
SAFE_FRAME_NAME_RE = re.compile(r"^frame-(\d{4,})\.png$")
INCOMPLETE_SURFACE_RE = re.compile(r"^incomplete-surface-[0-9a-fA-F]+\.png$")
SYNTHETIC_PNG_RE = re.compile(r"(gradient-test|selftest|self-test|synthetic|test)", re.IGNORECASE)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_int_handle(value) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Boolean is not a valid handle: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise ValueError(f"Unsupported handle value: {value!r}")


def _contains_traversal(raw_path: str) -> bool:
    if "\x00" in raw_path:
        return True
    posix_parts = PurePosixPath(raw_path).parts
    win_parts = PureWindowsPath(raw_path).parts
    return ".." in posix_parts or ".." in win_parts


def resolve_trace_artifact_path(
    trace_dir: Path,
    raw_path,
    expected_name_re: re.Pattern,
    kind: str,
    event_index: int,
    line_number: int,
):
    """Resolve an artifact path strictly within `trace_dir`.

    Supports relocated captures (where calls.jsonl recorded an absolute path
    from the original capture machine) ONLY when:
    - no path traversal (`..` or NUL) appears anywhere in `raw_path`,
    - the basename matches `expected_name_re` (preventing arbitrary file reads
      like `/etc/passwd`), AND
    - the resolved target stays strictly inside `trace_dir.resolve()`.

    Returns `(resolved_path, meta_dict, error_dict_or_None)`.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, None, {
            "code": "invalid_path",
            "kind": kind,
            "event_index": event_index,
            "line": line_number,
            "recorded_path": raw_path,
            "message": f"{kind} path must be a non-empty string",
        }

    if _contains_traversal(raw_path):
        return None, None, {
            "code": "path_traversal",
            "kind": kind,
            "event_index": event_index,
            "line": line_number,
            "recorded_path": raw_path,
            "message": f"Rejected path traversal / NUL in {kind} path: {raw_path!r}",
        }

    trace_root = trace_dir.resolve()
    recorded = Path(raw_path)
    basename = PurePosixPath(raw_path.replace("\\", "/")).name
    relocated = False

    if recorded.is_absolute():
        # If the absolute path already points inside trace_root, keep it.
        # Otherwise only allow safe relocation by basename when basename matches
        # the expected artifact filename pattern.
        try:
            direct_resolved = recorded.resolve(strict=False)
        except OSError as exc:
            return None, None, {
                "code": "invalid_path",
                "kind": kind,
                "event_index": event_index,
                "line": line_number,
                "recorded_path": raw_path,
                "message": f"Cannot resolve {kind} path {raw_path!r}: {exc}",
            }
        if direct_resolved.is_relative_to(trace_root):
            candidate = recorded
        else:
            if not expected_name_re.match(basename):
                return None, None, {
                    "code": "invalid_path",
                    "kind": kind,
                    "event_index": event_index,
                    "line": line_number,
                    "recorded_path": raw_path,
                    "message": (
                        f"Absolute {kind} path outside trace directory with "
                        f"unexpected filename {basename!r}: {raw_path!r}"
                    ),
                }
            candidate = trace_dir / basename
            relocated = True
    else:
        if not expected_name_re.match(basename):
            return None, None, {
                "code": "invalid_path",
                "kind": kind,
                "event_index": event_index,
                "line": line_number,
                "recorded_path": raw_path,
                "message": f"Unexpected {kind} filename {basename!r} in {raw_path!r}",
            }
        candidate = trace_dir / recorded

    # Check symlink/canonical containment strictly inside trace_root.
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        return None, None, {
            "code": "invalid_path",
            "kind": kind,
            "event_index": event_index,
            "line": line_number,
            "recorded_path": raw_path,
            "message": f"Failed resolving {kind} path {candidate}: {exc}",
        }

    if not resolved.is_relative_to(trace_root):
        return None, None, {
            "code": "path_traversal",
            "kind": kind,
            "event_index": event_index,
            "line": line_number,
            "recorded_path": raw_path,
            "resolved_path": str(resolved),
            "message": f"Resolved {kind} path escapes trace directory: {resolved}",
        }

    meta = {
        "recorded_path": raw_path,
        "resolved_path": str(resolved),
        "basename": basename,
        "relocated_to_trace_dir": relocated,
    }

    if not resolved.exists():
        return None, meta, {
            "code": "missing_dependency",
            "kind": kind,
            "event_index": event_index,
            "line": line_number,
            "recorded_path": raw_path,
            "expected_path": str(resolved),
            "message": f"Missing {kind} file: {resolved}",
        }

    if not resolved.is_file():
        return None, meta, {
            "code": "invalid_path",
            "kind": kind,
            "event_index": event_index,
            "line": line_number,
            "recorded_path": raw_path,
            "resolved_path": str(resolved),
            "message": f"{kind} path is not a regular file: {resolved}",
        }

    return resolved, meta, None


def compare_rgba_images(ref_img: Image.Image, rep_img: Image.Image) -> dict:
    """Compare two PIL images by dimensions and decoded RGBA pixels."""
    ref_rgba = ref_img.convert("RGBA")
    rep_rgba = rep_img.convert("RGBA")
    dimensions_match = ref_rgba.size == rep_rgba.size
    if not dimensions_match:
        return {
            "dimensions_match": False,
            "reference_size": list(ref_rgba.size),
            "replayed_size": list(rep_rgba.size),
            "rgba_pixels_identical": False,
            "changed_pixel_count": None,
            "diff_bbox": None,
        }

    diff = ImageChops.difference(ref_rgba, rep_rgba)
    r, g, b, a = diff.split()
    # Combine all four RGBA channels into an L-mode image so getbbox() never
    # ignores RGB-only differences where alpha diff is zero (Pillow's RGBA
    # getbbox defaults to alpha_only=True).
    any_diff = ImageChops.lighter(ImageChops.lighter(r, g), ImageChops.lighter(b, a))
    bbox = any_diff.getbbox()
    if bbox is None:
        changed_pixels = 0
    else:
        mask = any_diff.point(lambda v: 255 if v else 0)
        changed_pixels = int(mask.histogram()[255])

    return {
        "dimensions_match": True,
        "reference_size": list(ref_rgba.size),
        "replayed_size": list(rep_rgba.size),
        "rgba_pixels_identical": changed_pixels == 0,
        "changed_pixel_count": changed_pixels,
        "diff_bbox": list(bbox) if bbox is not None else None,
    }


def count_bright_pixels(img: Image.Image, roi: tuple[int, int, int, int]) -> int:
    region = img.convert("RGBA").crop(roi)
    return sum(1 for p in region.getdata() if p[0] > 100 and p[3] > 100)


def check_shared_image_stride_roi(frame0_path: Path, frame1_path: Path, label: str) -> dict:
    """Targeted two-frame ROI regression check for local-shared-image-stride.

    Verifies against display/render-wait-analysis/two-frame-pixel-verification.json:
    - Center ROI (160, 250)-(800, 390) is 100% pixel-identical between frame 0 and 1.
    - All whole-image differences lie strictly within the battery icon region
      (866, 21)-(929, 59), and at least one pixel in that region changed.
    - Center bright pixel count matches 9696 on each frame.
    """
    img0 = Image.open(frame0_path).convert("RGBA")
    img1 = Image.open(frame1_path).convert("RGBA")

    full_cmp = compare_rgba_images(img0, img1)
    roi_cmp = compare_rgba_images(
        img0.crop(SHARED_IMAGE_STRIDE_CENTER_ROI),
        img1.crop(SHARED_IMAGE_STRIDE_CENTER_ROI),
    )

    bright0 = count_bright_pixels(img0, SHARED_IMAGE_STRIDE_CENTER_ROI)
    bright1 = count_bright_pixels(img1, SHARED_IMAGE_STRIDE_CENTER_ROI)

    bbox = full_cmp["diff_bbox"]
    bx0, by0, bx1, by1 = SHARED_IMAGE_STRIDE_BATTERY_BBOX
    diff_non_empty = bbox is not None and (full_cmp["changed_pixel_count"] or 0) > 0
    diff_within_battery_region = bool(
        diff_non_empty
        and bbox[0] >= bx0
        and bbox[1] >= by0
        and bbox[2] <= bx1
        and bbox[3] <= by1
    )
    diff_bbox_exact_match = bbox == list(SHARED_IMAGE_STRIDE_BATTERY_BBOX)
    center_identical = bool(roi_cmp["dimensions_match"] and roi_cmp["rgba_pixels_identical"])
    bright_match = (
        bright0 == SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED
        and bright1 == SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED
    )

    failures = []
    if not full_cmp["dimensions_match"]:
        failures.append(f"{label}: frame dimensions differ ({full_cmp['reference_size']} vs {full_cmp['replayed_size']})")
    if not center_identical:
        failures.append(
            f"{label}: center ROI {list(SHARED_IMAGE_STRIDE_CENTER_ROI)} is NOT identical "
            f"(changed_pixels={roi_cmp['changed_pixel_count']}, roi_diff_bbox={roi_cmp['diff_bbox']})"
        )
    if not diff_non_empty:
        failures.append(f"{label}: whole-image diff between frame 0 and frame 1 is unexpectedly empty")
    elif not diff_within_battery_region:
        failures.append(
            f"{label}: whole-image diff_bbox {bbox} extends outside battery region "
            f"{list(SHARED_IMAGE_STRIDE_BATTERY_BBOX)}"
        )
    if not bright_match:
        failures.append(
            f"{label}: center bright pixel counts {[bright0, bright1]} != "
            f"[{SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED}, {SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED}]"
        )

    return {
        "label": label,
        "frames": [str(frame0_path), str(frame1_path)],
        "center_roi": list(SHARED_IMAGE_STRIDE_CENTER_ROI),
        "expected_battery_region_bbox": list(SHARED_IMAGE_STRIDE_BATTERY_BBOX),
        "center_pixels_identical": center_identical,
        "center_changed_pixel_count": roi_cmp["changed_pixel_count"],
        "center_diff_bbox": roi_cmp["diff_bbox"],
        "whole_image_changed_pixel_count": full_cmp["changed_pixel_count"],
        "whole_image_diff_bbox": bbox,
        "diff_within_battery_region": diff_within_battery_region,
        "diff_bbox_exact_match": diff_bbox_exact_match,
        "center_bright_pixels_each": [bright0, bright1],
        "expected_center_bright_pixels_each": [
            SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED,
            SHARED_IMAGE_STRIDE_CENTER_BRIGHT_EXPECTED,
        ],
        "passed": len(failures) == 0,
        "failures": failures,
    }


def replay_transform_path_geometry(capture: dict, scratch_parent: Path) -> dict:
    """Re-execute OpenVGBackend.transform_path on captured inputs and compare."""
    required_keys = (
        "source",
        "matrix_path_user_to_surface",
        "destination_before",
        "appended_segments",
        "appended_coordinates",
    )
    for key in required_keys:
        if key not in capture:
            raise ValueError(f"transform-path JSON missing required key {key!r}")

    src_spec = capture["source"]
    dst_spec = capture["destination_before"]
    matrix = capture["matrix_path_user_to_surface"]
    if not isinstance(matrix, list) or len(matrix) != 9 or not all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
        for x in matrix
    ):
        raise ValueError("transform-path JSON has invalid 3x3 matrix_path_user_to_surface")

    with tempfile.TemporaryDirectory(prefix="s5-tp-replay-", dir=str(scratch_parent)) as td:
        tb = OpenVGBackend(Path(td))
        try:
            sh = tb.create_path(
                src_spec["datatype"],
                src_spec["scale"],
                src_spec["bias"],
                src_spec["capabilities"],
            )
            tb.paths[sh]["segments"] = list(src_spec["segments"])
            tb.paths[sh]["coords"] = list(src_spec["coords"])

            same_handle = src_spec == dst_spec and capture.get("source_unchanged") is False
            if same_handle:
                dh = sh
            else:
                dh = tb.create_path(
                    dst_spec["datatype"],
                    dst_spec["scale"],
                    dst_spec["bias"],
                    dst_spec["capabilities"],
                )
                tb.paths[dh]["segments"] = list(dst_spec["segments"])
                tb.paths[dh]["coords"] = list(dst_spec["coords"])

            tb.matrices[0x1400] = list(matrix)
            tb.transform_path(dh, sh)
            tp_events = [e for e in tb.events if e["name"] == "transform_path"]
            if not tp_events:
                raise ValueError(
                    f"transform_path produced no event (backend error={hex(tb.error)})"
                )
            tp_ev = tp_events[-1]
            replayed_cap = json.loads(Path(tp_ev["captured_geometry"]).read_text())
            return {
                "replayed_sha256": tp_ev["sha256"],
                "segments_match": replayed_cap["appended_segments"] == capture["appended_segments"],
                "coordinates_match": replayed_cap["appended_coordinates"] == capture["appended_coordinates"],
                "replayed_segments_count": len(replayed_cap["appended_segments"]),
                "replayed_coordinates_count": len(replayed_cap["appended_coordinates"]),
            }
        finally:
            tb.close()


def inventory_directory_pngs(trace_dir: Path, swap_basenames: set[str], is_synthetic_trace: bool) -> dict:
    """Strictly separate PNG files in `trace_dir` by provenance category."""
    real_swap_files = []
    incomplete_surface_files = []
    synthetic_selftest_files = []
    unclassified_pngs = []

    for png in sorted(trace_dir.glob("*.png")):
        if not png.is_file():
            continue
        size = None
        read_error = None
        try:
            with Image.open(png) as im:
                size = list(im.size)
        except Exception as exc:
            read_error = str(exc)

        entry = {
            "filename": png.name,
            "path": str(png.resolve()),
            "size_bytes": png.stat().st_size,
            "dimensions": size,
            "sha256": sha256_file(png),
            "read_error": read_error,
        }
        if INCOMPLETE_SURFACE_RE.match(png.name):
            entry["category"] = "incomplete_or_intermediate_surface"
            entry["is_real_swap_frame"] = False
            entry["note"] = "Diagnostic surface dump at stop; NOT an EGL-presented swap frame."
            incomplete_surface_files.append(entry)
        elif SYNTHETIC_PNG_RE.search(png.name) or (is_synthetic_trace and png.name in swap_basenames):
            entry["category"] = "synthetic_selftest_image"
            entry["is_real_swap_frame"] = False
            entry["note"] = "Synthetic/self-test image; NOT captured firmware UI."
            synthetic_selftest_files.append(entry)
        elif png.name in swap_basenames:
            entry["category"] = "real_swap_frame"
            entry["is_real_swap_frame"] = True
            real_swap_files.append(entry)
        else:
            entry["category"] = "unclassified_png"
            entry["is_real_swap_frame"] = False
            unclassified_pngs.append(entry)

    return {
        "real_swap_frames": real_swap_files,
        "intermediate_and_incomplete_surfaces": incomplete_surface_files,
        "synthetic_selftest_images": synthetic_selftest_files,
        "unclassified_pngs": unclassified_pngs,
    }


def _default_tmp_root() -> Path:
    env_tmp = os.environ.get("TMPDIR")
    if env_tmp:
        p = Path(env_tmp)
        p.mkdir(parents=True, exist_ok=True)
        return p
    sata_tmp = REPO_ROOT.parent.parent / "tmp"
    if sata_tmp.parent.exists():
        sata_tmp.mkdir(parents=True, exist_ok=True)
        return sata_tmp
    return Path(tempfile.gettempdir())


def verify_trace(
    trace_path: Path,
    workdir: Path,
    *,
    name: str | None = None,
    expected_completion_status: str | None = None,
    targeted_roi_check: str | None = None,
    require_full: bool = False,
) -> dict:
    """Verify a single graphics trace directory in isolation."""
    trace_path = Path(trace_path)
    if trace_path.is_file() and trace_path.name == "calls.jsonl":
        trace_dir = trace_path.parent
    else:
        trace_dir = trace_path

    trace_name = name or (
        trace_dir.parent.name if trace_dir.name == "graphics" and trace_dir.parent.name else trace_dir.name
    )

    errors: list[dict] = []
    warnings: list[dict] = []
    image_dependencies: list[dict] = []
    transform_dependencies: list[dict] = []
    path_draw_checks: list[dict] = []
    swap_frames: list[dict] = []
    surfaces_summary = {
        "window_or_pbuffer_surfaces": [],
        "client_image_surfaces": [],
    }
    unsupported_stop = None
    event_counts: dict[str, int] = {}

    trace_dir_resolved = trace_dir.resolve()
    workdir_resolved = workdir.resolve()
    if workdir_resolved == trace_dir_resolved or trace_dir_resolved.is_relative_to(workdir_resolved) or workdir_resolved.is_relative_to(trace_dir_resolved):
        errors.append(
            {
                "code": "unsafe_workdir",
                "message": f"Replay workdir {workdir_resolved} must not overlap source trace dir {trace_dir_resolved}",
            }
        )
        return {
            "name": trace_name,
            "trace_dir": str(trace_dir_resolved),
            "replay_workdir": str(workdir_resolved),
            "completion_status": "failed",
            "full_run_succeeded": False,
            "prefix_verified": False,
            "passed": False,
            "errors": errors,
        }

    calls_file = trace_dir / "calls.jsonl"
    if not trace_dir.is_dir():
        errors.append({"code": "missing_trace_dir", "message": f"Trace directory does not exist: {trace_dir}"})
        return {
            "name": trace_name,
            "trace_dir": str(trace_dir),
            "replay_workdir": str(workdir_resolved),
            "completion_status": "failed",
            "full_run_succeeded": False,
            "prefix_verified": False,
            "passed": False,
            "errors": errors,
        }
    if not calls_file.is_file():
        errors.append({"code": "missing_calls_jsonl", "message": f"Missing calls.jsonl: {calls_file}"})
        return {
            "name": trace_name,
            "trace_dir": str(trace_dir_resolved),
            "replay_workdir": str(workdir_resolved),
            "completion_status": "failed",
            "full_run_succeeded": False,
            "prefix_verified": False,
            "passed": False,
            "errors": errors,
        }

    calls_bytes = calls_file.read_bytes()
    calls_sha256 = sha256_bytes(calls_bytes)
    calls_text = None
    try:
        calls_text = calls_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(
            {
                "code": "corrupt_calls_jsonl",
                "file": str(calls_file),
                "message": f"calls.jsonl is not valid UTF-8: {exc}",
            }
        )

    rows: list[tuple[int, dict]] = []
    if calls_text is not None:
        raw_lines = calls_text.splitlines()
        if not raw_lines:
            errors.append(
                {
                    "code": "empty_calls_jsonl",
                    "file": str(calls_file),
                    "message": "calls.jsonl contains zero lines",
                }
            )
        for lineno, raw_line in enumerate(raw_lines, start=1):
            if not raw_line.strip():
                errors.append(
                    {
                        "code": "corrupt_json_line",
                        "file": str(calls_file),
                        "line": lineno,
                        "message": f"Line {lineno} in calls.jsonl is empty/blank",
                    }
                )
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                errors.append(
                    {
                        "code": "corrupt_json_line",
                        "file": str(calls_file),
                        "line": lineno,
                        "column": exc.colno,
                        "snippet": raw_line[:160],
                        "message": f"Truncated or corrupt JSON at {calls_file.name}:{lineno}:{exc.colno}: {exc.msg}",
                    }
                )
                continue
            if not isinstance(obj, dict):
                errors.append(
                    {
                        "code": "invalid_event_object",
                        "file": str(calls_file),
                        "line": lineno,
                        "message": f"Line {lineno} JSON value is not an object",
                    }
                )
                continue
            if "name" not in obj or "index" not in obj:
                errors.append(
                    {
                        "code": "invalid_event_schema",
                        "file": str(calls_file),
                        "line": lineno,
                        "message": f"Line {lineno} missing required 'name' or 'index' field",
                    }
                )
                continue
            expected_index = lineno - 1
            if obj["index"] != expected_index:
                errors.append(
                    {
                        "code": "non_sequential_event_index",
                        "file": str(calls_file),
                        "line": lineno,
                        "expected_index": expected_index,
                        "actual_index": obj["index"],
                        "message": f"Line {lineno} has index {obj['index']}, expected {expected_index}",
                    }
                )
            rows.append((lineno, obj))

    # Replay into an isolated subdirectory under workdir.
    replay_out = workdir_resolved / "replay"
    replay_out.mkdir(parents=True, exist_ok=True)

    handles: dict[int, int] = {}
    pending_entry: dict | None = None
    captured_image_meta: dict[int, dict] = {}
    referenced_swap_basenames: set[str] = set()
    is_synthetic_trace = False
    swap_seq = 0
    seen_unsupported = False

    def map_handle(raw_val, kind: str, ev_idx: int, lineno: int) -> int:
        key = parse_int_handle(raw_val)
        if key not in handles:
            raise KeyError(
                f"Unknown {kind} handle {raw_val!r} (parsed {hex(key)}) at event {ev_idx} (line {lineno})"
            )
        return handles[key]

    backend = OpenVGBackend(replay_out)
    try:
        for lineno, row in rows:
            ev_name = row["name"]
            ev_idx = row["index"]
            event_counts[ev_name] = event_counts.get(ev_name, 0) + 1

            if seen_unsupported:
                errors.append(
                    {
                        "code": "events_after_unsupported_stop",
                        "event_index": ev_idx,
                        "line": lineno,
                        "event_name": ev_name,
                        "message": (
                            f"Unexpected event {ev_name!r} at index {ev_idx} (line {lineno}) "
                            "after unsupported_or_invalid_call"
                        ),
                    }
                )
                continue

            if ev_name not in KNOWN_EVENTS:
                errors.append(
                    {
                        "code": "unknown_event",
                        "event_index": ev_idx,
                        "line": lineno,
                        "event_name": ev_name,
                        "message": f"Unrecognized trace event {ev_name!r} at index {ev_idx} (line {lineno})",
                    }
                )
                continue

            try:
                if ev_name == "guest_api_entry":
                    if pending_entry is not None:
                        errors.append(
                            {
                                "code": "unclosed_guest_api_entry",
                                "event_index": ev_idx,
                                "line": lineno,
                                "previous_entry": {
                                    "index": pending_entry["index"],
                                    "api": pending_entry.get("api"),
                                },
                                "message": (
                                    f"guest_api_entry {pending_entry.get('api')!r} at index "
                                    f"{pending_entry['index']} had no matching return or unsupported stop"
                                ),
                            }
                        )
                    pending_entry = row

                elif ev_name == "guest_api_return":
                    api = row.get("api")
                    if pending_entry is None or pending_entry.get("api") != api:
                        errors.append(
                            {
                                "code": "mismatched_guest_api_return",
                                "event_index": ev_idx,
                                "line": lineno,
                                "api": api,
                                "pending_api": pending_entry.get("api") if pending_entry else None,
                                "message": (
                                    f"guest_api_return for {api!r} at index {ev_idx} (line {lineno}) "
                                    f"does not match pending entry {pending_entry.get('api') if pending_entry else None!r}"
                                ),
                            }
                        )
                        pending_entry = None
                        continue
                    args = pending_entry.get("r0_r3", [])
                    pending_entry = None
                    if api == "vgCreatePaint":
                        res_handle = parse_int_handle(row["result"])
                        handles[res_handle] = backend.create_paint()
                    elif api == "vgSetColor":
                        paint_h = map_handle(args[0], "paint", ev_idx, lineno)
                        if paint_h not in backend.paints:
                            backend.error = 0x1000
                        else:
                            backend.paints[paint_h][0x1a01] = [
                                (args[1] >> shift & 255) / 255.0 for shift in (24, 16, 8, 0)
                            ]
                    elif api == "vgSetPaint":
                        paint_h = map_handle(args[0], "paint", ev_idx, lineno) if args[0] else 0
                        backend.set_paint(paint_h, args[1])

                elif ev_name == "unsupported_or_invalid_call":
                    seen_unsupported = True
                    pending_entry = None
                    unsupported_stop = {
                        "event_index": ev_idx,
                        "line": lineno,
                        "api": row.get("api"),
                        "pc": row.get("pc"),
                        "lr": row.get("lr"),
                        "r0_r3": row.get("r0_r3"),
                        "exception": row.get("exception"),
                        "error": row.get("error"),
                        "draws_completed_before_stop": backend.draw_count,
                        "swaps_completed_before_stop": backend.swaps,
                    }

                elif ev_name == "create_surface":
                    origin = str(row.get("origin", ""))
                    if "synthetic" in origin.lower() or "self-test" in origin.lower():
                        is_synthetic_trace = True
                    rec_handle = parse_int_handle(row["handle"])
                    if origin == "original VGImage client buffer":
                        surfaces_summary["client_image_surfaces"].append(
                            {
                                "event_index": ev_idx,
                                "handle": row["handle"],
                                "width": row["width"],
                                "height": row["height"],
                                "origin": origin,
                                "is_real_swap_frame": False,
                            }
                        )
                    else:
                        new_h = backend.create_surface(row["width"], row["height"], origin)
                        handles[rec_handle] = new_h
                        surfaces_summary["window_or_pbuffer_surfaces"].append(
                            {
                                "event_index": ev_idx,
                                "handle": row["handle"],
                                "width": row["width"],
                                "height": row["height"],
                                "origin": origin,
                                "is_real_swap_frame": False,
                            }
                        )

                elif ev_name == "captured_image":
                    resolved, meta, path_err = resolve_trace_artifact_path(
                        trace_dir,
                        row.get("source_path"),
                        SAFE_IMAGE_NAME_RE,
                        "captured_image",
                        ev_idx,
                        lineno,
                    )
                    rec_sha = row.get("sha256")
                    if path_err is not None:
                        errors.append(path_err)
                        image_dependencies.append(
                            {
                                "event_index": ev_idx,
                                "line": lineno,
                                "handle": row.get("handle"),
                                "recorded_path": row.get("source_path"),
                                "recorded_sha256": rec_sha,
                                "actual_sha256": None,
                                "sha256_match": False,
                                "status": path_err["code"],
                            }
                        )
                        continue

                    raw_bytes = resolved.read_bytes()
                    actual_sha = sha256_bytes(raw_bytes)
                    sha_ok = bool(rec_sha) and actual_sha == rec_sha
                    dep_entry = {
                        "event_index": ev_idx,
                        "line": lineno,
                        "handle": row.get("handle"),
                        "format": row.get("format"),
                        "width": row.get("width"),
                        "height": row.get("height"),
                        "stride": row.get("stride"),
                        "size_bytes": len(raw_bytes),
                        **meta,
                        "recorded_sha256": rec_sha,
                        "actual_sha256": actual_sha,
                        "sha256_match": sha_ok,
                        "status": "ok" if sha_ok else "sha256_mismatch",
                    }
                    image_dependencies.append(dep_entry)
                    if not sha_ok:
                        errors.append(
                            {
                                "code": "dependency_sha256_mismatch",
                                "kind": "captured_image",
                                "event_index": ev_idx,
                                "line": lineno,
                                "handle": row.get("handle"),
                                "path": str(resolved),
                                "expected_sha256": rec_sha,
                                "actual_sha256": actual_sha,
                                "message": (
                                    f"Image dependency SHA-256 mismatch at index {ev_idx} (line {lineno}) "
                                    f"for {resolved.name}: expected {rec_sha}, got {actual_sha}"
                                ),
                            }
                        )

                    rec_handle = parse_int_handle(row["handle"])
                    new_h = backend.create_image(
                        raw_bytes,
                        parse_int_handle(row["format"]),
                        row["width"],
                        row["height"],
                        row.get("stride"),
                    )
                    handles[rec_handle] = new_h
                    captured_image_meta[rec_handle] = {
                        "width": row["width"],
                        "height": row["height"],
                        "stride": row.get("stride"),
                        "size_bytes": len(raw_bytes),
                    }

                elif ev_name == "original_standard_image_layout":
                    img_h = parse_int_handle(row["image"])
                    meta_info = captured_image_meta.get(img_h)
                    if meta_info is not None:
                        if (
                            meta_info["width"] != row.get("width")
                            or meta_info["height"] != row.get("height")
                            or meta_info["size_bytes"] != row.get("bytes")
                        ):
                            errors.append(
                                {
                                    "code": "image_layout_mismatch",
                                    "event_index": ev_idx,
                                    "line": lineno,
                                    "image": row["image"],
                                    "message": "original_standard_image_layout disagrees with captured_image",
                                }
                            )

                elif ev_name == "bind_client_image_surface":
                    rec_surf = parse_int_handle(row["surface"])
                    img_h = map_handle(row["image"], "image", ev_idx, lineno)
                    handles[rec_surf] = backend.create_image_surface(img_h)

                elif ev_name == "destroy_surface":
                    surf_h = map_handle(row["surface"], "surface", ev_idx, lineno)
                    backend.destroy_surface(surf_h)

                elif ev_name == "destroy_image":
                    img_h = map_handle(row["image"], "image", ev_idx, lineno)
                    backend.destroy_image(img_h)

                elif ev_name == "make_current":
                    surf_h = map_handle(row["surface"], "surface", ev_idx, lineno)
                    backend.make_current(surf_h)

                elif ev_name in (
                    "set_state",
                    "vendor_resource_configuration",
                    "vendor_yuv_configuration",
                    "vendor_yuv_coefficients",
                ):
                    backend.set_state(parse_int_handle(row["parameter"]), row["values"])

                elif ev_name == "set_matrix":
                    mode = parse_int_handle(row["mode"])
                    mat = row["matrix"]
                    if mode not in backend.matrices:
                        raise UnsupportedVG(f"Unknown matrix mode {hex(mode)}")
                    if (
                        not isinstance(mat, list)
                        or len(mat) != 9
                        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in mat)
                    ):
                        raise ValueError(f"Invalid 3x3 matrix at index {ev_idx} (line {lineno})")
                    backend.matrices[mode] = list(mat)

                elif ev_name == "paint_parameter":
                    paint_h = map_handle(row["handle"], "paint", ev_idx, lineno)
                    backend.set_paint_parameter(paint_h, parse_int_handle(row["parameter"]), row["values"])

                elif ev_name == "destroy_paint":
                    paint_h = map_handle(row["handle"], "paint", ev_idx, lineno)
                    backend.destroy_paint(paint_h)

                elif ev_name == "clear":
                    rect = row["rect"]
                    if not isinstance(rect, list) or len(rect) != 4:
                        raise ValueError(f"Invalid clear rect at index {ev_idx} (line {lineno})")
                    backend.clear(*rect)

                elif ev_name == "transform_path":
                    resolved, meta, path_err = resolve_trace_artifact_path(
                        trace_dir,
                        row.get("captured_geometry"),
                        SAFE_TRANSFORM_NAME_RE,
                        "transform_path",
                        ev_idx,
                        lineno,
                    )
                    rec_sha = row.get("sha256")
                    if path_err is not None:
                        errors.append(path_err)
                        transform_dependencies.append(
                            {
                                "event_index": ev_idx,
                                "line": lineno,
                                "recorded_path": row.get("captured_geometry"),
                                "recorded_sha256": rec_sha,
                                "actual_sha256": None,
                                "sha256_match": False,
                                "replay_match": False,
                                "status": path_err["code"],
                            }
                        )
                        continue

                    raw_bytes = resolved.read_bytes()
                    actual_sha = sha256_bytes(raw_bytes)
                    sha_ok = bool(rec_sha) and actual_sha == rec_sha
                    fn_match = SAFE_TRANSFORM_NAME_RE.match(resolved.name)
                    filename_sha_ok = bool(fn_match and fn_match.group(1) == actual_sha)

                    parse_err = None
                    replay_res = None
                    matrix_match = False
                    seg_count_match = False
                    try:
                        cap = json.loads(raw_bytes.decode("utf-8"))
                        matrix_match = cap.get("matrix_path_user_to_surface") == row.get("matrix")
                        seg_count_match = len(cap.get("appended_segments", [])) == row.get("segments_appended")
                        replay_res = replay_transform_path_geometry(cap, workdir_resolved)
                    except Exception as exc:
                        parse_err = f"{type(exc).__name__}: {exc}"

                    replay_ok = bool(
                        parse_err is None
                        and replay_res is not None
                        and replay_res["segments_match"]
                        and replay_res["coordinates_match"]
                        and replay_res["replayed_sha256"] == rec_sha
                        and matrix_match
                        and seg_count_match
                    )

                    dep_entry = {
                        "event_index": ev_idx,
                        "line": lineno,
                        "source_handle": row.get("source"),
                        "destination_handle": row.get("destination"),
                        "segments_appended": row.get("segments_appended"),
                        "size_bytes": len(raw_bytes),
                        **meta,
                        "recorded_sha256": rec_sha,
                        "actual_sha256": actual_sha,
                        "sha256_match": sha_ok,
                        "filename_sha256_match": filename_sha_ok,
                        "matrix_match": matrix_match,
                        "segments_count_match": seg_count_match,
                        "replay_check": replay_res,
                        "parse_error": parse_err,
                        "replay_match": replay_ok,
                        "status": "ok" if (sha_ok and filename_sha_ok and replay_ok) else "geometry_mismatch",
                    }
                    transform_dependencies.append(dep_entry)

                    if not sha_ok or not filename_sha_ok:
                        errors.append(
                            {
                                "code": "transform_path_sha256_mismatch",
                                "event_index": ev_idx,
                                "line": lineno,
                                "path": str(resolved),
                                "expected_sha256": rec_sha,
                                "actual_sha256": actual_sha,
                                "message": (
                                    f"transform_path geometry SHA-256 mismatch at index {ev_idx} "
                                    f"(line {lineno}) for {resolved.name}: expected {rec_sha}, got {actual_sha}"
                                ),
                            }
                        )
                    if not replay_ok:
                        errors.append(
                            {
                                "code": "transform_path_geometry_mismatch",
                                "event_index": ev_idx,
                                "line": lineno,
                                "path": str(resolved),
                                "parse_error": parse_err,
                                "replay_check": replay_res,
                                "message": (
                                    f"transform_path geometry verification failed at index {ev_idx} "
                                    f"(line {lineno}) for {resolved.name}"
                                ),
                            }
                        )

                elif ev_name == "draw_captured_path":
                    segs = row["segments"]
                    coords = row["coordinates"]
                    expected_geom_sha = row.get("geometry_sha256")
                    actual_geom_sha = sha256_bytes(json.dumps([list(segs), coords]).encode())
                    geom_ok = expected_geom_sha is None or actual_geom_sha == expected_geom_sha
                    path_draw_checks.append(
                        {
                            "event_index": ev_idx,
                            "line": lineno,
                            "segment_count": len(segs),
                            "coordinate_count": len(coords),
                            "recorded_geometry_sha256": expected_geom_sha,
                            "actual_geometry_sha256": actual_geom_sha,
                            "geometry_sha256_match": geom_ok,
                        }
                    )
                    if not geom_ok:
                        errors.append(
                            {
                                "code": "draw_path_geometry_sha256_mismatch",
                                "event_index": ev_idx,
                                "line": lineno,
                                "expected_sha256": expected_geom_sha,
                                "actual_sha256": actual_geom_sha,
                                "message": (
                                    f"draw_captured_path geometry_sha256 mismatch at index {ev_idx} "
                                    f"(line {lineno}): expected {expected_geom_sha}, got {actual_geom_sha}"
                                ),
                            }
                        )
                    backend.draw_path_data(
                        segs,
                        coords,
                        row["modes"],
                        row["datatype"],
                        row["scale"],
                        row["bias"],
                    )

                elif ev_name == "draw_captured_image":
                    img_h = map_handle(row["handle"], "image", ev_idx, lineno)
                    backend.draw_image(img_h, *row["source_rect"])

                elif ev_name == "software_swap":
                    surf_h = map_handle(row["surface"], "surface", ev_idx, lineno)
                    replayed_png = backend.swap(surf_h)
                    replayed_swap_event = [e for e in backend.events if e["name"] == "software_swap"][-1]
                    expected_frame_name = f"frame-{swap_seq:04d}.png"
                    swap_index_cur = swap_seq
                    swap_seq += 1

                    resolved_ref, ref_meta, path_err = resolve_trace_artifact_path(
                        trace_dir,
                        row.get("path"),
                        SAFE_FRAME_NAME_RE,
                        "software_swap",
                        ev_idx,
                        lineno,
                    )
                    if path_err is not None:
                        errors.append(path_err)
                        swap_frames.append(
                            {
                                "swap_index": swap_index_cur,
                                "event_index": ev_idx,
                                "line": lineno,
                                "surface": row.get("surface"),
                                "expected_filename": expected_frame_name,
                                "recorded_path": row.get("path"),
                                "replayed_path": str(replayed_png),
                                "replayed_png_sha256": sha256_file(replayed_png),
                                "passed": False,
                                "error": path_err["code"],
                            }
                        )
                        continue

                    referenced_swap_basenames.add(resolved_ref.name)
                    seq_name_ok = resolved_ref.name == expected_frame_name
                    if not seq_name_ok:
                        errors.append(
                            {
                                "code": "swap_frame_sequence_mismatch",
                                "swap_index": swap_index_cur,
                                "event_index": ev_idx,
                                "line": lineno,
                                "expected_filename": expected_frame_name,
                                "actual_filename": resolved_ref.name,
                                "message": (
                                    f"Swap #{swap_index_cur} at index {ev_idx} references {resolved_ref.name}, "
                                    f"expected {expected_frame_name}"
                                ),
                            }
                        )

                    ref_png_sha = sha256_file(resolved_ref)
                    rep_png_sha = sha256_file(replayed_png)
                    with Image.open(resolved_ref) as ref_im, Image.open(replayed_png) as rep_im:
                        cmp_result = compare_rgba_images(ref_im, rep_im)

                    rec_draw_calls = row.get("draw_calls")
                    draw_calls_match = rec_draw_calls is None or rec_draw_calls == backend.draw_count
                    if not draw_calls_match:
                        errors.append(
                            {
                                "code": "swap_draw_count_mismatch",
                                "swap_index": swap_index_cur,
                                "event_index": ev_idx,
                                "line": lineno,
                                "recorded_draw_calls": rec_draw_calls,
                                "replayed_draw_calls": backend.draw_count,
                                "message": (
                                    f"Swap #{swap_index_cur} draw_calls mismatch: "
                                    f"recorded {rec_draw_calls} vs replayed {backend.draw_count}"
                                ),
                            }
                        )

                    frame_passed = bool(
                        seq_name_ok
                        and draw_calls_match
                        and cmp_result["dimensions_match"]
                        and cmp_result["rgba_pixels_identical"]
                    )
                    if not (cmp_result["dimensions_match"] and cmp_result["rgba_pixels_identical"]):
                        errors.append(
                            {
                                "code": "frame_pixel_mismatch",
                                "swap_index": swap_index_cur,
                                "event_index": ev_idx,
                                "line": lineno,
                                "reference_frame": str(resolved_ref),
                                "replayed_frame": str(replayed_png),
                                "dimensions_match": cmp_result["dimensions_match"],
                                "reference_size": cmp_result["reference_size"],
                                "replayed_size": cmp_result["replayed_size"],
                                "changed_pixel_count": cmp_result["changed_pixel_count"],
                                "diff_bbox": cmp_result["diff_bbox"],
                                "message": (
                                    f"Frame {resolved_ref.name} (swap #{swap_index_cur}, event {ev_idx}) "
                                    f"RGBA mismatch: changed_pixels={cmp_result['changed_pixel_count']}, "
                                    f"diff_bbox={cmp_result['diff_bbox']}"
                                ),
                            }
                        )

                    swap_frames.append(
                        {
                            "swap_index": swap_index_cur,
                            "event_index": ev_idx,
                            "line": lineno,
                            "surface": row.get("surface"),
                            "filename": resolved_ref.name,
                            "expected_filename": expected_frame_name,
                            "sequence_filename_match": seq_name_ok,
                            "recorded_draw_calls": rec_draw_calls,
                            "replayed_draw_calls": backend.draw_count,
                            "draw_calls_match": draw_calls_match,
                            "reference_path": str(resolved_ref),
                            "replayed_path": str(replayed_png),
                            "reference_png_sha256": ref_png_sha,
                            "replayed_png_sha256": rep_png_sha,
                            "png_bytes_identical": ref_png_sha == rep_png_sha,
                            "recorded_canvas_pixels_sha256": row.get("pixels_sha256"),
                            "replayed_canvas_pixels_sha256": replayed_swap_event.get("pixels_sha256"),
                            "canvas_pixels_sha256_match": (
                                row.get("pixels_sha256") == replayed_swap_event.get("pixels_sha256")
                            ),
                            **cmp_result,
                            "passed": frame_passed,
                        }
                    )

                elif ev_name in (
                    "original_context_resource_requests",
                    "observed_native_window",
                    "original_linear_buffer_mode",
                    "original_frame_mode",
                    "original_frame_status_read_clear",
                ):
                    pass

            except UnsupportedVG as exc:
                errors.append(
                    {
                        "code": "backend_unsupported_operation",
                        "event_index": ev_idx,
                        "line": lineno,
                        "event_name": ev_name,
                        "exception": type(exc).__name__,
                        "message": f"UnsupportedVG at event {ev_idx} (line {lineno}, {ev_name}): {exc}",
                    }
                )
            except Exception as exc:
                errors.append(
                    {
                        "code": "replay_execution_error",
                        "event_index": ev_idx,
                        "line": lineno,
                        "event_name": ev_name,
                        "exception": type(exc).__name__,
                        "message": f"{type(exc).__name__} at event {ev_idx} (line {lineno}, {ev_name}): {exc}",
                    }
                )

        if pending_entry is not None:
            errors.append(
                {
                    "code": "truncated_call_sequence",
                    "event_index": pending_entry.get("index"),
                    "api": pending_entry.get("api"),
                    "message": (
                        f"Trace ended with unclosed guest_api_entry {pending_entry.get('api')!r} "
                        f"at index {pending_entry.get('index')} (no guest_api_return or unsupported_or_invalid_call)"
                    ),
                }
            )
    finally:
        backend.close()

    # Inventory all PNG files in source directory, strictly separating categories.
    png_inventory = inventory_directory_pngs(trace_dir, referenced_swap_basenames, is_synthetic_trace)

    # Check for orphan frame-XXXX.png files not accounted for by software_swap.
    orphan_frames = [
        p["filename"]
        for p in png_inventory["unclassified_pngs"]
        if SAFE_FRAME_NAME_RE.match(p["filename"])
    ]
    if orphan_frames:
        errors.append(
            {
                "code": "unreferenced_swap_frame_files",
                "files": orphan_frames,
                "message": f"Found frame-*.png files not referenced by any software_swap event: {orphan_frames}",
            }
        )

    # Targeted regression check for local-shared-image-stride (explicitly NOT a generic menu detector).
    targeted_regression = None
    if targeted_roi_check == "shared_image_stride_two_frame":
        if len(swap_frames) != 2 or not all("reference_path" in f and "replayed_path" in f for f in swap_frames):
            roi_err = {
                "code": "targeted_roi_regression_failed",
                "message": (
                    f"shared-image-stride targeted ROI check requires exactly 2 valid swap frames, "
                    f"found {len(swap_frames)}"
                ),
            }
            errors.append(roi_err)
            targeted_regression = {
                "check_id": "shared_image_stride_two_frame",
                "is_generic_menu_detector": False,
                "disclaimer": (
                    "Targeted pixel regression check against display/render-wait-analysis/"
                    "two-frame-pixel-verification.json; NOT a generic menu detector."
                ),
                "passed": False,
                "errors": [roi_err["message"]],
            }
        else:
            ref_roi = check_shared_image_stride_roi(
                Path(swap_frames[0]["reference_path"]),
                Path(swap_frames[1]["reference_path"]),
                "reference_frames",
            )
            rep_roi = check_shared_image_stride_roi(
                Path(swap_frames[0]["replayed_path"]),
                Path(swap_frames[1]["replayed_path"]),
                "replayed_frames",
            )
            roi_passed = ref_roi["passed"] and rep_roi["passed"]
            if not roi_passed:
                for msg in ref_roi["failures"] + rep_roi["failures"]:
                    errors.append({"code": "targeted_roi_regression_failed", "message": msg})
            targeted_regression = {
                "check_id": "shared_image_stride_two_frame",
                "evidence_reference": "display/render-wait-analysis/two-frame-pixel-verification.json",
                "is_generic_menu_detector": False,
                "disclaimer": (
                    "Targeted pixel regression check for local-shared-image-stride two-frame "
                    "battery update; explicitly NOT a generic menu detector or firmware UI validator."
                ),
                "passed": roi_passed,
                "reference_pair": ref_roi,
                "replayed_pair": rep_roi,
            }

    # Honest completion status classification.
    if errors:
        completion_status = "failed"
        full_run_succeeded = False
        prefix_verified = False
    elif unsupported_stop is not None:
        completion_status = "completed_prefix_only"
        full_run_succeeded = False
        prefix_verified = True
        warnings.append(
            {
                "code": "mid_run_unsupported_stop",
                "message": (
                    f"Trace stopped mid-run at unsupported call {unsupported_stop['api']!r} "
                    f"(index {unsupported_stop['event_index']}, line {unsupported_stop['line']}). "
                    "Verified completed prefix ONLY; NOT a full-run success."
                ),
            }
        )
    else:
        completion_status = "completed_full"
        full_run_succeeded = True
        prefix_verified = True

    if expected_completion_status is not None:
        passed = completion_status == expected_completion_status
        if not passed and not errors:
            errors.append(
                {
                    "code": "unexpected_completion_status",
                    "expected": expected_completion_status,
                    "actual": completion_status,
                    "message": (
                        f"Expected completion_status={expected_completion_status!r}, "
                        f"got {completion_status!r}"
                    ),
                }
            )
    elif require_full:
        passed = completion_status == "completed_full"
        if not passed and not errors:
            errors.append(
                {
                    "code": "full_run_required",
                    "actual": completion_status,
                    "message": (
                        f"Trace did not complete full run (status={completion_status!r}); "
                        "--require-full rejects prefix-only traces"
                    ),
                }
            )
    else:
        passed = completion_status in ("completed_full", "completed_prefix_only")

    return {
        "name": trace_name,
        "trace_dir": str(trace_dir_resolved),
        "replay_workdir": str(replay_out),
        "source_calls_jsonl": {
            "path": str(calls_file.resolve()),
            "size_bytes": len(calls_bytes),
            "sha256": calls_sha256,
            "event_count": len(rows),
            "event_type_counts": event_counts,
        },
        "trace_provenance": "synthetic_test" if is_synthetic_trace else "captured_firmware_calls",
        "completion_status": completion_status,
        "expected_completion_status": expected_completion_status,
        "full_run_succeeded": full_run_succeeded,
        "prefix_verified": prefix_verified,
        "passed": passed,
        "unsupported_stop": unsupported_stop,
        "dependencies": {
            "images": image_dependencies,
            "transform_paths": transform_dependencies,
            "draw_path_geometry_checks": {
                "total": len(path_draw_checks),
                "matched": sum(1 for c in path_draw_checks if c["geometry_sha256_match"]),
                "mismatched": [c for c in path_draw_checks if not c["geometry_sha256_match"]],
            },
        },
        "surfaces": surfaces_summary,
        "png_inventory": png_inventory,
        "swap_frames": swap_frames,
        "swap_frame_count": len(swap_frames),
        "targeted_regression": targeted_regression,
        "warnings": warnings,
        "errors": errors,
    }


def verify_corpus(
    trace_specs: list[dict],
    *,
    workdir: Path | None = None,
    keep_workdir: bool = False,
    require_full: bool = False,
) -> dict:
    """Verify a list of trace specs and build the aggregated corpus report."""
    created_temp = False
    if workdir is None:
        tmp_root = _default_tmp_root()
        workdir = Path(tempfile.mkdtemp(prefix="s5-trace-corpus-", dir=str(tmp_root)))
        created_temp = True
    else:
        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)

    trace_reports = []
    try:
        for idx, spec in enumerate(trace_specs):
            safe_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec["name"])
            per_trace_work = workdir / f"{idx:02d}-{safe_slug}"
            per_trace_work.mkdir(parents=True, exist_ok=True)
            rep = verify_trace(
                Path(spec["path"]),
                per_trace_work,
                name=spec["name"],
                expected_completion_status=spec.get("expected_completion_status"),
                targeted_roi_check=spec.get("targeted_roi_check"),
                require_full=require_full,
            )
            trace_reports.append(rep)
    finally:
        if created_temp and not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    full_count = sum(1 for r in trace_reports if r["completion_status"] == "completed_full")
    prefix_only_count = sum(1 for r in trace_reports if r["completion_status"] == "completed_prefix_only")
    failed_count = sum(1 for r in trace_reports if r["completion_status"] == "failed")
    overall_passed = all(r["passed"] for r in trace_reports) and len(trace_reports) > 0

    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": (
            "Host-only OpenVGBackend replay verification against previously captured "
            "graphics traces. Does NOT execute QEMU, does NOT prove new firmware boot, "
            "and does NOT claim main menu rendering."
        ),
        "qemu_executed": False,
        "workdir": str(workdir),
        "workdir_preserved": bool(keep_workdir or not created_temp),
        "summary": {
            "total_traces": len(trace_reports),
            "completed_full": full_count,
            "completed_prefix_only": prefix_only_count,
            "failed": failed_count,
            "total_swap_frames_verified": sum(
                sum(1 for f in r.get("swap_frames", []) if f.get("passed"))
                for r in trace_reports
            ),
            "overall_passed": overall_passed,
        },
        "traces": trace_reports,
    }


def build_default_specs(repo_root: Path = REPO_ROOT) -> list[dict]:
    specs = []
    for entry in DEFAULT_CORPUS:
        specs.append(
            {
                "name": entry["name"],
                "path": str((repo_root / entry["relative_dir"]).resolve()),
                "expected_completion_status": entry["expected_completion_status"],
                "targeted_roi_check": entry["targeted_roi_check"],
            }
        )
    return specs


def build_custom_specs(paths: list[str], enable_shared_stride_roi: bool = False) -> list[dict]:
    specs = []
    for raw in paths:
        p = Path(raw).resolve()
        trace_dir = p.parent if p.is_file() and p.name == "calls.jsonl" else p
        name = trace_dir.parent.name if trace_dir.name == "graphics" and trace_dir.parent.name else trace_dir.name
        roi_check = (
            "shared_image_stride_two_frame"
            if (enable_shared_stride_roi or name == "local-shared-image-stride")
            else None
        )
        specs.append(
            {
                "name": name,
                "path": str(trace_dir),
                "expected_completion_status": None,
                "targeted_roi_check": roi_check,
            }
        )
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay & regression-verify captured S5 OpenVG graphics traces."
    )
    parser.add_argument(
        "traces",
        nargs="*",
        help="Optional trace directories (or calls.jsonl paths). Defaults to the 3-trace corpus.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write machine-readable JSON report to this path.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="Optional isolated work directory for replay artifacts (must not overlap source).",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep temporary replay work directory after completion.",
    )
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="Treat prefix-only unsupported traces as verification failure.",
    )
    parser.add_argument(
        "--shared-image-stride-roi",
        action="store_true",
        help="Force the targeted local-shared-image-stride two-frame ROI check on custom traces.",
    )
    args = parser.parse_args(argv)

    specs = (
        build_custom_specs(args.traces, enable_shared_stride_roi=args.shared_image_stride_roi)
        if args.traces
        else build_default_specs(REPO_ROOT)
    )

    report = verify_corpus(
        specs,
        workdir=args.workdir,
        keep_workdir=args.keep_workdir,
        require_full=args.require_full,
    )

    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")

    # Concise human-readable summary on stdout.
    summary = report["summary"]
    print(
        f"Trace corpus verification: overall_passed={summary['overall_passed']} "
        f"(total={summary['total_traces']}, full={summary['completed_full']}, "
        f"prefix_only={summary['completed_prefix_only']}, failed={summary['failed']}, "
        f"swap_frames_verified={summary['total_swap_frames_verified']})"
    )
    for t in report["traces"]:
        stop_info = ""
        if t.get("unsupported_stop"):
            us = t["unsupported_stop"]
            stop_info = f" [PREFIX ONLY: stopped at {us['api']} idx={us['event_index']}]"
        print(
            f"  - {t['name']}: status={t['completion_status']} "
            f"full_run_succeeded={t['full_run_succeeded']} "
            f"swaps={t['swap_frame_count']} errors={len(t['errors'])}{stop_info}"
        )
        for err in t["errors"]:
            print(f"      ERROR [{err.get('code')}]: {err.get('message')}")
    if args.output:
        print(f"Report written to: {args.output.resolve()}")

    return 0 if summary["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
