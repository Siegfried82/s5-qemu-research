# Task B Report: Reliable Key-Sequence Scheduler

## Scope & Isolation Compliance
- **Allowed write scope only**:
  - `menu-smp/key_sequence_scheduler.py` (new module)
  - `serial/test_key_sequence_scheduler.py` (new host-only behavioral test suite)
  - `qa/task-b/boot-integration.patch` (proposed unified diff against `menu-smp/boot.py`; `boot.py` itself is untouched)
  - `qa/task-b/report-task-b.md` (this report)
  - `qa/task-b/verification.json` & `qa/task-b/test-key-sequence-scheduler.log` (machine-readable verification & test output)
- **Untouched**: `menu-smp/boot.py`, `menu-smp/key_probe.py`, `qemu/`, `results/`, `qa/task-a/`.
- **No QEMU / no `toolchain/build.sh` run**: Host-only Python tests (`python3 -B`) executed under `TMPDIR=/Volumes/SATA-1TB/Projects/s5-qemu/tmp`.

---

## Original EGL Swap Boundary & Layout Sampling Contract

### 1. Exact Original EGL Swap Breakpoint
From `display/graphics-api-table.json` and `display/graphics_hle.py`:
- **API**: `eglSwapBuffers`
- **Entry breakpoint address**: `0x064c3cda` (Thumb pointer `0x064c3cdb`, original table `0x070d59d4`, relocated table `0x08ab501c`)
- **Handling path in `menu-smp/boot.py` (lines 518–531)**:
  1. Guest stops at `pc == 0x064c3cda` (registered in `graphics.breakpoints` by `GraphicsHLE.__init__`).
  2. `gfx_result = graphics.handle(pc, emu.registers())` calls `GraphicsHLE.egl("eglSwapBuffers", args)` -> `OpenVGBackend.swap(surface)`.
  3. `OpenVGBackend.swap()` increments `graphics.backend.swaps`, saves `frame-NNNN.png`, and appends a `"software_swap"` event containing `path`, `draw_calls`, and `pixels_sha256`.
  4. `graphics.handle()` sets `r0 = 1` and `pc = lr & ~1` and returns `{"api": "eglSwapBuffers", "result": 1, "software_driver": True}`.
  5. **Sampling point**: Immediately after `graphics.handle(...)` returns with `gfx_result["api"] == "eglSwapBuffers"`, **before** `emu.resume_all(...)` is called again. Because the guest CPU is still halted at this exact boundary and `GraphicsHLE` does not mutate layout RAM, reading guest RAM here captures the exact layout committed by that swap, and only when the swap itself succeeded without raising `UnsupportedVG`/`ValueError`.

### 2. Which Layout Is Authoritative at Swap Time (`0x0b392e90` vs `0x08cb10c8`)
From `display/inspect_render_wait.py`, `menu-smp/boot.py`, `serial/timezone-key-evidence.json`, and `serial/lens-warning-evidence.json`:
- **Source layout (`SOURCE_LAYOUT_ADDR = 0x08cb10c8`, 16 bytes; `+4` modal u16, `+8` scene u16)**:
  - Written by input/UI task285 (`0x06832c1c` / `0x06832cce`), then queued onto `queue 0x101` (`0x06833506` -> `0x067ea590`).
  - Reflects the *requested/pending* UI layout, which can advance ahead of the frame currently being rendered.
- **Render working copy (`RENDER_LAYOUT_ADDR = 0x0b392e90`, 16 bytes; `+4` modal u16, `+8` scene u16)**:
  - Copied from the `queue 0x101` payload by render task270 (`0x0727cc9c` -> `0x0727cac2` -> `0x0681475e`) and consumed by `0x0727c896` to issue the OpenVG draw calls ending in `eglSwapBuffers` (`0x064c3cda`).
