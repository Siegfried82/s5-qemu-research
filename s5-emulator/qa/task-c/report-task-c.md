# Task C Report: Graphics Trace Batch Replay & Regression Verifier

## Scope & Honest Boundaries
- **Verified**: Host-side `OpenVGBackend` replay fidelity against previously captured real S5 graphics traces (`local-frame-query`, `local-shared-image-stride`, `local-requested-lcd-refresh`), including source trace integrity, dependency SHA-256s, `transform_path` geometry re-execution, sequential real swap frame RGBA comparison, targeted `local-shared-image-stride` two-frame ROI regression, and destructive failure-mode detection.
- **Not verified / Out of scope**: QEMU was **not** executed (`qemu_executed: false`). Replay verifies backend rendering on previously captured calls only — it does **not** prove new firmware execution, full camera boot, or main menu display.
- **Parallel isolation**: No existing files were modified (`display/openvg_backend.py`, `display/graphics_hle.py`, `display/replay_graphics_trace.py`, `menu-smp/boot.py`, `qemu/`, and all existing `results/` / `qa/task-*` artifacts are untouched). Defects discovered in `display/replay_graphics_trace.py` are delivered as a suggested patch (`qa/task-c/replay_graphics_trace.patch`).

## Deliverables
| File | Purpose |
| --- | --- |
| `display/verify_trace_corpus.py` | Batch & custom-trace replay verifier with strict dependency SHA checking, path-traversal rejection, RGBA 4-channel comparison, artifact provenance separation, honest mid-run unsupported classification, targeted `local-shared-image-stride` ROI regression, machine-readable JSON output, and non-zero exit code on failure. |
| `display/test_trace_corpus.py` | 12-test destructive fixture suite operating strictly on temporary copies under SATA `TMPDIR`, asserting zero source mutation and zero baseline auto-update. |
| `qa/task-c/corpus-report.json` | Machine-readable per-trace & per-frame verification report for the 3-trace real corpus. |
| `qa/task-c/verify-trace-corpus.log` | CLI stdout log of `verify_trace_corpus.py`. |
| `qa/task-c/test-trace-corpus.log` | Full verbose unittest log of `test_trace_corpus.py` (12/12 `OK`). |
| `qa/task-c/replay_graphics_trace.patch` | Suggested unified diff for `display/replay_graphics_trace.py` (dry-run apply + `py_compile` verified on a temp copy). |
| `qa/task-c/verification.json` | Machine-readable Task C summary separating `tests_executed_ok` and `assertions_passed`. |

## Reproducible Commands
```sh
cd /Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator

# 1. Verify default 3-trace real corpus into isolated SATA TMPDIR
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B \
  display/verify_trace_corpus.py --output qa/task-c/corpus-report.json

# 2. Run 12 destructive fixture & regression tests on temporary copies
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B \
  display/test_trace_corpus.py
```

## Real Corpus Verification Results
| Trace | Completion Status | `full_run_succeeded` | Real Swap Frames | Image Deps | Transform Paths | Incomplete Surfaces (Separated) | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `local-frame-query` | **`completed_prefix_only`** | `false` | 1 (`frame-0000.png`, 0 RGBA diff) | 4 / 4 SHA match | 2 / 2 re-verified | 1 (`incomplete-surface-60000002.png`) | Stopped mid-run at `eglCreatePbufferFromClientBuffer` (event 1028, line 1029, PC `0x64c2878`, `UnsupportedVG`); prefix only (27 draws, 1 swap). Rejected under `--require-full`. |
| `local-shared-image-stride` | `completed_full` | `true` | 2 (`frame-0000.png`, `frame-0001.png`, 0 RGBA diff) | 5 / 5 SHA match | 2 / 2 re-verified | 1 (`incomplete-surface-60000002.png`) | Targeted ROI check passed on both reference & replayed pairs: center ROI `(160,250)-(800,390)` 0 diff pixels, center bright pixels `[9696, 9696]`, whole-image `diff_bbox=[866, 21, 929, 59]` (2,309 changed pixels strictly inside battery region). Explicitly marked `is_generic_menu_detector: false`. |
| `local-requested-lcd-refresh` | `completed_full` | `true` | 5 (`frame-0000.png`..`frame-0004.png`, 0 RGBA diff) | 38 / 38 SHA match | 10 / 10 re-verified | 1 (`incomplete-surface-60000002.png`) | All 110 draws and 5 swaps replayed with 0 RGBA pixel diffs. |

## Destructive Fixture Tests (12/12 Passed)
1. **Baseline copy**: passes without modifying any file in the copy or source directory.
2. **Deleted image dependency** (`image-40000012.bin` deleted): fails with locatable `missing_dependency` (event index & line) without recreating the file.
3. **Mutated `transform-path-*.json` geometry**: fails with `transform_path_sha256_mismatch` and `transform_path_geometry_mismatch`.
4. **Mutated `draw_captured_path` geometry in `calls.jsonl`**: fails with `draw_path_geometry_sha256_mismatch`.
5. **Swapped two reference frames** (`frame-0000.png` <-> `frame-0001.png`): fails with `frame_pixel_mismatch` on both swaps reporting `diff_bbox=[866, 21, 929, 59]`; baselines are not overwritten.
6. **Corrupted/truncated tail line in `calls.jsonl`**: fails with `corrupt_json_line` reporting exact 1-based line number.
7. **Single pixel modified (RGB-only) inside center ROI** at `(300, 300)`: fails with `changed_pixel_count=1`, `diff_bbox=[300, 300, 301, 301]`, AND fails the targeted reference center ROI check. *(Note: this fixture caught a real Pillow RGBA `Image.getbbox()` pitfall where Pillow defaults to `alpha_only=True` and would silently miss RGB-only diffs when alpha diff is 0; fixed by combining all 4 RGBA channels into an L-mode max-diff image before calling `getbbox()`.)*
8. **Single pixel modified (alpha-only)** at `(10, 10)`: fails with `changed_pixel_count=1`, `diff_bbox=[10, 10, 11, 11]`.
9. **Mid-run unsupported trace (`local-frame-query`)**: honestly reports `completed_prefix_only` / `full_run_succeeded=false`, and exits non-zero under `--require-full`.
10. **Recompressed PNG with identical RGBA** (`compress_level=0`): passes even when PNG file SHA-256 differs (`png_bytes_identical=false`, `rgba_pixels_identical=true`).
11. **Path traversal (`../outside-secret.bin`) & unknown event**: both rejected (`path_traversal`, `unknown_event`), never silently skipped.
12. **CLI exit codes**: exits `0` on valid default corpus, non-zero on broken fixture.

## Defects Found in `display/replay_graphics_trace.py` (Suggested Patch Only)
Delivered as `qa/task-c/replay_graphics_trace.patch` (not applied directly to preserve parallel isolation):
1. Hardcoded capture-machine `Path(row['source_path']).read_bytes()` fails when the repository or trace directory is relocated, and does not reject `..` path traversal or verify `sha256`.
2. Event name typo `'vendor_yuv_coefficients'` instead of `'vendor_yuv_configuration'` (what `OpenVGBackend.set_state()` actually logs), causing YUV config events to be silently skipped.
3. Silent ignoring of `unsupported_or_invalid_call` and unrecognized event types, plus lack of line-numbered JSONL diagnostics on corrupt/truncated lines.
