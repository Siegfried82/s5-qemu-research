# Task D Report — Read-Only Local Native Frame Viewer

## Scope & Isolation Compliance

- **Allowed write scope strictly respected**: Created only files under `viewer/` and `qa/task-d/`.
- **Zero untouched-area modifications**: Did not modify `menu-smp/boot.py`, `display/`, `qemu/`, `启动S5模拟器.command`, or any existing `results/` / `qa/task-a/` / `qa/task-b/` / `qa/task-timezone-drift/` directories.
- **No QEMU execution or external dependencies**: Implemented strictly in Python 3 standard library (`Python 3.9.6`), zero third-party packages, zero CDN/network requests.

## Created Files

1. `viewer/s5_frame_viewer.py` — Pure stdlib loopback HTTP server (`ThreadingHTTPServer`), read-only `ResultDirScanner`, in-memory PNG chunk/CRC/`IHDR`/`IDAT`/`IEND` validator, and self-contained single-page web UI.
2. `viewer/README.md` — Usage, CLI options, HTTP API, and security/scope boundary documentation.
3. `qa/task-d/test_frame_viewer.py` — Automated 10-case verification suite (synthetic live-run simulation + security + byte-level read-only invariance + both real corpora).
4. `qa/task-d/verification.json` — Machine-readable verification record separating `tests_executed_ok` and `assertions_passed`.
5. `qa/task-d/test.log` — Captured test run stdout.
6. `qa/task-d/report-task-d.md` — This report.

## Copy-Pasteable Launch Command

Run from `/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator`:

```sh
python3 -B viewer/s5_frame_viewer.py --result-dir menu-smp/results/local-requested-lcd-refresh
```

It binds `127.0.0.1` on a random free port (`--port 0`) and prints the exact loopback URL (e.g. `http://127.0.0.1:59360/`). Non-loopback bind addresses such as `--host 0.0.0.0` are rejected with exit code `2`.

## Automated Verification Command & Outcome

```sh
cd /Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator && \
TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp python3 -B qa/task-d/test_frame_viewer.py
```

- **`tests_executed_ok`**: `true` (10 / 10 test cases executed without runtime error)
- **`assertions_passed`**: `true` (10 / 10 test cases passed all assertions)
- Machine-readable output: `qa/task-d/verification.json`
- Log output: `qa/task-d/test.log`

### Test Matrix Summary

| # | Case | Executed OK | Assertions Passed | Key Checks |
| --- | --- | --- | --- | --- |
| 1 | `1_start_before_result_and_calls_exist` | `true` | `true` | Starts cleanly with empty dir; reports waiting; no placeholder frame; no fake camera buttons; CSP/nosniff headers; no host path leaked |
| 2 | `2_non_swap_and_truncated_jsonl_tail` | `true` | `true` | Parses non-swap lines, safely ignores truncated trailing JSONL line, rejects unswapped `incomplete-surface-*.png` |
| 3 | `3_swap1_chunked_png_then_complete` | `true` | `true` | Rejects partial PNG missing `IEND` (`HTTP 409`, `png_status=incomplete_png`), serves exact raw bytes once write completes |
| 4 | `4_swap2_retains_prev_then_advances_and_downloads` | `true` | `true` | Retains swap #0 while swap #1 PNG is incomplete, advances in exact `[0, 1]` order once complete, download returns `Content-Disposition: attachment` + byte-identical PNG |
| 5 | `5_result_json_completion_and_xss_safety` | `true` | `true` | Loads `result.json`, preserves `menu_rendered: false` verbatim, static HTML shell contains zero XSS payload interpolation, API serves `application/json` + `nosniff` |
| 6 | `6_failure_exit_and_missing_png` | `true` | `true` | Surfaces `qemu_exit_code=1` and `graphics_error` as `run_state=failed`; missing PNG reported as `missing_file` and `HTTP 404` without crash |
| 7 | `7_security_traversal_symlinks_methods_loopback` | `true` | `true` | Rejects non-loopback hosts (`0.0.0.0`, `192.168.1.10`, `::`, `example.com`), raw/encoded traversal (`HTTP 400`), symlink escape (`HTTP 403`), and `POST`/`PUT`/`DELETE`/`PATCH` (`HTTP 405`) |
| 8 | `8_readonly_byte_invariance_on_sim_dir` | `true` | `true` | Recursive filename + byte-size + SHA-256 snapshot identical before and after full HTTP interaction |
| 9 | `9_real_corpus_local_requested_lcd_refresh_and_cli` | `true` | `true` | CLI subprocess binds loopback random port; all 5 real swap frames match disk byte-for-byte; `menu_rendered=false`; 75 source files untouched |
| 10 | `10_real_corpus_live_timezone_confirm_03` | `true` | `true` | All 13 real swap frames match disk byte-for-byte; unswapped `incomplete-surface-60000002.png` rejected (`HTTP 400`); `menu_rendered=false`; `lens_release_input` surfaced; 91 source files untouched |