- **Sampling & Matching Rule**:
  - At `eglSwapBuffers` (`0x064c3cda`), `read_layout_snapshot(emu.read_mem)` reads **both** `0x08cb10c8` (diagnostic `source_scene`/`source_modal`) and `0x0b392e90` (authoritative `render_scene`/`render_modal`) read-only.
  - Target matching (`StepTarget.evaluate`) checks **only** `render_scene` / `render_modal` (from `0x0b392e90`) on an **actual** `eglSwapBuffers` event with `swap_count > press_dispatch_swap_count` and `layout.captured == True`.
  - If `source_layout` already shows the new scene while `render_layout` at swap time still shows the old scene, the swap is explicitly rejected as `source_matched_but_render_scene_stale`.
  - Observing `source_layout == render_layout` in RAM via `observe_working_layout()` without an actual `eglSwapBuffers` event **never** counts as a swap and **never** satisfies a step target.
  - Observing an actual `eglSwapBuffers` without captured layout proof (`layout=None` or `layout.captured=False` or missing `render_scene`/`render_modal`) is explicitly rejected (`missing_layout_proof`) and **never** guessed.

---

## Event Wiring Summary (`qa/task-b/boot-integration.patch`)

| Observed Event | Boundary in `menu-smp/boot.py` | Scheduler Call |
|---|---|---|
| Original input readiness | Top of main `while` loop (`sample_key_input_readiness()`, read-only `read_input_route_readiness(emu.read_mem)`) | `key_scheduler.observe_input_readiness(...)` |
| Next key command | Top of main `while` loop when `not key_probe.active` | `cmd = key_scheduler.next_action()` -> `key_probe.arm(cmd.key, cmd.action, swaps_observed=..., **cmd.arm_options)` |
| Original press dispatch | `key_probe.handle(pc, index)` when `request["action"] == 1` and last event is `"original_key_dispatch"` (`KEY_DISPATCH = 0x064759c6`) | `key_scheduler.observe_press_dispatched(request["key"], swap_count=graphics.backend.swaps)` |
| Original press consumed | `key_probe.handle(pc, index)` when `not key_probe.active` and `request["consumed"]` (`action == 1`, `KEY_DISPATCH_RETURN = 0x064759ca`) | `key_scheduler.observe_press_consumed(request["key"])` |
| Original release consumed | `key_probe.handle(pc, index)` when `not key_probe.active` and `request["consumed"]` (`action == 0`, `KEY_DISPATCH_RETURN = 0x064759ca`) | `key_scheduler.observe_release_consumed(request["key"])` |
| Queue rejection / probe error | `not key_probe.active and not request["consumed"]`, or `KeyProbeError` | `key_scheduler.fail(...)` (reports `unsafe_release_unconfirmed` if press was armed/dispatched) |
| Actual EGL swap + layout proof | `graphics.handle(pc, regs)` returning `api == "eglSwapBuffers"` (`pc == 0x064c3cda`) | `key_scheduler.observe_swap(SwapObservation(swap_count=..., layout=read_layout_snapshot(emu.read_mem), frame_id=..., pixels_sha256=..., boundary_pc=0x064c3cda))` |
| Timeout / stop cleanup | `check_timeout()` on loop/timeout; `cancel(stop_reason)` on exit | Reports exact `pending_wait_reason` and `unsafe_release_unconfirmed` in `report["software_key_sequence"]` |

### Readiness Polling Note in `boot-integration.patch`
When `key_scheduler` is waiting in `waiting_input_ready` for a subsequent step (`current_index > 0`) and `key_probe` is not armed yet, no further graphics calls may occur while `secondary_stage` (`0x08aa090c`) settles. The patch bounds `emu.resume_all` to a 0.25s slice during that state and continues the loop on non-expired `TimeoutError` (while still strictly enforcing `key_scheduler.check_timeout()` and the outer `deadline`).

