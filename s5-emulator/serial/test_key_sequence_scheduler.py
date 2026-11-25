#!/usr/bin/env python3
"""Host-only behavioral tests for menu-smp/key_sequence_scheduler.py.

These tests verify the scheduler contract via dependency injection (fake clock,
simulated KeyProbe/EGL-swap streams, and a strict read-only guest RAM spy).
They do not execute QEMU or claim firmware menu execution.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "menu-smp/key_sequence_scheduler.py"
SPEC = importlib.util.spec_from_file_location("key_sequence_scheduler", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class StrictReadOnlyGuestSpy:
    """Fake guest memory that traps ANY attempted write or mutation."""

    def __init__(self) -> None:
        self._mem = {}
        self.reads = []
        self.write_attempts = 0

    def seed_u32(self, addr: int, value: int) -> None:
        self._seed(addr, struct.pack("<I", value))

    def seed_u8(self, addr: int, value: int) -> None:
        self._seed(addr, bytes([value]))

    def seed_layout(self, base: int, *, modal: int, scene: int) -> None:
        raw = struct.pack("<IIHH4x", 0, modal, scene, 0)
        # Layout struct: +0 u32(0), +4 u16(modal), +6 u16(0), +8 u16(scene), +10..15 0
        raw = struct.pack("<IHHHI", 0, modal, 0, scene, 0)
        self._seed(base, raw)

    def _seed(self, addr: int, data: bytes) -> None:
        for i, byte in enumerate(data):
            self._mem[addr + i] = byte

    def snapshot_digest(self) -> str:
        items = sorted(self._mem.items())
        payload = b"".join(struct.pack("<IB", a, v) for a, v in items)
        return hashlib.sha256(payload).hexdigest()

    def read_mem(self, addr: int, size: int) -> bytes:
        self.reads.append((addr, size))
        return bytes(self._mem.get(addr + i, 0) for i in range(size))

    def write_mem(self, addr: int, data: bytes) -> None:
        self.write_attempts += 1
        raise AssertionError(f"Scheduler attempted forbidden guest RAM write at {hex(addr)}")

    def write_reg(self, name: str, value: int) -> None:
        self.write_attempts += 1
        raise AssertionError(f"Scheduler attempted forbidden guest register write {name}")


def make_layout(
    *,
    source_scene: int,
    source_modal: int,
    render_scene: int,
    render_modal: int,
    captured: bool = True,
) -> MOD.LayoutSnapshot:
    return MOD.LayoutSnapshot(
        source_scene=source_scene,
        source_modal=source_modal,
        render_scene=render_scene,
        render_modal=render_modal,
        captured=captured,
    )


def make_swap(
    swap_count: int,
    *,
    render_scene: int = 0x1001,
    render_modal: int = 0x023C,
    source_scene: int | None = None,
    source_modal: int | None = None,
    layout_captured: bool = True,
    omit_layout: bool = False,
    frame_id: str | None = None,
    pixels_sha256: str | None = None,
    metadata: dict | None = None,
) -> MOD.SwapObservation:
    if omit_layout:
        layout = None
    else:
        layout = make_layout(
            source_scene=render_scene if source_scene is None else source_scene,
            source_modal=render_modal if source_modal is None else source_modal,
            render_scene=render_scene,
            render_modal=render_modal,
            captured=layout_captured,
        )
    return MOD.SwapObservation(
        swap_count=swap_count,
        layout=layout,
        frame_id=frame_id or f"frame-{swap_count - 1:04d}.png",
        pixels_sha256=pixels_sha256 or f"sha-{swap_count:04d}",
        draw_calls=swap_count * 10,
        boundary_pc=MOD.EGL_SWAP_BUFFERS_PC,
        metadata=metadata or {},
    )


class KeySequenceSchedulerBehavioralTests(unittest.TestCase):
    def test_stale_old_scene_swap_arriving_late_does_not_advance_even_when_source_updated(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
            default_phase_timeout=10.0,
        )
        # Initial rendered frame is timezone prompt (scene 0x1001, modal 0x023c).
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        sched.observe_input_readiness(True)

        press0 = sched.next_action()
        self.assertIsNotNone(press0)
        self.assertEqual((press0.step_index, press0.key, press0.action), (0, "KEY_CENTER", 1))
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")

        rel0 = sched.next_action()
        self.assertEqual((rel0.step_index, rel0.key, rel0.action), (0, "KEY_CENTER", 0))

        # Stale queued frame from old scene swaps AFTER press dispatch, BEFORE release.
        sched.observe_swap(make_swap(2, render_scene=0x1001, render_modal=0x023C))
        sched.observe_release_consumed("KEY_CENTER")
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(sched.next_action())

        # Realistic S5 race: task285 updated source_layout (0x08cb10c8) to 0x70de/0x0,
        # but task270 just finished swapping an older frame with render_layout 0x1001/0x023c!
        out = sched.observe_swap(
            make_swap(
                3,
                source_scene=0x70DE,
                source_modal=0x0,
                render_scene=0x1001,
                render_modal=0x023C,
            )
        )
        self.assertEqual(out.details["reason"], "source_matched_but_render_scene_stale")
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(sched.next_action())

        # Scene switched to 0x70de, but lens warning modal 0x4a is still covering it!
        out_modal = sched.observe_swap(make_swap(4, render_scene=0x70DE, render_modal=0x4A))
        self.assertEqual(out_modal.details["reason"], "render_modal_mismatch")
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(sched.next_action())

        # Finally, real EGL swap commits render_scene=0x70de and render_modal=0x0.
        out_ok = sched.observe_swap(make_swap(5, render_scene=0x70DE, render_modal=0x0))
        self.assertTrue(out_ok.details["matched"])
        press1 = sched.next_action()
        self.assertIsNotNone(press1)
        self.assertEqual((press1.step_index, press1.key, press1.action), (1, "KEY_RIGHT", 1))
        self.assertEqual(len(sched.report()["steps"][0]["unmatched_post_dispatch_swaps"]), 3)

    def test_target_scene_swaps_before_release_consumed_still_completes_release_first(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
        )
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        sched.observe_input_readiness(True)

        self.assertEqual(sched.next_action().action, 1)
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        # Input gate drops to False because press triggered scene transition.
        sched.observe_input_readiness(False)
        sched.observe_press_consumed("KEY_CENTER")

        # Release MUST still be emitted for the exact same key even with input_ready=False.
        rel0 = sched.next_action()
        self.assertIsNotNone(rel0)
        self.assertEqual((rel0.step_index, rel0.key, rel0.action), (0, "KEY_CENTER", 0))
        self.assertEqual(rel0.arm_options, {})

        # Target scene swaps BEFORE release is consumed!
        sched.observe_swap(make_swap(2, render_scene=0x70DE, render_modal=0x0))
        # Must NOT arm step 1 while step 0 release is unconsumed.
        self.assertEqual(sched.current_index, 0)
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_RELEASE_CONSUMED)
        self.assertIsNone(sched.next_action())

        # Restore input readiness, then consume step 0 release -> step 1 arms immediately
        # without requiring any redundant 3rd swap!
        sched.observe_input_readiness(True)
        sched.observe_release_consumed("KEY_CENTER")
        self.assertEqual(sched.current_index, 1)
        self.assertTrue(sched.report()["steps"][0]["target_satisfied_before_release"])

        press1 = sched.next_action()
        self.assertIsNotNone(press1)
        self.assertEqual((press1.step_index, press1.key, press1.action), (1, "KEY_RIGHT", 1))

    def test_working_layout_match_without_swap_never_satisfies_target_or_extends_deadline(self):
        clock = FakeClock(50.0)
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
            phase_timeouts={MOD.WAIT_TARGET_FRAME: 4.0},
        )
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        sched.observe_input_readiness(True)
        sched.next_action()
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")
        sched.next_action()
        sched.observe_release_consumed("KEY_CENTER")

        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        # Source 0x08cb10c8 and render 0x0b392e90 both equal target in RAM, but NO swap occurred.
        for dt in (1.0, 1.5, 1.0):
            clock.advance(dt)
            res = sched.observe_working_layout(
                make_layout(
                    source_scene=0x70DE,
                    source_modal=0x0,
                    render_scene=0x70DE,
                    render_modal=0x0,
                )
            )
            self.assertFalse(res.details["satisfies_target"])
            self.assertEqual(sched.last_swap_count, 1)
            self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
            self.assertIsNone(sched.next_action())

        # Total elapsed = 3.5s < 4.0s; advancing another 0.6s must time out (no deadline extension!).
        clock.advance(0.6)
        with self.assertRaises(MOD.KeySequenceTimeoutError) as ctx:
            sched.check_timeout()
        self.assertEqual(ctx.exception.wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertFalse(ctx.exception.unsafe_release_unconfirmed)
        self.assertEqual(sched.report()["working_layout_observations"], 3)

    def test_actual_swap_without_layout_proof_keeps_waiting_and_never_guesses(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
            default_phase_timeout=5.0,
        )
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        sched.observe_input_readiness(True)
        sched.next_action()
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")
        sched.next_action()
        sched.observe_release_consumed("KEY_CENTER")

        # Swap 2: no layout object at all.
        out2 = sched.observe_swap(make_swap(2, omit_layout=True))
        self.assertFalse(out2.details["matched"])
        self.assertEqual(out2.details["reason"], "missing_layout_proof")
        self.assertIsNone(sched.next_action())

        # Swap 3: layout explicitly marked captured=False even though numeric fields match.
        out3 = sched.observe_swap(
            make_swap(3, render_scene=0x70DE, render_modal=0x0, layout_captured=False)
        )
        self.assertFalse(out3.details["matched"])
        self.assertEqual(out3.details["reason"], "missing_layout_proof")
        self.assertIsNone(sched.next_action())

        # Swap 4: captured=True but render_scene is None.
        out4 = sched.observe_swap(
            MOD.SwapObservation(
                swap_count=4,
                layout=MOD.LayoutSnapshot(
                    source_scene=0x70DE,
                    source_modal=0x0,
                    render_scene=None,
                    render_modal=0x0,
                    captured=True,
                ),
            )
        )
        self.assertFalse(out4.details["matched"])
        self.assertEqual(out4.details["reason"], "missing_render_scene_proof")
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(sched.next_action())

        # Swap 5: real captured layout satisfies target.
        out5 = sched.observe_swap(make_swap(5, render_scene=0x70DE, render_modal=0x0))
        self.assertTrue(out5.details["matched"])
        self.assertEqual(sched.next_action().key, "KEY_RIGHT")

    def test_input_gate_not_ready_blocks_next_press_even_after_target_frame_swapped(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
            default_phase_timeout=5.0,
        )
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        # Even before step 0, if input_readiness was never observed as True, step 0 waits.
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_INPUT_READY)
        self.assertIsNone(sched.next_action())

        sched.observe_input_readiness(MOD.InputReadiness(ready=True, input_route_ready=True))
        sched.next_action()
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")
        sched.next_action()
        sched.observe_release_consumed("KEY_CENTER")

        # Input gate is blocked (secondary_stage=2) when target frame swaps.
        sched.observe_input_readiness(
            MOD.InputReadiness(
                ready=False,
                input_route_ready=False,
                reason="input_route_not_ready",
                details={"secondary_stage": 2},
            )
        )
        sched.observe_swap(make_swap(2, render_scene=0x70DE, render_modal=0x0))
        self.assertEqual(sched.current_index, 1)
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_INPUT_READY)
        self.assertIsNone(sched.next_action())

        # Gate opens -> step 1 press is emitted.
        sched.observe_input_readiness(MOD.InputReadiness(ready=True, input_route_ready=True))
        cmd = sched.next_action()
        self.assertIsNotNone(cmd)
        self.assertEqual((cmd.step_index, cmd.key, cmd.action), (1, "KEY_RIGHT", 1))

    def test_duplicate_press_and_release_notifications_are_idempotently_rejected(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
        )
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        sched.observe_input_readiness(True)

        # Press emitted once; second next_action() returns None.
        self.assertEqual(sched.next_action().action, 1)
        self.assertIsNone(sched.next_action())

        # Press consumed BEFORE dispatch is rejected (no swap floor yet).
        early_cons = sched.observe_press_consumed("KEY_CENTER")
        self.assertFalse(early_cons.accepted)
        self.assertEqual(early_cons.reason, "press_not_dispatched")

        # Duplicate press dispatch rejected without overwriting swap floor.
        self.assertTrue(sched.observe_press_dispatched("KEY_CENTER", swap_count=1).accepted)
        dup_disp = sched.observe_press_dispatched("KEY_CENTER", swap_count=5)
        self.assertFalse(dup_disp.accepted)
        self.assertTrue(dup_disp.duplicate)
        self.assertEqual(sched.report()["steps"][0]["press_dispatch_swap_count"], 1)

        # Wrong-key press consumed rejected.
        wrong_key = sched.observe_press_consumed("KEY_RIGHT")
        self.assertFalse(wrong_key.accepted)
        self.assertEqual(wrong_key.reason, "key_mismatch")

        # Duplicate press consumed must emit ONLY ONE release.
        self.assertTrue(sched.observe_press_consumed("KEY_CENTER").accepted)
        dup_press = sched.observe_press_consumed("KEY_CENTER")
        self.assertFalse(dup_press.accepted)
        self.assertTrue(dup_press.duplicate)

        rel = sched.next_action()
        self.assertEqual((rel.key, rel.action), ("KEY_CENTER", 0))
        self.assertIsNone(sched.next_action())

        # Duplicate release consumed must NOT double-increment completed_steps.
        self.assertTrue(sched.observe_release_consumed("KEY_CENTER").accepted)
        dup_rel = sched.observe_release_consumed("KEY_CENTER")
        self.assertFalse(dup_rel.accepted)
        self.assertTrue(dup_rel.duplicate)
        self.assertEqual(sched.completed_steps, 1)

        # Duplicate swap count rejected.
        dup_swap = sched.observe_swap(make_swap(1, render_scene=0x70DE, render_modal=0x0))
        self.assertFalse(dup_swap.accepted)
        self.assertTrue(dup_swap.duplicate)

        # Complete step 1 cleanly and ensure late duplicate release after completion is ignored.
        sched.observe_swap(make_swap(2, render_scene=0x70DE, render_modal=0x0))
        self.assertEqual(sched.next_action().key, "KEY_RIGHT")
        sched.observe_press_dispatched("KEY_RIGHT", swap_count=2)
        sched.observe_press_consumed("KEY_RIGHT")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_RIGHT")
        self.assertTrue(sched.completed)

        late_dup = sched.observe_release_consumed("KEY_RIGHT")
        self.assertFalse(late_dup.accepted)
        self.assertTrue(late_dup.duplicate)
        self.assertEqual(sched.completed_steps, 2)
        self.assertEqual(sched.report()["steps"][0]["release_armed_count"], 1)
        self.assertEqual(sched.report()["steps"][1]["release_armed_count"], 1)

    def test_multi_step_different_keys_with_same_scene_down_no_backfill_and_honest_reporting(self):
        clock = FakeClock()
        predicate_calls = []

        def down_frame_predicate(swap: MOD.SwapObservation, ctx: dict) -> bool:
            predicate_calls.append((swap.swap_count, ctx["press_dispatch_swap_count"]))
            return swap.metadata.get("post_down_tag") == "highlight_moved_candidate"

        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep(
                    "KEY_DOWN",
                    target=MOD.StepTarget.same_scene(
                        expected_scene=0x70DE,
                        expected_modal=0x0,
                        require_pixels_change=True,
                        frame_predicate=down_frame_predicate,
                        description="post-DOWN redraw in scene 0x70de",
                    ),
                ),
                MOD.KeyStep(
                    "KEY_RIGHT",
                    target=MOD.StepTarget.scene_modal(scene=0x70DF, modal=0x0),
                ),
                MOD.KeyStep("KEY_CENTER"),
            ],
            clock=clock,
        )
        # Initial swap 1 (timezone prompt).
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C, pixels_sha256="px1"))
        sched.observe_input_readiness(True)

        # Step 0: KEY_CENTER -> transitions to 0x70de / modal 0x0.
        self.assertEqual(sched.next_action().key, "KEY_CENTER")
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_CENTER")
        sched.observe_swap(
            make_swap(
                2,
                render_scene=0x70DE,
                render_modal=0x0,
                pixels_sha256="px_70de_initial",
                # Even if an earlier frame carried the tag, it cannot backfill step 1!
                metadata={"post_down_tag": "highlight_moved_candidate"},
            )
        )

        # Step 1: KEY_DOWN (same scene 0x70de).
        cmd1 = sched.next_action()
        self.assertEqual((cmd1.step_index, cmd1.key, cmd1.action), (1, "KEY_DOWN", 1))
        sched.observe_press_dispatched("KEY_DOWN", swap_count=2)
        sched.observe_press_consumed("KEY_DOWN")
        self.assertEqual(sched.next_action().key, "KEY_DOWN")
        sched.observe_release_consumed("KEY_DOWN")
        # Swap 2 happened BEFORE/AT press dispatch -> MUST NOT backfill!
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(sched.next_action())

        # Swap 3: post-dispatch swap in same scene 0x70de, but pixels UNCHANGED!
        out3 = sched.observe_swap(
            make_swap(
                3,
                render_scene=0x70DE,
                render_modal=0x0,
                pixels_sha256="px_70de_initial",
                metadata={"post_down_tag": "highlight_moved_candidate"},
            )
        )
        self.assertFalse(out3.details["matched"])
        self.assertEqual(out3.details["reason"], "pixels_unchanged")
        self.assertIsNone(sched.next_action())

        # Swap 4: pixels changed, but caller predicate not yet matched.
        out4 = sched.observe_swap(
            make_swap(
                4,
                render_scene=0x70DE,
                render_modal=0x0,
                pixels_sha256="px_70de_changed",
                metadata={"post_down_tag": "unrelated_redraw"},
            )
        )
        self.assertFalse(out4.details["matched"])
        self.assertEqual(out4.details["reason"], "frame_predicate_not_satisfied")

        # Swap 5: pixels changed AND caller predicate matched!
        out5 = sched.observe_swap(
            make_swap(
                5,
                render_scene=0x70DE,
                render_modal=0x0,
                pixels_sha256="px_70de_down_1",
                metadata={"post_down_tag": "highlight_moved_candidate"},
            )
        )
        self.assertTrue(out5.details["matched"])
        self.assertTrue(out5.details["selection_change_unverified_by_scene_alone"])
        self.assertFalse(out5.details["selection_change_proven_by_scene_alone"])
        self.assertEqual(predicate_calls, [(4, 2), (5, 2)])

        # Step 2: KEY_RIGHT -> transitions to 0x70df / modal 0x0.
        cmd2 = sched.next_action()
        self.assertEqual((cmd2.step_index, cmd2.key, cmd2.action), (2, "KEY_RIGHT", 1))
        sched.observe_press_dispatched("KEY_RIGHT", swap_count=5)
        sched.observe_press_consumed("KEY_RIGHT")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_RIGHT")
        sched.observe_swap(make_swap(6, render_scene=0x70DF, render_modal=0x0))

        # Step 3 (final): KEY_CENTER -> finishes right after release consumed (NO swap 7 wait!).
        cmd3 = sched.next_action()
        self.assertEqual((cmd3.step_index, cmd3.key, cmd3.action), (3, "KEY_CENTER", 1))
        sched.observe_press_dispatched("KEY_CENTER", swap_count=6)
        sched.observe_press_consumed("KEY_CENTER")
        rel3 = sched.next_action()
        self.assertEqual((rel3.step_index, rel3.key, rel3.action), (3, "KEY_CENTER", 0))
        sched.observe_release_consumed("KEY_CENTER")

        self.assertTrue(sched.completed)
        self.assertFalse(sched.active)
        self.assertIsNone(sched.pending_wait_reason)
        self.assertEqual(sched.completed_steps, 4)
        self.assertFalse(sched.unsafe_release_unconfirmed)
        self.assertIsNone(sched.next_action())

        # Advancing clock far past timeout after completion must NOT raise.
        clock.advance(999.0)
        sched.check_timeout()

        rep = sched.report()
        self.assertFalse(rep["menu_opened_verified_by_scheduler"])
        self.assertTrue(rep["steps"][1]["target_verification"]["selection_change_unverified_by_scene_alone"])

    def test_final_step_ignores_extra_target_and_finishes_without_extra_swap(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                )
            ],
            clock=clock,
        )
        sched.observe_swap(make_swap(1))
        sched.observe_input_readiness(True)
        sched.next_action()
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")
        sched.next_action()
        sched.observe_release_consumed("KEY_CENTER")

        self.assertTrue(sched.completed)
        self.assertIsNone(sched.pending_wait_reason)
        self.assertTrue(sched.report()["steps"][0]["final_step_target_ignored"])

    def test_cancellation_across_all_lifecycle_points_reports_release_safety_accurately(self):
        # 1. Cancel before start (no press armed): safe.
        s1 = MOD.KeySequenceScheduler([MOD.KeyStep("KEY_CENTER")])
        rep1 = s1.cancel("abort_before_start")
        self.assertEqual(s1.status, "cancelled")
        self.assertFalse(s1.unsafe_release_unconfirmed)
        self.assertFalse(rep1["unsafe_release_unconfirmed"])
        s1.observe_swap(make_swap(1))
        s1.observe_input_readiness(True)
        self.assertIsNone(s1.next_action())

        # 2. Cancel after press armed (before dispatch): unsafe_release_unconfirmed=True.
        s2 = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep("KEY_CENTER", target=MOD.StepTarget.scene_modal(scene=0x70DE)),
                MOD.KeyStep("KEY_RIGHT"),
            ]
        )
        s2.observe_swap(make_swap(1))
        s2.observe_input_readiness(True)
        self.assertEqual(s2.next_action().action, 1)
        rep2 = s2.cancel("abort_while_press_armed")
        self.assertTrue(s2.unsafe_release_unconfirmed)
        self.assertEqual(rep2["unconfirmed_release_details"]["key"], "KEY_CENTER")
        self.assertIsNone(s2.next_action())

        # 3. Cancel after press dispatched and release armed (before release consumed): unsafe=True.
        s3 = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep("KEY_CENTER", target=MOD.StepTarget.scene_modal(scene=0x70DE)),
                MOD.KeyStep("KEY_RIGHT"),
            ]
        )
        s3.observe_swap(make_swap(1))
        s3.observe_input_readiness(True)
        s3.next_action()
        s3.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s3.observe_press_consumed("KEY_CENTER")
        s3.next_action()  # release armed
        rep3 = s3.fail("key_probe_error:timeout_call_incomplete")
        self.assertEqual(s3.status, "failed")
        self.assertTrue(s3.unsafe_release_unconfirmed)
        self.assertTrue(rep3["unconfirmed_release_details"]["release_armed"])
        self.assertFalse(rep3["unconfirmed_release_details"]["release_consumed"])
        self.assertIsNone(s3.next_action())

        # 4. Cancel AFTER release consumed while waiting for target frame: safe!
        s4 = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep("KEY_CENTER", target=MOD.StepTarget.scene_modal(scene=0x70DE)),
                MOD.KeyStep("KEY_RIGHT"),
            ]
        )
        s4.observe_swap(make_swap(1))
        s4.observe_input_readiness(True)
        s4.next_action()
        s4.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s4.observe_press_consumed("KEY_CENTER")
        s4.next_action()
        s4.observe_release_consumed("KEY_CENTER")
        self.assertEqual(s4.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        rep4 = s4.cancel("abort_during_target_wait")
        self.assertFalse(s4.unsafe_release_unconfirmed)
        self.assertIsNone(rep4["unconfirmed_release_details"])
        # Even if target later swaps, no new press may ever be emitted.
        s4.observe_swap(make_swap(2, render_scene=0x70DE, render_modal=0x0))
        self.assertIsNone(s4.next_action())

    def test_timeouts_in_every_distinct_wait_phase_report_exact_reason(self):
        timeouts = {
            MOD.WAIT_INPUT_READY: 2.0,
            MOD.WAIT_PRESS_DISPATCH: 3.0,
            MOD.WAIT_PRESS_CONSUMED: 4.0,
            MOD.WAIT_RELEASE_CONSUMED: 5.0,
            MOD.WAIT_TARGET_FRAME: 6.0,
        }

        def make_sched(clock: FakeClock) -> MOD.KeySequenceScheduler:
            return MOD.KeySequenceScheduler(
                [
                    MOD.KeyStep(
                        "KEY_CENTER",
                        target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                    ),
                    MOD.KeyStep("KEY_RIGHT"),
                ],
                clock=clock,
                phase_timeouts=timeouts,
            )

        # Phase 1: waiting_input_ready
        c = FakeClock(0.0)
        s = make_sched(c)
        self.assertAlmostEqual(s.timeout_hint(), 2.0)
        c.advance(2.1)
        with self.assertRaises(MOD.KeySequenceTimeoutError) as ctx:
            s.check_timeout()
        self.assertEqual(ctx.exception.wait_reason, MOD.WAIT_INPUT_READY)
        self.assertFalse(ctx.exception.unsafe_release_unconfirmed)
        self.assertIsNone(s.next_action())

        # Phase 2: waiting_press_dispatch
        c = FakeClock(0.0)
        s = make_sched(c)
        s.observe_swap(make_swap(1))
        s.observe_input_readiness(True)
        s.next_action()
        self.assertEqual(s.pending_wait_reason, MOD.WAIT_PRESS_DISPATCH)
        c.advance(3.1)
        with self.assertRaises(MOD.KeySequenceTimeoutError) as ctx:
            s.check_timeout()
        self.assertEqual(ctx.exception.wait_reason, MOD.WAIT_PRESS_DISPATCH)
        self.assertTrue(ctx.exception.unsafe_release_unconfirmed)

        # Phase 3: waiting_press_consumed
        c = FakeClock(0.0)
        s = make_sched(c)
        s.observe_swap(make_swap(1))
        s.observe_input_readiness(True)
        s.next_action()
        s.observe_press_dispatched("KEY_CENTER", swap_count=1)
        self.assertEqual(s.pending_wait_reason, MOD.WAIT_PRESS_CONSUMED)
        c.advance(4.1)
        with self.assertRaises(MOD.KeySequenceTimeoutError) as ctx:
            s.check_timeout()
        self.assertEqual(ctx.exception.wait_reason, MOD.WAIT_PRESS_CONSUMED)
        self.assertTrue(ctx.exception.unsafe_release_unconfirmed)

        # Phase 4: waiting_release_consumed
        c = FakeClock(0.0)
        s = make_sched(c)
        s.observe_swap(make_swap(1))
        s.observe_input_readiness(True)
        s.next_action()
        s.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s.observe_press_consumed("KEY_CENTER")
        s.next_action()  # release armed
        self.assertEqual(s.pending_wait_reason, MOD.WAIT_RELEASE_CONSUMED)
        c.advance(5.1)
        with self.assertRaises(MOD.KeySequenceTimeoutError) as ctx:
            s.check_timeout()
        self.assertEqual(ctx.exception.wait_reason, MOD.WAIT_RELEASE_CONSUMED)
        self.assertTrue(ctx.exception.unsafe_release_unconfirmed)

        # Phase 5: waiting_target_frame
        c = FakeClock(0.0)
        s = make_sched(c)
        s.observe_swap(make_swap(1))
        s.observe_input_readiness(True)
        s.next_action()
        s.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s.observe_press_consumed("KEY_CENTER")
        s.next_action()
        s.observe_release_consumed("KEY_CENTER")
        self.assertEqual(s.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        c.advance(3.0)
        # Unmatched swap midway must NOT reset the 6.0s phase deadline.
        s.observe_swap(make_swap(2, render_scene=0x1001, render_modal=0x023C))
        c.advance(3.1)
        with self.assertRaises(MOD.KeySequenceTimeoutError) as ctx:
            s.check_timeout()
        self.assertEqual(ctx.exception.wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertFalse(ctx.exception.unsafe_release_unconfirmed)
        self.assertEqual(s.report()["timeout_details"]["wait_reason"], MOD.WAIT_TARGET_FRAME)

    def test_strict_read_only_guest_ram_guarantee(self):
        spy = StrictReadOnlyGuestSpy()
        spy.seed_layout(MOD.SOURCE_LAYOUT_ADDR, modal=0x023C, scene=0x1001)
        spy.seed_layout(MOD.RENDER_LAYOUT_ADDR, modal=0x023C, scene=0x1001)
        spy.seed_u32(MOD.UI_STATE_ADDR, 1)
        spy.seed_u32(MOD.UI_MODE_ADDR, 1)
        spy.seed_u8(MOD.MODE35_ACK_MASK_ADDR, 0xFF)
        spy.seed_u32(MOD.TRANSITION_STEP_ADDR, 0x4A0)
        spy.seed_u32(MOD.PRIMARY_STAGE_ADDR, 0)
        spy.seed_u32(MOD.PENDING_MODE_ADDR, 0xFF)
        spy.seed_u32(MOD.SECONDARY_STAGE_ADDR, 1)
        digest_before = spy.snapshot_digest()

        snap = MOD.read_layout_snapshot(spy.read_mem)
        self.assertEqual(snap.source_scene, 0x1001)
        self.assertEqual(snap.source_modal, 0x023C)
        self.assertEqual(snap.render_scene, 0x1001)
        self.assertEqual(snap.render_modal, 0x023C)

        readiness = MOD.read_input_route_readiness(
            spy.read_mem,
            has_initial_swap=True,
            require_ui_active=True,
            require_mode35_ack=True,
            require_transition_complete=True,
            require_input_route_ready=True,
        )
        self.assertTrue(readiness.ready)

        sched = MOD.KeySequenceScheduler(
            [MOD.KeyStep("KEY_CENTER")],
            input_readiness_probe=lambda: MOD.read_input_route_readiness(
                spy.read_mem,
                has_initial_swap=True,
                require_input_route_ready=True,
            ),
        )
        sched.observe_swap(MOD.SwapObservation(swap_count=1, layout=snap))
        self.assertEqual(sched.next_action().action, 1)
        sched.observe_press_dispatched("KEY_CENTER", swap_count=1)
        sched.observe_press_consumed("KEY_CENTER")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_CENTER")

        self.assertTrue(sched.completed)
        self.assertEqual(spy.write_attempts, 0)
        self.assertEqual(spy.snapshot_digest(), digest_before)
        self.assertGreater(len(spy.reads), 0)
        self.assertFalse(sched.report()["guest_ram_or_state_mutated_by_scheduler"])

    def test_legacy_single_key_and_legacy_sequence_compatibility(self):
        # 1. --key-event KEY_CENTER --key-action 1 (press-only, no --key-click)
        s_press = MOD.KeySequenceScheduler.from_cli(
            key_event="KEY_CENTER",
            key_action=1,
            key_click=False,
            initial_arm_options={"wait_for_ui_active": True},
        )
        s_press.observe_swap(make_swap(1))
        s_press.observe_input_readiness(True)
        cmd = s_press.next_action()
        self.assertEqual((cmd.key, cmd.action), ("KEY_CENTER", 1))
        self.assertEqual(cmd.arm_options, {"wait_for_ui_active": True})
        s_press.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s_press.observe_press_consumed("KEY_CENTER")
        self.assertTrue(s_press.completed)
        self.assertIsNone(s_press.next_action())

        # 2. --key-event KEY_CENTER --key-action 0 (release-only)
        s_rel = MOD.KeySequenceScheduler.from_cli(
            key_event="KEY_CENTER",
            key_action=0,
            key_click=False,
        )
        s_rel.observe_swap(make_swap(1))
        s_rel.observe_input_readiness(True)
        cmd = s_rel.next_action()
        self.assertEqual((cmd.key, cmd.action), ("KEY_CENTER", 0))
        s_rel.observe_release_consumed("KEY_CENTER")
        self.assertTrue(s_rel.completed)

        # 3. --key-event KEY_CENTER --key-click
        s_click = MOD.KeySequenceScheduler.from_cli(
            key_event="KEY_CENTER",
            key_action=1,
            key_click=True,
        )
        s_click.observe_swap(make_swap(1))
        s_click.observe_input_readiness(True)
        self.assertEqual(s_click.next_action().action, 1)
        s_click.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s_click.observe_press_consumed("KEY_CENTER")
        self.assertEqual(s_click.next_action().action, 0)
        s_click.observe_release_consumed("KEY_CENTER")
        self.assertTrue(s_click.completed)

        # 4. Legacy --key-sequence without explicit --key-target uses safe captured-layout fallback
        s_legacy_seq = MOD.KeySequenceScheduler.from_cli(
            key_sequence=["KEY_CENTER", "KEY_RIGHT"],
        )
        s_legacy_seq.observe_swap(make_swap(1))
        s_legacy_seq.observe_input_readiness(True)
        s_legacy_seq.next_action()
        s_legacy_seq.observe_press_dispatched("KEY_CENTER", swap_count=1)
        s_legacy_seq.observe_press_consumed("KEY_CENTER")
        s_legacy_seq.next_action()
        s_legacy_seq.observe_release_consumed("KEY_CENTER")
        # Layoutless swap STILL rejected even in legacy fallback mode!
        s_legacy_seq.observe_swap(make_swap(2, omit_layout=True))
        self.assertEqual(s_legacy_seq.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(s_legacy_seq.next_action())
        # Swap with captured render layout satisfies legacy fallback and flags semantics unverified.
        out = s_legacy_seq.observe_swap(make_swap(3, render_scene=0x70DE, render_modal=0x0))
        self.assertTrue(out.details["matched"])
        self.assertTrue(out.details["legacy_fallback"])
        self.assertFalse(out.details["target_semantics_verified"])
        self.assertEqual(s_legacy_seq.next_action().key, "KEY_RIGHT")

        # 5. CLI --key-sequence with explicit --key-target strings
        s_targeted = MOD.KeySequenceScheduler.from_cli(
            key_sequence=["KEY_CENTER", "KEY_DOWN", "KEY_RIGHT"],
            key_targets=[
                "scene=0x70de,modal=0x0",
                "same_scene,scene=0x70de,modal=0x0,pixels_change",
            ],
        )
        self.assertEqual(s_targeted.steps[0].target.expected_scene, 0x70DE)
        self.assertEqual(s_targeted.steps[0].target.expected_modal, 0x0)
        self.assertTrue(s_targeted.steps[1].target.same_scene_frame)
        self.assertTrue(s_targeted.steps[1].target.require_pixels_change)
        self.assertIsNone(s_targeted.steps[2].target)

    def test_swap_before_press_dispatch_cannot_satisfy_target_even_if_layout_matches(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_CENTER",
                    target=MOD.StepTarget.scene_modal(scene=0x70DE, modal=0x0),
                ),
                MOD.KeyStep("KEY_RIGHT"),
            ],
            clock=clock,
        )
        sched.observe_swap(make_swap(1, render_scene=0x1001, render_modal=0x023C))
        sched.observe_input_readiness(True)

        # Press is armed on KeyProbe, but NOT yet dispatched by original firmware!
        self.assertEqual(sched.next_action().action, 1)
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_PRESS_DISPATCH)

        # A swap matching 0x70de arrives BEFORE original press dispatch (e.g. from prior work).
        sched.observe_swap(make_swap(2, render_scene=0x70DE, render_modal=0x0))
        # Press finally dispatches at swap_count=2.
        sched.observe_press_dispatched("KEY_CENTER", swap_count=2)
        sched.observe_press_consumed("KEY_CENTER")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_CENTER")

        # Swap 2 happened BEFORE press dispatch, so scheduler MUST still wait for swap > 2!
        self.assertEqual(sched.current_index, 0)
        self.assertEqual(sched.pending_wait_reason, MOD.WAIT_TARGET_FRAME)
        self.assertIsNone(sched.next_action())

        # Swap 3 after press dispatch satisfies target.
        sched.observe_swap(make_swap(3, render_scene=0x70DE, render_modal=0x0))
        self.assertEqual(sched.next_action().key, "KEY_RIGHT")

    def test_consecutive_same_key_down_uses_post_step_frame_id_without_backfill(self):
        clock = FakeClock()
        sched = MOD.KeySequenceScheduler(
            [
                MOD.KeyStep(
                    "KEY_DOWN",
                    target=MOD.StepTarget.same_scene(
                        expected_scene=0x70DE,
                        expected_modal=0x0,
                        expected_frame_id="frame-0002.png",
                    ),
                ),
                MOD.KeyStep("KEY_DOWN"),
            ],
            clock=clock,
        )
        # Swap 1 already has scene 0x70de, frame-0000.png; Swap 2 has frame-0001.png.
        sched.observe_swap(make_swap(1, render_scene=0x70DE, render_modal=0x0, frame_id="frame-0000.png"))
        sched.observe_swap(make_swap(2, render_scene=0x70DE, render_modal=0x0, frame_id="frame-0001.png"))
        sched.observe_input_readiness(True)

        self.assertEqual(sched.next_action().key, "KEY_DOWN")
        sched.observe_press_dispatched("KEY_DOWN", swap_count=2)
        # Target swap arrives even BEFORE press consumed -> recorded, but release still runs first.
        out_early = sched.observe_swap(
            make_swap(3, render_scene=0x70DE, render_modal=0x0, frame_id="frame-0002.png")
        )
        self.assertTrue(out_early.details["matched"])
        self.assertTrue(out_early.details["selection_change_unverified_by_scene_alone"])
        self.assertEqual(sched.current_index, 0)

        sched.observe_press_consumed("KEY_DOWN")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_DOWN")

        # Now on step 1 (also KEY_DOWN); late duplicate press/release from step 0 rejected.
        self.assertEqual(sched.current_index, 1)
        dup_p = sched.observe_press_consumed("KEY_DOWN")
        self.assertFalse(dup_p.accepted)
        self.assertTrue(dup_p.duplicate)
        dup_r = sched.observe_release_consumed("KEY_DOWN")
        self.assertFalse(dup_r.accepted)
        self.assertTrue(dup_r.duplicate)

        # Step 1 proceeds cleanly and completes immediately on release consumed.
        self.assertEqual(sched.next_action().action, 1)
        sched.observe_press_dispatched("KEY_DOWN", swap_count=3)
        sched.observe_press_consumed("KEY_DOWN")
        self.assertEqual(sched.next_action().action, 0)
        sched.observe_release_consumed("KEY_DOWN")
        self.assertTrue(sched.completed)

    def test_validation_rejects_missing_non_final_target_and_invalid_inputs(self):
        with self.assertRaises(ValueError):
            MOD.KeySequenceScheduler([])
        with self.assertRaises(ValueError):
            MOD.KeySequenceScheduler([MOD.KeyStep("KEY_CENTER"), MOD.KeyStep("KEY_RIGHT")])
        with self.assertRaises(ValueError):
            MOD.KeySequenceScheduler([MOD.KeyStep("POWER_OFF")])
        with self.assertRaises(ValueError):
            MOD.KeySequenceScheduler.for_single_key("KEY_CENTER", action=0, click=True)
        with self.assertRaises(ValueError):
            MOD.KeySequenceScheduler.from_cli(key_sequence=["KEY_CENTER"] * 17)
        with self.assertRaises(ValueError):
            MOD.parse_target_spec("same_scene")  # needs at least one concrete criterion


if __name__ == "__main__":
    unittest.main(verbosity=2)
