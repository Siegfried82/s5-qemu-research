# S5 Native Swap Frame Viewer (Read-Only)

Local, read-only web viewer for inspecting running or completed Panasonic LUMIX S5 QEMU emulator result directories (`menu-smp/boot.py` outputs).

## Design Principles & Boundaries

- **Pure Python 3 standard library**: Zero external packages, zero CDN/network fetches.
- **Strictly read-only**: Never creates, modifies, or deletes any file in `--result-dir`. HTTP write methods (`POST`, `PUT`, `DELETE`, `PATCH`) are rejected with `405 Method Not Allowed`.
- **Real `software_swap` PNGs only**: Only serves unmodified PNG bytes referenced by `name == "software_swap"` records in `graphics/calls.jsonl`. Unswapped `incomplete-surface-*.png` files are reported only as metadata and never rendered as frames; no placeholder camera screen is ever synthesized.
- **Live-run tolerant**: Can start before `result.json` or `graphics/calls.jsonl` exists, safely ignores incomplete trailing JSONL lines, and validates PNG signature + chunk structure + CRCs + `IEND` before exposing a frame (retaining the previous valid frame while waiting for the next complete PNG).
- **Clear scope separation**: Separates (1) native UI swap frames, (2) `result.json` run/full-boot status (always preserving `menu_rendered` verbatim, never upgrading based on swap count or visible text), and (3) local HLE/TEST experiment scope.
- **No fake camera controls**: Provides only frame browsing controls (First / Prev / Next / Latest, Pause/Resume Auto-Follow, Refresh, Download Raw PNG). No simulated camera buttons or input injection.
- **Local & safe**: Restricts `--host` to loopback (`127.0.0.1`, `::1`, or `localhost`), rejects path traversal and symlink escapes, never interpolates untrusted JSON strings into HTML (`textContent`-only DOM rendering + strict CSP), and avoids exposing host filesystem absolute paths over HTTP.

## Usage

From `/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator`:

```sh
python3 -B viewer/s5_frame_viewer.py --result-dir menu-smp/results/local-requested-lcd-refresh
```

This binds `127.0.0.1` on a random free port (`--port 0`) and prints the exact local URL, e.g.:

```text
S5 Native Frame Viewer (read-only) listening on http://127.0.0.1:54321/
```

Open that URL in a local browser. Press `Ctrl+C` to stop.

### CLI Options

| Flag | Default | Description |
| --- | --- | --- |
| `--result-dir <path>` | (required) | Path to a running or completed result directory |
| `--host <addr>` | `127.0.0.1` | Loopback bind address (`127.0.0.1`, `::1`, `localhost` only) |
| `--port <int>` | `0` | TCP port (`0` selects a random free port) |
| `--verbose` | off | Print HTTP request logs to stderr |

## HTTP API (Local Loopback Only)

- `GET /`: Self-contained read-only web UI.
- `GET /api/state`: Live JSON snapshot containing `viewer`, `native_frames`, `boot_status`, `experiment_scope`, and `warnings`.
- `GET /api/frames/<frame-XXXX.png>`: Exact raw PNG bytes for an authorized, complete `software_swap` frame.
- `GET /api/frames/<frame-XXXX.png>/download`: Exact raw PNG bytes with `Content-Disposition: attachment`.
