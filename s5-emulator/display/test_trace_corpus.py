#!/usr/bin/env python3
"""Destructive fixture tests for display/verify_trace_corpus.py.

All destructive mutations run exclusively on temporary copies of real captured
traces under TMPDIR (SATA disk). Source trace directories are hashed before and
after every test to prove zero mutation of original evidence and zero auto-update
of baselines.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import verify_trace_corpus as vtc  # noqa: E402


REAL_FRAME_QUERY_DIR = REPO_ROOT / "menu-smp/results/local-frame-query/graphics"
REAL_SHARED_STRIDE_DIR = REPO_ROOT / "menu-smp/results/local-shared-image-stride/graphics"
REAL_LCD_REFRESH_DIR = REPO_ROOT / "menu-smp/results/local-requested-lcd-refresh/graphics"


def directory_snapshot(path: Path) -> dict[str, str]:
    snap = {}
    for item in sorted(path.iterdir()):
        if item.is_file():
            snap[item.name] = hashlib.sha256(item.read_bytes()).hexdigest()
    return snap


def make_temp_root() -> Path:
    env_tmp = os.environ.get("TMPDIR")
    if env_tmp:
        root = Path(env_tmp)
        root.mkdir(parents=True, exist_ok=True)
        return root
    sata_tmp = REPO_ROOT.parent.parent / "tmp"
    if sata_tmp.parent.exists():
        sata_tmp.mkdir(parents=True, exist_ok=True)
        return sata_tmp
    return Path(tempfile.gettempdir())


class TraceCorpusVerifierTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix="s5-trace-fixture-", dir=str(make_temp_root())
        )
        self.tmp_root = Path(self._tmp.name)
        self.source_snapshots_before = {
            "local-frame-query": directory_snapshot(REAL_FRAME_QUERY_DIR),
            "local-shared-image-stride": directory_snapshot(REAL_SHARED_STRIDE_DIR),
            "local-requested-lcd-refresh": directory_snapshot(REAL_LCD_REFRESH_DIR),
        }

    def tearDown(self):
        self._tmp.cleanup()
        # Prove zero mutation of any real source trace directory.
        self.assertEqual(
            directory_snapshot(REAL_FRAME_QUERY_DIR),
            self.source_snapshots_before["local-frame-query"],
        )
        self.assertEqual(
            directory_snapshot(REAL_SHARED_STRIDE_DIR),
            self.source_snapshots_before["local-shared-image-stride"],
        )
        self.assertEqual(
            directory_snapshot(REAL_LCD_REFRESH_DIR),
            self.source_snapshots_before["local-requested-lcd-refresh"],
        )

    def _copy_trace(self, src: Path, label: str) -> tuple[Path, Path]:
        dst = self.tmp_root / f"{label}-src" / "graphics"
        shutil.copytree(src, dst)
        work = self.tmp_root / f"{label}-work"
        work.mkdir(parents=True, exist_ok=True)
        return dst, work

    def test_01_baseline_copy_passes_and_does_not_mutate_copy(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "baseline")
        before = directory_snapshot(trace_copy)
        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
            targeted_roi_check="shared_image_stride_two_frame",
        )
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["completion_status"], "completed_full")
        self.assertTrue(report["full_run_succeeded"])
        self.assertEqual(report["swap_frame_count"], 2)
        self.assertTrue(report["targeted_regression"]["passed"])
        self.assertFalse(report["targeted_regression"]["is_generic_menu_detector"])
        self.assertEqual(len(report["png_inventory"]["real_swap_frames"]), 2)
        self.assertEqual(len(report["png_inventory"]["intermediate_and_incomplete_surfaces"]), 1)
        self.assertEqual(directory_snapshot(trace_copy), before)

    def test_02_deleted_image_dependency_fails_with_locatable_error(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "deleted-image")
        victim = trace_copy / "image-40000012.bin"
        self.assertTrue(victim.exists())
        victim.unlink()
        before = directory_snapshot(trace_copy)

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["completion_status"], "failed")
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("missing_dependency", codes)
        missing_errs = [e for e in report["errors"] if e["code"] == "missing_dependency"]
        self.assertTrue(any("image-40000012.bin" in e["message"] for e in missing_errs))
        self.assertIsNotNone(missing_errs[0].get("event_index"))
        self.assertIsNotNone(missing_errs[0].get("line"))
        # Must never recreate or auto-update missing files.
        self.assertFalse(victim.exists())
        self.assertEqual(directory_snapshot(trace_copy), before)

    def test_03_mutated_transform_path_geometry_file_fails(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "mutated-transform-json")
        tp_files = sorted(trace_copy.glob("transform-path-*.json"))
        self.assertTrue(tp_files)
        target = tp_files[0]
        data = json.loads(target.read_text(encoding="utf-8"))
        # Perturb one coordinate in appended_coordinates.
        data["appended_coordinates"][0] = float(data["appended_coordinates"][0]) + 1.25
        target.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        before = directory_snapshot(trace_copy)

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["completion_status"], "failed")
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("transform_path_sha256_mismatch", codes)
        self.assertIn("transform_path_geometry_mismatch", codes)
        self.assertEqual(directory_snapshot(trace_copy), before)

    def test_04_mutated_draw_path_geometry_in_calls_jsonl_fails(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "mutated-draw-path")
        calls = trace_copy / "calls.jsonl"
        lines = calls.read_text(encoding="utf-8").splitlines()
        mutated = False
        new_lines = []
        for line in lines:
            obj = json.loads(line)
            if not mutated and obj.get("name") == "draw_captured_path" and obj.get("coordinates"):
                obj["coordinates"][0] = float(obj["coordinates"][0]) + 15.0
                mutated = True
            new_lines.append(json.dumps(obj))
        self.assertTrue(mutated)
        calls.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["completion_status"], "failed")
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("draw_path_geometry_sha256_mismatch", codes)

    def test_05_swapped_two_reference_frames_fails_with_diff_bbox(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "swapped-frames")
        f0 = trace_copy / "frame-0000.png"
        f1 = trace_copy / "frame-0001.png"
        b0, b1 = f0.read_bytes(), f1.read_bytes()
        f0.write_bytes(b1)
        f1.write_bytes(b0)
        before = directory_snapshot(trace_copy)

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["completion_status"], "failed")
        pixel_errs = [e for e in report["errors"] if e["code"] == "frame_pixel_mismatch"]
        self.assertEqual(len(pixel_errs), 2)
        for err in pixel_errs:
            self.assertGreater(err["changed_pixel_count"], 0)
            self.assertEqual(err["diff_bbox"], list(vtc.SHARED_IMAGE_STRIDE_BATTERY_BBOX))
        # Ensure baselines were NOT auto-updated back!
        self.assertEqual(directory_snapshot(trace_copy), before)

    def test_06_corrupted_truncated_tail_line_in_calls_jsonl_fails(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "truncated-tail")
        calls = trace_copy / "calls.jsonl"
        lines = calls.read_text(encoding="utf-8").splitlines()
        # Truncate the final JSON line mid-object.
        lines[-1] = lines[-1][:20]
        calls.write_text("\n".join(lines) + "\n", encoding="utf-8")

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["completion_status"], "failed")
        corrupt_errs = [e for e in report["errors"] if e["code"] == "corrupt_json_line"]
        self.assertEqual(len(corrupt_errs), 1)
        self.assertEqual(corrupt_errs[0]["line"], len(lines))

    def test_07_single_known_pixel_modified_in_reference_png_fails(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "single-pixel")
        f0 = trace_copy / "frame-0000.png"
        target_xy = (300, 300)  # Inside center ROI (160, 250)-(800, 390)
        with Image.open(f0) as im:
            rgba = im.convert("RGBA")
            r, g, b, a = rgba.getpixel(target_xy)
            rgba.putpixel(target_xy, ((r + 1) % 256, g, b, a))
            rgba.save(f0)
        before = directory_snapshot(trace_copy)

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
            targeted_roi_check="shared_image_stride_two_frame",
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["completion_status"], "failed")
        pixel_errs = [e for e in report["errors"] if e["code"] == "frame_pixel_mismatch"]
        self.assertEqual(len(pixel_errs), 1)
        self.assertEqual(pixel_errs[0]["changed_pixel_count"], 1)
        self.assertEqual(pixel_errs[0]["diff_bbox"], [300, 300, 301, 301])
        # Targeted center ROI check must ALSO flag the reference center ROI mutation.
        self.assertFalse(report["targeted_regression"]["passed"])
        self.assertFalse(report["targeted_regression"]["reference_pair"]["center_pixels_identical"])
        # Replayed frames (rendered from unmutated calls.jsonl) still agree on center ROI.
        self.assertTrue(report["targeted_regression"]["replayed_pair"]["center_pixels_identical"])
        # Reference PNG must NOT be overwritten.
        self.assertEqual(directory_snapshot(trace_copy), before)

    def test_07b_single_pixel_alpha_only_change_also_fails(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "single-pixel-alpha")
        f1 = trace_copy / "frame-0001.png"
        target_xy = (10, 10)
        with Image.open(f1) as im:
            rgba = im.convert("RGBA")
            r, g, b, a = rgba.getpixel(target_xy)
            rgba.putpixel(target_xy, (r, g, b, (a + 1) % 256))
            rgba.save(f1)

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
        )
        self.assertFalse(report["passed"])
        pixel_errs = [e for e in report["errors"] if e["code"] == "frame_pixel_mismatch"]
        self.assertEqual(len(pixel_errs), 1)
        self.assertEqual(pixel_errs[0]["changed_pixel_count"], 1)
        self.assertEqual(pixel_errs[0]["diff_bbox"], [10, 10, 11, 11])

    def test_08_mid_run_unsupported_trace_is_honestly_prefix_only_not_full(self):
        trace_copy, work = self._copy_trace(REAL_FRAME_QUERY_DIR, "frame-query-prefix")
        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-frame-query",
            expected_completion_status="completed_prefix_only",
        )
        self.assertTrue(report["passed"])
        self.assertEqual(report["completion_status"], "completed_prefix_only")
        self.assertFalse(report["full_run_succeeded"])
        self.assertTrue(report["prefix_verified"])
        self.assertIsNotNone(report["unsupported_stop"])
        self.assertEqual(report["unsupported_stop"]["api"], "eglCreatePbufferFromClientBuffer")
        self.assertEqual(report["unsupported_stop"]["event_index"], 1028)
        self.assertEqual(report["swap_frame_count"], 1)
        self.assertTrue(report["swap_frames"][0]["rgba_pixels_identical"])

        # With --require-full, a prefix-only trace MUST fail with non-zero CLI exit code.
        rc = vtc.main([str(trace_copy), "--require-full"])
        self.assertNotEqual(rc, 0)

    def test_09_recompressed_png_with_identical_rgba_still_passes(self):
        """RGBA identity must NOT require identical PNG compression bytes."""
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "recompressed-png")
        f0 = trace_copy / "frame-0000.png"
        orig_sha = vtc.sha256_file(f0)
        with Image.open(f0) as im:
            im.convert("RGBA").save(f0, compress_level=0)
        new_sha = vtc.sha256_file(f0)
        self.assertNotEqual(orig_sha, new_sha)

        report = vtc.verify_trace(
            trace_copy,
            work,
            name="local-shared-image-stride",
            expected_completion_status="completed_full",
            targeted_roi_check="shared_image_stride_two_frame",
        )
        self.assertTrue(report["passed"], report["errors"])
        self.assertFalse(report["swap_frames"][0]["png_bytes_identical"])
        self.assertTrue(report["swap_frames"][0]["rgba_pixels_identical"])
        self.assertEqual(report["swap_frames"][0]["changed_pixel_count"], 0)

    def test_10_path_traversal_and_unknown_event_rejected(self):
        trace_copy, work = self._copy_trace(REAL_SHARED_STRIDE_DIR, "traversal-and-unknown")
        calls = trace_copy / "calls.jsonl"
        lines = calls.read_text(encoding="utf-8").splitlines()
        new_lines = []
        mutated_img = False
        for line in lines:
            obj = json.loads(line)
            if not mutated_img and obj.get("name") == "captured_image":
                obj["source_path"] = "../outside-secret.bin"
                mutated_img = True
            new_lines.append(json.dumps(obj))
        new_lines.append(json.dumps({"index": len(new_lines), "name": "invented_unknown_op"}))
        calls.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        report = vtc.verify_trace(trace_copy, work, name="traversal-test")
        self.assertFalse(report["passed"])
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("path_traversal", codes)
        self.assertIn("unknown_event", codes)

    def test_11_cli_returns_nonzero_on_failure_and_zero_on_default_corpus(self):
        out_json = self.tmp_root / "corpus-ok.json"
        rc_ok = vtc.main(["--output", str(out_json)])
        self.assertEqual(rc_ok, 0)
        data = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertTrue(data["summary"]["overall_passed"])
        self.assertEqual(data["summary"]["completed_full"], 2)
        self.assertEqual(data["summary"]["completed_prefix_only"], 1)
        self.assertEqual(data["summary"]["failed"], 0)

        # Now run CLI against a broken fixture and verify non-zero exit code.
        broken_copy, _ = self._copy_trace(REAL_SHARED_STRIDE_DIR, "cli-broken")
        (broken_copy / "image-40000012.bin").unlink()
        out_bad = self.tmp_root / "corpus-bad.json"
        rc_bad = vtc.main([str(broken_copy), "--output", str(out_bad)])
        self.assertNotEqual(rc_bad, 0)
        bad_data = json.loads(out_bad.read_text(encoding="utf-8"))
        self.assertFalse(bad_data["summary"]["overall_passed"])
        self.assertEqual(bad_data["summary"]["failed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