### Legacy CLI Compatibility
- `--key-event KEY --key-action 1` (press only): `KeySequenceScheduler.from_cli(...)` creates a 1-step `press_only` schedule; completes upon press consumed.
- `--key-event KEY --key-action 0` (release only): creates a 1-step `release_only` schedule; completes upon release consumed.
- `--key-event KEY --key-click`: creates a 1-step `click` schedule; completes immediately upon release consumed (no extra swap wait).
- `--key-sequence K1 K2 ...` without `--key-target`: defaults non-final steps to `StepTarget.legacy_captured_layout()`, which still requires an actual post-dispatch EGL swap with captured render layout (never fooled by working-layout-only updates or layoutless swaps) and explicitly records `legacy_fallback: true, target_semantics_verified: false`.
- `--key-sequence K1 K2 ... --key-target "scene=0x70de,modal=0x0" --key-target "same_scene,scene=0x70de,modal=0x0,pixels_change"`: parses explicit per-step targets via `parse_target_spec()`.

---

## Verified vs Unverified (Remaining Assumptions)

### Verified (Host-Only)
- All 15 behavioral unit tests in `serial/test_key_sequence_scheduler.py` executed and passed under `python3 -B`:
  - Stale old-scene swap arriving late (including `source_layout` already updated while `render_layout` still stale, and wrong modal `0x4a` overlaying scene `0x70de`).
  - Target scene swapping before release consumed (both before press consumed and after press consumed), while ensuring release still fires for the exact same key even when `input_ready` drops to `False`, and next press waits for release consumed without requiring an extra swap.
  - Working-layout-only updates (`source == render == target`) with no actual swap neither satisfy the target nor extend the phase deadline.
  - Actual swaps without layout proof (`layout=None`, `captured=False`, missing `render_scene`) keep waiting and never guess.
  - Input gate not ready blocks subsequent press even after target frame swapped.
  - Duplicate press dispatch, duplicate press consumed, duplicate release consumed, duplicate swaps, wrong-key notifications, and late duplicates across consecutive identical keys are idempotently rejected (at most 1 release per press, no double counting).
  - Multi-step different-key sequence including same-scene `KEY_DOWN` (`require_pixels_change`, `frame_predicate`, `expected_frame_id`), proving no older-frame backfill and explicit `selection_change_unverified_by_scene_alone: true` / `selection_change_proven_by_scene_alone: false`.
  - Final step finishes immediately upon release consumed (no extra swap wait).
  - Cancellation and failure across all lifecycle points accurately set `unsafe_release_unconfirmed` (`False` before press armed or after release consumed; `True` while press armed/dispatched/release-unconfirmed) and never emit a new press afterward.
  - Timeouts in all 5 distinct wait phases (`waiting_input_ready`, `waiting_press_dispatch`, `waiting_press_consumed`, `waiting_release_consumed`, `waiting_target_frame`).
  - Read-only guest RAM spy confirms zero writes and identical RAM digest.
  - Legacy single-key (`--key-event`, `--key-action`, `--key-click`), legacy `--key-sequence`, and explicit `--key-target` parsing.
- `qa/task-b/boot-integration.patch` verified via `patch --dry-run`, `patch` apply on a temporary copy, and `python3 -B -m py_compile`.

### Not Yet Verified (Requires Live QEMU After Integration)
- Live execution of `menu-smp/boot.py` with `qa/task-b/boot-integration.patch` applied against real S5 firmware under QEMU.
- Exact live timing of `0x0b392e90` updates relative to `eglSwapBuffers` (`0x064c3cda`) on multi-step sequences beyond the timezone confirmation (`0x1001/0x023c` -> `0x70de`).
- Whether `KEY_UP`/`KEY_DOWN` on target scenes produces deterministic pixel-hash changes (`pixels_sha256`) or requires a specific post-step UI selection-state observer in live firmware (the scheduler intentionally never claims same `scene` proves selection changed).
- Live interaction with `--lens-release-input-high` (Task A) removing modal `0x4a` before subsequent keys are dispatched.