## Real-Corpus Read-Only Verification Details

### 1. `menu-smp/results/local-requested-lcd-refresh` (5 real `software_swap` frames)

- **`menu_rendered`**: `false` (preserved verbatim)
- **`stop_reason`**: `time_bound` (`qemu_exit_code: 0`)
- **`original_ui_state_at_stop`**: `modal_id_08a92898 = 0x4a`, `render_scene_0b392e98 = 0x70de`
- **Source directory invariance**: 75 files checked before and after CLI run — 100% byte-identical (`source_dir_byte_identical_after_run: true`).
- Note: `calls.jsonl` in this directory retains original host paths from `/Users/lixiuqi/.../graphics/frame-000X.png`; the viewer safely extracts only the validated `frame-XXXX.png` basename and resolves strictly inside `<result-dir>/graphics/` without ever opening raw paths from the log.

| Swap Seq | Filename | Trace Index | Draw Calls | Dimensions | PNG Bytes | SHA-256 (prefix) | Byte-Identical to Disk |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | `frame-0000.png` | 696 | 15 | 1024×768 | 20624 | `441650dc9de75635` | `true` |
| 1 | `frame-0001.png` | 1289 | 31 | 1024×768 | 21421 | `4cc91201a32a93d8` | `true` |
| 2 | `frame-0002.png` | 3443 | 84 | 1024×768 | 69029 | `5716bb8ee9b24138` | `true` |
| 3 | `frame-0003.png` | 4476 | 98 | 1024×768 | 34369 | `ef76bb7730fa498f` | `true` |
| 4 | `frame-0004.png` | 4881 | 110 | 1024×768 | 34369 | `ef76bb7730fa498f` | `true` |

### 2. `qa/task-b/live-timezone-confirm-03` (13 real `software_swap` frames)

- **`menu_rendered`**: `false` (preserved verbatim)
- **`stop_reason`**: `time_bound` (`qemu_exit_code: 0`)
- **Frames verified**: `frame-0000.png` .. `frame-0012.png` (13 / 13 in chronological order, byte-identical to disk)
- **Unswapped surface guard**: `incomplete-surface-60000002.png` listed only under diagnostic metadata and rejected with `HTTP 400` if requested via `/api/frames/`.
- **Source directory invariance**: 91 files checked before and after — 100% byte-identical.

## Verified vs. Unclaimed / Remaining Assumptions

- **Verified**:
  - Loopback-only binding, random port allocation, and CLI non-loopback rejection.
  - Live incremental `calls.jsonl` tolerance (missing file, non-swap records, truncated trailing line) and chunked PNG validation (signature, `IHDR`, `IDAT`, CRCs, `IEND`) with previous-frame retention.
  - Exact raw PNG byte fidelity on display and download endpoints, strict whitelist enforcement against `software_swap` records, traversal/symlink/write-method rejection, and byte-level read-only invariance.
  - Clear separation of native UI swap frames, `result.json` boot status (`menu_rendered` preserved verbatim), and local HLE/TEST experiment scope; zero fake camera buttons; zero HTML string interpolation of untrusted JSON data.
- **Not claimed / remaining assumptions**:
  - Interactive browser rendering (CSS layout / JS runtime execution in a real browser engine) was not driven via headless Chrome/Playwright in this stdlib-only test suite; DOM XSS safety was verified via static inspection (no `innerHTML`/`outerHTML`/`insertAdjacentHTML`/`document.write`), strict CSP headers, and raw HTTP JSON/HTML responses.
  - The viewer does not claim main menu rendering or full camera boot (`menu_rendered` remains `false` on both real corpora).
