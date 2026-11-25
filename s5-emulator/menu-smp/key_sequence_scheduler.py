#!/usr/bin/env python3
"""Reliable, dependency-injected key-sequence scheduler for S5 QEMU probes.

This module is pure host-side scheduling logic: it never launches QEMU, never
patches firmware, and never mutates guest RAM, registers, TCBs, event flags, or
UI state. All guest observations are supplied explicitly by the caller (or via
read-only memory-reader callbacks).

Core invariants:
1. Every press produces at most one matching release for the exact same key;
   release is allowed even after the press switches scene/modal or drops the
   input-ready gate.
2. Every subsequent press requires BOTH:
   - explicit input-ready gate satisfaction, AND
   - the previous step's explicit completion target proven by an *actual* EGL
     swap observed strictly after that step's original press dispatch.
3. Working-layout updates in RAM (source 0x08cb10c8 / render 0x0b392e90) without
   an actual EGL swap NEVER satisfy a step target.
4. An actual EGL swap without captured layout proof NEVER satisfies a scene or
   modal target (the scheduler keeps waiting rather than guessing).
5. Same-scene steps (such as UP/DOWN) may use a post-step frame criterion, never
   backfill older frames, and explicitly record that scene equality alone does
   not prove selection changed.
6. The sequence finishes immediately after the final step's release is consumed
   without waiting for an extra swap.
7. Cancellation, failure, or timeout never emits a new press and explicitly
   reports ``unsafe_release_unconfirmed=True`` whenever a press was armed or
   dispatched without a confirmed matching release.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import struct
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union


# Exact canonical key allowlist matching menu-smp/key_probe.py KEYS.
SUPPORTED_KEYS: Tuple[str, ...] = (
    "KEY_UP",
    "KEY_DOWN",
    "KEY_LEFT",
    "KEY_RIGHT",
    "KEY_CENTER",
    "KEY_PLAY",
    "KEY_DISPLAY",
    "KEY_DELETE",
    "KEY_Q_MENU",
    "KEY_ISO",
    "KEY_WB",
    "KEY_EXPOSE",
    "CONTROL_DIAL_LEFT",
    "CONTROL_DIAL_RIGHT",
    "FRONT_DIAL_LEFT",
    "FRONT_DIAL_RIGHT",
    "REAR_DIAL_LEFT",
    "REAR_DIAL_RIGHT",
    "KEY_SET",
)
_SHORT_KEY_ALIASES: Dict[str, str] = {
    "UP": "KEY_UP",
    "DOWN": "KEY_DOWN",
    "LEFT": "KEY_LEFT",
    "RIGHT": "KEY_RIGHT",
    "CENTER": "KEY_CENTER",
    "PLAY": "KEY_PLAY",
    "Q_MENU": "KEY_Q_MENU",
    "SET": "KEY_SET",
}
MAX_SEQUENCE_LENGTH = 16

# Original firmware guest addresses (read-only observation only).
SOURCE_LAYOUT_ADDR = 0x08CB10C8
RENDER_LAYOUT_ADDR = 0x0B392E90
LAYOUT_MODAL_OFFSET = 4
LAYOUT_SCENE_OFFSET = 8
LAYOUT_SIZE = 16
EGL_SWAP_BUFFERS_PC = 0x064C3CDA

# Original read-only input-gate addresses matching menu-smp/key_probe.py.
UI_STATE_ADDR = 0x08A928B4
UI_MODE_ADDR = 0x08A928BC
MODE35_ACK_MASK_ADDR = 0x08AA1103
TRANSITION_STEP_ADDR = 0x08CB0EE0
PRIMARY_STAGE_ADDR = 0x08AA1120
PENDING_MODE_ADDR = 0x08AA1124
SECONDARY_STAGE_ADDR = 0x08AA090C

# Canonical wait-phase identifiers reported on status and timeout.
WAIT_INPUT_READY = "waiting_input_ready"
WAIT_PRESS_DISPATCH = "waiting_press_dispatch"
WAIT_PRESS_CONSUMED = "waiting_press_consumed"
WAIT_RELEASE_CONSUMED = "waiting_release_consumed"
WAIT_TARGET_FRAME = "waiting_target_frame"

ALL_WAIT_REASONS: Tuple[str, ...] = (
    WAIT_INPUT_READY,
    WAIT_PRESS_DISPATCH,
    WAIT_PRESS_CONSUMED,
    WAIT_RELEASE_CONSUMED,
    WAIT_TARGET_FRAME,
)


def normalize_key(key: str) -> str:
    """Validate and canonicalize a key name against the original allowlist."""
    if not isinstance(key, str):
        raise ValueError(f"Key must be a string, got {type(key).__name__}")
    canonical = _SHORT_KEY_ALIASES.get(key, key)
    if canonical not in SUPPORTED_KEYS:
        raise ValueError(f"Unsupported key {key!r}; allowed: {', '.join(SUPPORTED_KEYS)}")
    return canonical


class KeySequenceError(RuntimeError):
    """Base error for key-sequence scheduler failures."""


class KeySequenceTimeoutError(KeySequenceError):
    """Raised when a bounded scheduler wait phase expires."""

    def __init__(
        self,
        wait_reason: str,
        *,
        step_index: int,
        key: str,
        unsafe_release_unconfirmed: bool,
        diagnostics: Dict[str, Any],
    ) -> None:
        self.wait_reason = wait_reason
        self.step_index = step_index
        self.key = key
        self.unsafe_release_unconfirmed = unsafe_release_unconfirmed
        self.diagnostics = diagnostics
        suffix = " (unsafe_release_unconfirmed=True)" if unsafe_release_unconfirmed else ""
        super().__init__(
            f"Key sequence timed out at step {step_index} ({key}): {wait_reason}{suffix}"
        )


def _hex_or_none(value: Optional[int]) -> Optional[str]:
    return hex(value) if value is not None else None


@dataclass(frozen=True)
class LayoutSnapshot:
    """Read-only observation of original source (0x08cb10c8) and render (0x0b392e90) layouts."""

    source_scene: Optional[int] = None
    source_modal: Optional[int] = None
    render_scene: Optional[int] = None
    render_modal: Optional[int] = None
    source_raw_hex: Optional[str] = None
    render_raw_hex: Optional[str] = None
    captured: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "captured": self.captured,
            "source_scene": _hex_or_none(self.source_scene),
            "source_modal": _hex_or_none(self.source_modal),
            "render_scene": _hex_or_none(self.render_scene),
            "render_modal": _hex_or_none(self.render_modal),
            "source_raw_hex": self.source_raw_hex,
            "render_raw_hex": self.render_raw_hex,
            "working_copy_matches_source": (
                self.captured
                and self.source_scene is not None
                and self.render_scene is not None
                and self.source_scene == self.render_scene
                and self.source_modal == self.render_modal
            ),
        }


@dataclass(frozen=True)
class SwapObservation:
    """Observation recorded at an actual original eglSwapBuffers boundary."""

    swap_count: int
    layout: Optional[LayoutSnapshot] = None
    frame_id: Optional[str] = None
    pixels_sha256: Optional[str] = None
    draw_calls: Optional[int] = None
    boundary_pc: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        if type(self.swap_count) is not int or self.swap_count < 1:
            raise ValueError("SwapObservation.swap_count must be an integer >= 1")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "swap_count": self.swap_count,
            "layout": self.layout.to_dict() if self.layout is not None else None,
            "layout_captured": bool(self.layout is not None and self.layout.captured),
            "frame_id": self.frame_id,
            "pixels_sha256": self.pixels_sha256,
            "draw_calls": self.draw_calls,
            "boundary_pc": _hex_or_none(self.boundary_pc),
            "metadata": dict(self.metadata),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class InputReadiness:
    """Explicit original input-ready gate status."""

    ready: bool
    has_initial_swap: Optional[bool] = None
    ui_active: Optional[bool] = None
    mode35_ack: Optional[bool] = None
    transition_complete: Optional[bool] = None
    input_route_ready: Optional[bool] = None
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.ready) is not bool:
            raise ValueError("InputReadiness.ready must be a boolean")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "has_initial_swap": self.has_initial_swap,
            "ui_active": self.ui_active,
            "mode35_ack": self.mode35_ack,
            "transition_complete": self.transition_complete,
            "input_route_ready": self.input_route_ready,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class StepTarget:
    """Explicit post-step completion target that must be proven by a real EGL swap.

    Targets always evaluate against the swap's *rendered* layout (``render_scene``
    and ``render_modal`` captured from ``0x0b392e90`` at ``eglSwapBuffers``),
    never against ``source_layout`` (``0x08cb10c8``) alone.
    """

    expected_scene: Optional[int] = None
    expected_modal: Optional[int] = None
    require_render_layout: bool = False
    same_scene_frame: bool = False
    expected_frame_id: Optional[str] = None
    require_pixels_change: bool = False
    frame_predicate: Optional[Callable[[SwapObservation, Dict[str, Any]], bool]] = None
    description: str = ""
    legacy_fallback: bool = False

    def __post_init__(self) -> None:
        for field_name in ("expected_scene", "expected_modal"):
            val = getattr(self, field_name)
            if val is not None and (type(val) is not int or not 0 <= val <= 0xFFFF):
                raise ValueError(f"{field_name} must be an integer in 0..0xffff or None")
        for field_name in (
            "require_render_layout",
            "same_scene_frame",
            "require_pixels_change",
            "legacy_fallback",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        if self.expected_frame_id is not None and not isinstance(self.expected_frame_id, str):
            raise ValueError("expected_frame_id must be a string or None")
        if self.frame_predicate is not None and not callable(self.frame_predicate):
            raise ValueError("frame_predicate must be callable or None")

    @classmethod
    def scene_modal(
        cls,
        *,
        scene: Optional[int] = None,
        modal: Optional[int] = None,
        description: str = "",
    ) -> "StepTarget":
        if scene is None and modal is None:
            raise ValueError("StepTarget.scene_modal requires at least scene or modal")
        return cls(
            expected_scene=scene,
            expected_modal=modal,
            require_render_layout=True,
            description=description,
        )

    @classmethod
    def same_scene(
        cls,
        *,
        expected_scene: Optional[int] = None,
        expected_modal: Optional[int] = None,
        expected_frame_id: Optional[str] = None,
        require_pixels_change: bool = False,
        frame_predicate: Optional[Callable[[SwapObservation, Dict[str, Any]], bool]] = None,
        require_render_layout: bool = False,
        description: str = "",
    ) -> "StepTarget":
        if (
            expected_scene is None
            and expected_modal is None
            and expected_frame_id is None
            and not require_pixels_change
            and frame_predicate is None
            and not require_render_layout
        ):
            raise ValueError(
                "StepTarget.same_scene requires at least one post-step frame criterion "
                "(expected_scene/expected_modal/expected_frame_id/require_pixels_change/"
                "frame_predicate/require_render_layout)"
            )
        return cls(
            expected_scene=expected_scene,
            expected_modal=expected_modal,
            require_render_layout=(
                require_render_layout
                or expected_scene is not None
                or expected_modal is not None
            ),
            same_scene_frame=True,
            expected_frame_id=expected_frame_id,
            require_pixels_change=require_pixels_change,
            frame_predicate=frame_predicate,
            description=description,
        )

    @classmethod
    def legacy_captured_layout(
        cls,
        description: str = "legacy_post_press_swap_with_captured_layout",
    ) -> "StepTarget":
        return cls(
            require_render_layout=True,
            description=description,
            legacy_fallback=True,
        )

    def has_criterion(self) -> bool:
        return any(
            (
                self.expected_scene is not None,
                self.expected_modal is not None,
                self.require_render_layout,
                self.expected_frame_id is not None,
                self.require_pixels_change,
                self.frame_predicate is not None,
            )
        )

    def needs_layout_proof(self) -> bool:
        return (
            self.expected_scene is not None
            or self.expected_modal is not None
            or self.require_render_layout
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expected_scene": _hex_or_none(self.expected_scene),
            "expected_modal": _hex_or_none(self.expected_modal),
            "require_render_layout": self.require_render_layout,
            "same_scene_frame": self.same_scene_frame,
            "expected_frame_id": self.expected_frame_id,
            "require_pixels_change": self.require_pixels_change,
            "has_frame_predicate": self.frame_predicate is not None,
            "description": self.description,
            "legacy_fallback": self.legacy_fallback,
        }

    def evaluate(
        self,
        swap: SwapObservation,
        context: Dict[str, Any],
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Evaluate whether ``swap`` proves this target after press dispatch."""
        dispatch_floor = context["press_dispatch_swap_count"]
        if swap.swap_count <= dispatch_floor:
            return (
                False,
                "pre_dispatch_or_stale_swap_index",
                {
                    "swap_count": swap.swap_count,
                    "press_dispatch_swap_count": dispatch_floor,
                },
            )

        verified_criteria: List[str] = ["actual_swap_after_press_dispatch"]

        if self.needs_layout_proof():
            layout = swap.layout
            if layout is None or not layout.captured:
                return (
                    False,
                    "missing_layout_proof",
                    {"swap_count": swap.swap_count, "layout_captured": False},
                )
            if self.expected_scene is not None:
                if layout.render_scene is None:
                    return (
                        False,
                        "missing_render_scene_proof",
                        {"swap_count": swap.swap_count},
                    )
                if layout.render_scene != self.expected_scene:
                    reason = (
                        "source_matched_but_render_scene_stale"
                        if layout.source_scene == self.expected_scene
                        else "render_scene_mismatch"
                    )
                    return (
                        False,
                        reason,
                        {
                            "swap_count": swap.swap_count,
                            "expected_scene": hex(self.expected_scene),
                            "observed_render_scene": hex(layout.render_scene),
                            "observed_source_scene": _hex_or_none(layout.source_scene),
                        },
                    )
                verified_criteria.append("render_scene")

            if self.expected_modal is not None:
                if layout.render_modal is None:
                    return (
                        False,
                        "missing_render_modal_proof",
                        {"swap_count": swap.swap_count},
                    )
                if layout.render_modal != self.expected_modal:
                    reason = (
                        "source_matched_but_render_modal_stale"
                        if layout.source_modal == self.expected_modal
                        else "render_modal_mismatch"
                    )
                    return (
                        False,
                        reason,
                        {
                            "swap_count": swap.swap_count,
                            "expected_modal": hex(self.expected_modal),
                            "observed_render_modal": hex(layout.render_modal),
                            "observed_source_modal": _hex_or_none(layout.source_modal),
                        },
                    )
                verified_criteria.append("render_modal")

            if self.require_render_layout:
                if layout.render_scene is None or layout.render_modal is None:
                    return (
                        False,
                        "incomplete_render_layout_proof",
                        {
                            "swap_count": swap.swap_count,
                            "observed_render_scene": _hex_or_none(layout.render_scene),
                            "observed_render_modal": _hex_or_none(layout.render_modal),
                        },
                    )
                verified_criteria.append("captured_render_layout")

        if self.expected_frame_id is not None:
            if swap.frame_id != self.expected_frame_id:
                return (
                    False,
                    "frame_id_mismatch",
                    {
                        "swap_count": swap.swap_count,
                        "expected_frame_id": self.expected_frame_id,
                        "observed_frame_id": swap.frame_id,
                    },
                )
            verified_criteria.append("post_step_frame_id")

        if self.require_pixels_change:
            baseline_sha = context.get("baseline_pixels_sha256")
            if not swap.pixels_sha256:
                return (
                    False,
                    "missing_pixels_digest",
                    {"swap_count": swap.swap_count},
                )
            if not baseline_sha:
                return (
                    False,
                    "missing_baseline_pixels_digest",
                    {"swap_count": swap.swap_count},
                )
            if swap.pixels_sha256 == baseline_sha:
                return (
                    False,
                    "pixels_unchanged",
                    {
                        "swap_count": swap.swap_count,
                        "pixels_sha256": swap.pixels_sha256,
                    },
                )
            verified_criteria.append("pixels_sha256_changed")

        if self.frame_predicate is not None:
            if not bool(self.frame_predicate(swap, dict(context))):
                return (
                    False,
                    "frame_predicate_not_satisfied",
                    {"swap_count": swap.swap_count},
                )
            verified_criteria.append("caller_frame_predicate")

        details: Dict[str, Any] = {
            "swap_count": swap.swap_count,
            "press_dispatch_swap_count": dispatch_floor,
            "verified_criteria": verified_criteria,
            "frame_id": swap.frame_id,
            "pixels_sha256": swap.pixels_sha256,
            "layout": swap.layout.to_dict() if swap.layout is not None else None,
            "legacy_fallback": self.legacy_fallback,
            "target_semantics_verified": not self.legacy_fallback,
        }
        if self.same_scene_frame:
            details["same_scene_step"] = True
            details["selection_change_unverified_by_scene_alone"] = True
            details["selection_change_proven_by_scene_alone"] = False
        return True, "matched", details


def parse_target_spec(spec: str) -> StepTarget:
    """Parse a CLI target string into a ``StepTarget``.

    Supported tokens (comma-separated):
      - ``scene=0x70de`` (or decimal)
      - ``modal=0x0`` (or decimal)
      - ``frame=frame-0004.png``
      - ``same_scene``
      - ``pixels_change``
      - ``any_captured_layout``
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("Target specification must be a non-empty string")
    expected_scene: Optional[int] = None
    expected_modal: Optional[int] = None
    expected_frame_id: Optional[str] = None
    same_scene_frame = False
    require_pixels_change = False
    require_render_layout = False
    legacy_fallback = False

    for raw_token in spec.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if token == "same_scene":
            same_scene_frame = True
        elif token == "pixels_change":
            require_pixels_change = True
        elif token == "any_captured_layout":
            require_render_layout = True
            legacy_fallback = True
        elif "=" in token:
            name, value = (part.strip() for part in token.split("=", 1))
            if name == "scene":
                expected_scene = int(value, 0)
                require_render_layout = True
            elif name == "modal":
                expected_modal = int(value, 0)
                require_render_layout = True
            elif name == "frame":
                if not value:
                    raise ValueError("frame= requires a non-empty identifier")
                expected_frame_id = value
            else:
                raise ValueError(f"Unknown target token {name!r} in {spec!r}")
        else:
            raise ValueError(f"Unknown target flag {token!r} in {spec!r}")

    target = StepTarget(
        expected_scene=expected_scene,
        expected_modal=expected_modal,
        require_render_layout=require_render_layout,
        same_scene_frame=same_scene_frame,
        expected_frame_id=expected_frame_id,
        require_pixels_change=require_pixels_change,
        description=spec.strip(),
        legacy_fallback=legacy_fallback,
    )
    if not target.has_criterion():
        raise ValueError(
            f"Target specification {spec!r} must include at least one concrete criterion"
        )
    return target


@dataclass(frozen=True)
class KeyStep:
    """One scripted step in a key sequence."""

    key: str
    action: str = "click"  # "click" | "press_only" | "release_only"
    target: Optional[StepTarget] = None
    phase_timeout: Optional[float] = None
    phase_timeouts: Dict[str, float] = field(default_factory=dict)
    arm_options: Dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", normalize_key(self.key))
        if self.action not in ("click", "press_only", "release_only"):
            raise ValueError("KeyStep.action must be 'click', 'press_only', or 'release_only'")
        if self.target is not None and not isinstance(self.target, StepTarget):
            raise ValueError("KeyStep.target must be a StepTarget or None")
        if self.phase_timeout is not None and self.phase_timeout <= 0:
            raise ValueError("KeyStep.phase_timeout must be > 0")
        for wait_reason, limit in self.phase_timeouts.items():
            if wait_reason not in ALL_WAIT_REASONS:
                raise ValueError(f"Unknown wait reason {wait_reason!r} in phase_timeouts")
            if limit <= 0:
                raise ValueError(f"Timeout for {wait_reason!r} must be > 0")
        for option_name, option_value in self.arm_options.items():
            if type(option_value) is not bool:
                raise ValueError(f"arm_options[{option_name!r}] must be a boolean")


@dataclass(frozen=True)
class ArmCommand:
    """Explicit command emitted by the scheduler for the caller's KeyProbe."""

    step_index: int
    key: str
    action: int  # 1 = press, 0 = release
    arm_options: Dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "key": self.key,
            "action": self.action,
            "arm_options": dict(self.arm_options),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EventOutcome:
    """Structured result returned by every scheduler observation method."""

    accepted: bool
    duplicate: bool = False
    reason: str = "ok"
    status: str = ""
    pending_wait_reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


def read_layout_snapshot(read_mem: Callable[[int, int], bytes]) -> LayoutSnapshot:
    """Read original source (0x08cb10c8) and render (0x0b392e90) layouts read-only.

    Never writes to guest RAM or registers. ``read_mem(address, size)`` is the
    only operation performed.
    """
    source_raw = bytes(read_mem(SOURCE_LAYOUT_ADDR, LAYOUT_SIZE))
    render_raw = bytes(read_mem(RENDER_LAYOUT_ADDR, LAYOUT_SIZE))
    if len(source_raw) != LAYOUT_SIZE or len(render_raw) != LAYOUT_SIZE:
        raise ValueError("Layout read did not return 16 bytes for source and render layouts")
    source_modal = struct.unpack_from("<H", source_raw, LAYOUT_MODAL_OFFSET)[0]
    source_scene = struct.unpack_from("<H", source_raw, LAYOUT_SCENE_OFFSET)[0]
    render_modal = struct.unpack_from("<H", render_raw, LAYOUT_MODAL_OFFSET)[0]
    render_scene = struct.unpack_from("<H", render_raw, LAYOUT_SCENE_OFFSET)[0]
    return LayoutSnapshot(
        source_scene=source_scene,
        source_modal=source_modal,
        render_scene=render_scene,
        render_modal=render_modal,
        source_raw_hex=source_raw.hex(),
        render_raw_hex=render_raw.hex(),
        captured=True,
    )


def read_input_route_readiness(
    read_mem: Callable[[int, int], bytes],
    *,
    has_initial_swap: bool = True,
    service_probe_completed: bool = True,
    require_ui_active: bool = False,
    require_mode35_ack: bool = False,
    require_transition_complete: bool = False,
    require_input_route_ready: bool = True,
) -> InputReadiness:
    """Evaluate original read-only input-gate predicates without mutating guest RAM."""
    def _u32(addr: int) -> int:
        raw = bytes(read_mem(addr, 4))
        if len(raw) != 4:
            raise ValueError(f"Short read at {hex(addr)}")
        return struct.unpack("<I", raw)[0]

    def _u8(addr: int) -> int:
        raw = bytes(read_mem(addr, 1))
        if len(raw) != 1:
            raise ValueError(f"Short read at {hex(addr)}")
        return raw[0]

    details: Dict[str, Any] = {}
    reasons: List[str] = []

    if not has_initial_swap:
        reasons.append("no_initial_egl_swap")
    if not service_probe_completed:
        reasons.append("service_probe_incomplete")

    ui_active: Optional[bool] = None
    if require_ui_active:
        state = _u32(UI_STATE_ADDR)
        mode = _u32(UI_MODE_ADDR)
        ui_active = state != 0 and mode != 6
        details["ui_state"] = state
        details["ui_mode"] = mode
        if not ui_active:
            reasons.append("ui_not_active")

    mode35_ack: Optional[bool] = None
    if require_mode35_ack:
        mask = _u8(MODE35_ACK_MASK_ADDR)
        mode35_ack = (mask & 0xF) == 0xF
        details["mode35_ack_mask"] = hex(mask)
        if not mode35_ack:
            reasons.append("mode35_ack_incomplete")

    transition_complete: Optional[bool] = None
    if require_transition_complete:
        step = _u32(TRANSITION_STEP_ADDR)
        stage = _u32(PRIMARY_STAGE_ADDR)
        state = _u32(UI_STATE_ADDR)
        mode = _u32(UI_MODE_ADDR)
        transition_complete = (step == 0x4A0 and stage == 0 and state == 1 and mode == 1)
        details["transition_step"] = hex(step)
        details["primary_stage"] = stage
        if not transition_complete:
            reasons.append("transition_incomplete")

    input_route_ready: Optional[bool] = None
    if require_input_route_ready:
        primary_stage = _u32(PRIMARY_STAGE_ADDR)
        pending_mode = _u32(PENDING_MODE_ADDR)
        secondary_stage = _u32(SECONDARY_STAGE_ADDR)
        primary_ok = pending_mode == 0xFF and primary_stage in (0, 1)
        secondary_ok = secondary_stage in (0, 1)
        input_route_ready = primary_ok and secondary_ok
        details["primary_stage"] = primary_stage
        details["pending_mode"] = hex(pending_mode)
        details["secondary_stage"] = secondary_stage
        if not input_route_ready:
            reasons.append("input_route_not_ready")

    return InputReadiness(
        ready=not reasons,
        has_initial_swap=has_initial_swap,
        ui_active=ui_active,
        mode35_ack=mode35_ack,
        transition_complete=transition_complete,
        input_route_ready=input_route_ready,
        reason=",".join(reasons) if reasons else "ready",
        details=details,
    )


@dataclass
class _StepRuntime:
    index: int
    step: KeyStep
    press_armed: bool = False
    press_armed_count: int = 0
    press_dispatched: bool = False
    press_dispatch_swap_count: Optional[int] = None
    press_dispatch_timestamp: Optional[float] = None
    baseline_pixels_sha256: Optional[str] = None
    baseline_layout: Optional[LayoutSnapshot] = None
    press_consumed: bool = False
    press_consumed_timestamp: Optional[float] = None
    release_armed: bool = False
    release_armed_count: int = 0
    release_dispatched: bool = False
    release_consumed: bool = False
    release_consumed_timestamp: Optional[float] = None
    target_satisfied: bool = False
    target_satisfied_before_release: bool = False
    target_verification: Optional[Dict[str, Any]] = None
    unmatched_post_dispatch_swaps: List[Dict[str, Any]] = field(default_factory=list)
    final_step_target_ignored: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.index,
            "key": self.step.key,
            "action": self.step.action,
            "target": self.step.target.to_dict() if self.step.target is not None else None,
            "press_armed": self.press_armed,
            "press_armed_count": self.press_armed_count,
            "press_dispatched": self.press_dispatched,
            "press_dispatch_swap_count": self.press_dispatch_swap_count,
            "press_consumed": self.press_consumed,
            "release_armed": self.release_armed,
            "release_armed_count": self.release_armed_count,
            "release_dispatched": self.release_dispatched,
            "release_consumed": self.release_consumed,
            "target_satisfied": self.target_satisfied,
            "target_satisfied_before_release": self.target_satisfied_before_release,
            "target_verification": self.target_verification,
            "unmatched_post_dispatch_swaps": list(self.unmatched_post_dispatch_swaps),
            "final_step_target_ignored": self.final_step_target_ignored,
        }


class KeySequenceScheduler:
    """Event-driven state machine for deterministic multi-step key sequences."""

    def __init__(
        self,
        steps: Sequence[KeyStep],
        *,
        clock: Callable[[], float] = time.monotonic,
        default_phase_timeout: float = 5.0,
        phase_timeouts: Optional[Dict[str, float]] = None,
        sequence_timeout: Optional[float] = None,
        require_initial_swap: bool = True,
        input_readiness_probe: Optional[Callable[[], Union[bool, InputReadiness]]] = None,
    ) -> None:
        if not steps:
            raise ValueError("KeySequenceScheduler requires at least one KeyStep")
        if len(steps) > MAX_SEQUENCE_LENGTH:
            raise ValueError(f"Key sequence accepts at most {MAX_SEQUENCE_LENGTH} steps")
        if default_phase_timeout <= 0:
            raise ValueError("default_phase_timeout must be > 0")
        if sequence_timeout is not None and sequence_timeout <= 0:
            raise ValueError("sequence_timeout must be > 0 when provided")

        validated_steps: List[KeyStep] = []
        for idx, step in enumerate(steps):
            if not isinstance(step, KeyStep):
                raise ValueError(f"steps[{idx}] must be a KeyStep")
            is_final = idx == len(steps) - 1
            if not is_final:
                if step.target is None or not step.target.has_criterion():
                    raise ValueError(
                        f"Non-final step {idx} ({step.key}) must declare an explicit StepTarget"
                    )
            validated_steps.append(step)

        self._phase_timeouts: Dict[str, float] = {}
        if phase_timeouts:
            for wait_reason, limit in phase_timeouts.items():
                if wait_reason not in ALL_WAIT_REASONS:
                    raise ValueError(f"Unknown wait reason {wait_reason!r} in phase_timeouts")
                if limit <= 0:
                    raise ValueError(f"Timeout for {wait_reason!r} must be > 0")
                self._phase_timeouts[wait_reason] = float(limit)

        self._clock = clock
        self._default_phase_timeout = float(default_phase_timeout)
        self._sequence_timeout = float(sequence_timeout) if sequence_timeout is not None else None
        self._require_initial_swap = bool(require_initial_swap)
        self._input_readiness_probe = input_readiness_probe

        self._steps: List[_StepRuntime] = [
            _StepRuntime(index=i, step=step) for i, step in enumerate(validated_steps)
        ]
        self._current_index: int = 0
        self._completed_steps: int = 0
        self._status: str = "running"
        self._pending_wait_reason: Optional[str] = None

        self._started_at: float = float(self._clock())
        self._sequence_deadline: Optional[float] = (
            self._started_at + self._sequence_timeout
            if self._sequence_timeout is not None
            else None
        )
        self._phase_started_at: float = self._started_at
        self._phase_deadline: Optional[float] = None

        self._input_readiness: InputReadiness = InputReadiness(
            ready=False,
            reason="input_readiness_not_yet_observed",
        )
        self._last_swap_count: int = 0
        self._last_swap: Optional[SwapObservation] = None
        self._swap_history: List[Dict[str, Any]] = []
        self._last_working_layout: Optional[LayoutSnapshot] = None
        self._working_layout_observations: int = 0

        self._anomalies: List[Dict[str, Any]] = []
        self._events: List[Dict[str, Any]] = []
        self._stop_reason: Optional[str] = None
        self._unsafe_release_unconfirmed: bool = False
        self._unconfirmed_release_details: Optional[Dict[str, Any]] = None
        self._timeout_details: Optional[Dict[str, Any]] = None

        self._enter_phase(WAIT_INPUT_READY, now=self._started_at)

    @classmethod
    def for_single_key(
        cls,
        key: str,
        *,
        action: int = 1,
        click: bool = False,
        initial_arm_options: Optional[Dict[str, bool]] = None,
        **kwargs: Any,
    ) -> "KeySequenceScheduler":
        """Backward-compatible factory for legacy ``--key-event`` / ``--key-action`` / ``--key-click``."""
        if type(action) is not int or action not in (0, 1):
            raise ValueError("action must be 0 (release) or 1 (press)")
        if type(click) is not bool:
            raise ValueError("click must be a boolean")
        if click and action != 1:
            raise ValueError("click=True requires press action 1")
        step_action = "click" if click else ("press_only" if action == 1 else "release_only")
        step = KeyStep(
            key=key,
            action=step_action,
            target=None,
            arm_options=dict(initial_arm_options or {}),
        )
        return cls([step], **kwargs)

    @classmethod
    def from_cli(
        cls,
        *,
        key_event: Optional[str] = None,
        key_action: int = 1,
        key_click: bool = False,
        key_sequence: Optional[Sequence[str]] = None,
        key_targets: Optional[Sequence[Union[str, StepTarget]]] = None,
        initial_arm_options: Optional[Dict[str, bool]] = None,
        subsequent_arm_options: Optional[Dict[str, bool]] = None,
        **kwargs: Any,
    ) -> "KeySequenceScheduler":
        """Build a scheduler from legacy or target-annotated CLI arguments."""
        if key_sequence:
            keys = list(key_sequence)
            if len(keys) > MAX_SEQUENCE_LENGTH:
                raise ValueError(f"--key-sequence accepts at most {MAX_SEQUENCE_LENGTH} clicks")
            parsed_targets: List[StepTarget] = []
            if key_targets:
                for item in key_targets:
                    parsed_targets.append(
                        item if isinstance(item, StepTarget) else parse_target_spec(item)
                    )
            expected_non_final = max(0, len(keys) - 1)
            if parsed_targets and len(parsed_targets) not in (expected_non_final, len(keys)):
                raise ValueError(
                    f"Expected {expected_non_final} target(s) for {len(keys)} key(s), "
                    f"got {len(parsed_targets)}"
                )
            sub_opts = (
                dict(subsequent_arm_options)
                if subsequent_arm_options is not None
                else {"wait_for_input_route_ready": True}
            )
            steps: List[KeyStep] = []
            for idx, key in enumerate(keys):
                is_final = idx == len(keys) - 1
                if is_final:
                    target = None
                elif idx < len(parsed_targets):
                    target = parsed_targets[idx]
                else:
                    target = StepTarget.legacy_captured_layout()
                steps.append(
                    KeyStep(
                        key=key,
                        action="click",
                        target=target,
                        arm_options=dict(initial_arm_options or {}) if idx == 0 else dict(sub_opts),
                    )
                )
            return cls(steps, **kwargs)

        if key_event:
            return cls.for_single_key(
                key_event,
                action=key_action,
                click=key_click,
                initial_arm_options=initial_arm_options,
                **kwargs,
            )

        raise ValueError("Provide either key_sequence or key_event")

    @property
    def status(self) -> str:
        return self._status

    @property
    def active(self) -> bool:
        return self._status == "running"

    @property
    def completed(self) -> bool:
        return self._status == "completed"

    @property
    def pending_wait_reason(self) -> Optional[str]:
        return self._pending_wait_reason

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def completed_steps(self) -> int:
        return self._completed_steps

    @property
    def unsafe_release_unconfirmed(self) -> bool:
        return self._unsafe_release_unconfirmed

    @property
    def last_swap_count(self) -> int:
        return self._last_swap_count

    @property
    def steps(self) -> Tuple[KeyStep, ...]:
        return tuple(rt.step for rt in self._steps)

    def _current(self) -> _StepRuntime:
        return self._steps[self._current_index]

    def _timeout_for(self, wait_reason: str, step: KeyStep) -> float:
        if wait_reason in step.phase_timeouts:
            return float(step.phase_timeouts[wait_reason])
        if step.phase_timeout is not None:
            return float(step.phase_timeout)
        if wait_reason in self._phase_timeouts:
            return float(self._phase_timeouts[wait_reason])
        return self._default_phase_timeout

    def _enter_phase(self, wait_reason: str, *, now: Optional[float] = None) -> None:
        ts = float(self._clock()) if now is None else float(now)
        self._pending_wait_reason = wait_reason
        self._phase_started_at = ts
        limit = self._timeout_for(wait_reason, self._current().step)
        self._phase_deadline = ts + limit

    def _record_event(self, kind: str, **fields: Any) -> None:
        self._events.append({"index": len(self._events), "kind": kind, **fields})

    def _record_anomaly(self, kind: str, **fields: Any) -> None:
        self._anomalies.append({"index": len(self._anomalies), "kind": kind, **fields})

    def _refresh_readiness_from_probe(self) -> None:
        if self._input_readiness_probe is not None:
            sampled = self._input_readiness_probe()
            self._set_input_readiness(sampled, log_event=False)

    def _set_input_readiness(
        self,
        readiness: Union[bool, InputReadiness],
        *,
        log_event: bool = True,
    ) -> InputReadiness:
        if isinstance(readiness, bool):
            obs = InputReadiness(
                ready=readiness,
                reason="ready" if readiness else "not_ready",
            )
        elif isinstance(readiness, InputReadiness):
            obs = readiness
        else:
            raise ValueError("readiness must be a bool or InputReadiness")
        prev = self._input_readiness.to_dict() if self._input_readiness is not None else None
        self._input_readiness = obs
        if log_event and obs.to_dict() != prev:
            self._record_event("input_readiness_observed", **obs.to_dict())
        return obs

    def _has_required_initial_swap(self) -> bool:
        if not self._require_initial_swap:
            return True
        if self._input_readiness.has_initial_swap is False:
            return False
        return self._last_swap_count >= 1

    def _check_unconfirmed_release(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Return whether any step has an armed/dispatched press without confirmed release."""
        for rt in self._steps:
            if rt.step.action == "click" and rt.press_armed and not rt.release_consumed:
                return (
                    True,
                    {
                        "step_index": rt.index,
                        "key": rt.step.key,
                        "press_armed": rt.press_armed,
                        "press_dispatched": rt.press_dispatched,
                        "press_consumed": rt.press_consumed,
                        "release_armed": rt.release_armed,
                        "release_consumed": rt.release_consumed,
                        "warning": (
                            f"Press for {rt.step.key} at step {rt.index} was armed/dispatched "
                            "and matching release was not confirmed; cannot guarantee safe release"
                        ),
                    },
                )
        return False, None

    def _finish_terminal(
        self,
        new_status: str,
        reason: str,
        *,
        extra_timeout: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._status = new_status
        self._stop_reason = reason
        unsafe, details = self._check_unconfirmed_release()
        self._unsafe_release_unconfirmed = unsafe
        self._unconfirmed_release_details = details
        if extra_timeout is not None:
            self._timeout_details = extra_timeout
        self._phase_deadline = None

    def observe_input_readiness(self, readiness: Union[bool, InputReadiness]) -> EventOutcome:
        """Record an explicit observation of the original input-ready gate."""
        obs = self._set_input_readiness(readiness, log_event=True)
        return EventOutcome(
            accepted=True,
            reason="ok",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
            details=obs.to_dict(),
        )

    def observe_working_layout(self, layout: LayoutSnapshot) -> EventOutcome:
        """Record a RAM-only working-layout observation (0x08cb10c8 / 0x0b392e90).

        Working-layout updates NEVER count as an actual EGL swap and NEVER
        satisfy any step target.
        """
        if not isinstance(layout, LayoutSnapshot):
            raise ValueError("layout must be a LayoutSnapshot")
        self._last_working_layout = layout
        self._working_layout_observations += 1
        self._record_event("working_layout_observed", layout=layout.to_dict())
        return EventOutcome(
            accepted=True,
            reason="working_layout_recorded_not_a_swap",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
            details={
                "satisfies_target": False,
                "layout": layout.to_dict(),
            },
        )

    def observe_swap(self, swap: SwapObservation) -> EventOutcome:
        """Record an actual original eglSwapBuffers event with optional layout proof."""
        if not isinstance(swap, SwapObservation):
            raise ValueError("swap must be a SwapObservation")
        if swap.swap_count <= self._last_swap_count:
            self._record_anomaly(
                "non_monotonic_or_duplicate_swap",
                observed_swap_count=swap.swap_count,
                last_swap_count=self._last_swap_count,
            )
            return EventOutcome(
                accepted=False,
                duplicate=(swap.swap_count == self._last_swap_count),
                reason="non_monotonic_or_duplicate_swap",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        self._last_swap_count = swap.swap_count
        self._last_swap = swap
        self._swap_history.append(swap.to_dict())
        self._record_event("actual_egl_swap", swap=swap.to_dict())

        if not self.active:
            return EventOutcome(
                accepted=True,
                reason="swap_recorded_while_inactive",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        rt = self._current()
        is_final = rt.index == len(self._steps) - 1
        evaluation_details: Dict[str, Any] = {}

        if (
            not is_final
            and rt.press_dispatched
            and not rt.target_satisfied
            and rt.step.target is not None
        ):
            context = {
                "step_index": rt.index,
                "key": rt.step.key,
                "press_dispatch_swap_count": rt.press_dispatch_swap_count,
                "baseline_pixels_sha256": rt.baseline_pixels_sha256,
                "baseline_layout": rt.baseline_layout,
            }
            matched, reason, details = rt.step.target.evaluate(swap, context)
            evaluation_details = {"matched": matched, "reason": reason, **details}
            if matched:
                rt.target_satisfied = True
                rt.target_satisfied_before_release = (
                    rt.step.action == "click" and not rt.release_consumed
                )
                rt.target_verification = details
                self._record_event(
                    "step_target_satisfied",
                    step_index=rt.index,
                    key=rt.step.key,
                    before_release_consumed=rt.target_satisfied_before_release,
                    verification=details,
                )
                if self._pending_wait_reason == WAIT_TARGET_FRAME:
                    self._current_index += 1
                    self._enter_phase(WAIT_INPUT_READY)
            else:
                rt.unmatched_post_dispatch_swaps.append(
                    {"swap_count": swap.swap_count, "reason": reason, **details}
                )
                self._record_event(
                    "post_dispatch_swap_unmatched",
                    step_index=rt.index,
                    key=rt.step.key,
                    reason=reason,
                    details=details,
                )

        return EventOutcome(
            accepted=True,
            reason="ok",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
            details=evaluation_details,
        )

    def observe_press_dispatched(
        self,
        key: str,
        *,
        swap_count: Optional[int] = None,
        timestamp: Optional[float] = None,
    ) -> EventOutcome:
        """Record that the original firmware dispatched the armed press (KEY_DISPATCH)."""
        canonical = normalize_key(key)
        if not self.active:
            self._record_anomaly(
                "press_dispatched_while_inactive",
                key=canonical,
                status=self._status,
            )
            return EventOutcome(
                accepted=False,
                reason="scheduler_not_active",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        rt = self._current()
        if canonical != rt.step.key:
            self._record_anomaly(
                "press_dispatch_key_mismatch",
                expected_key=rt.step.key,
                observed_key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                reason="key_mismatch",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if not rt.press_armed:
            self._record_anomaly(
                "press_dispatched_before_arm",
                key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                reason="press_not_armed",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if rt.press_dispatched:
            self._record_anomaly(
                "duplicate_press_dispatched",
                key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                duplicate=True,
                reason="duplicate_press_dispatched",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if swap_count is not None:
            if type(swap_count) is not int or swap_count < 0:
                raise ValueError("swap_count must be an integer >= 0 or None")
            if swap_count < self._last_swap_count:
                self._record_anomaly(
                    "press_dispatch_stale_swap_count",
                    observed_swap_count=swap_count,
                    last_swap_count=self._last_swap_count,
                    step_index=rt.index,
                )
                return EventOutcome(
                    accepted=False,
                    reason="swap_count_behind_observed",
                    status=self._status,
                    pending_wait_reason=self._pending_wait_reason,
                )
            dispatch_swap_floor = swap_count
        else:
            dispatch_swap_floor = self._last_swap_count

        ts = float(self._clock()) if timestamp is None else float(timestamp)
        rt.press_dispatched = True
        rt.press_dispatch_swap_count = dispatch_swap_floor
        rt.press_dispatch_timestamp = ts
        rt.baseline_pixels_sha256 = (
            self._last_swap.pixels_sha256 if self._last_swap is not None else None
        )
        rt.baseline_layout = self._last_swap.layout if self._last_swap is not None else None

        self._record_event(
            "original_press_dispatched",
            step_index=rt.index,
            key=canonical,
            swap_count=dispatch_swap_floor,
            timestamp=ts,
        )
        self._enter_phase(WAIT_PRESS_CONSUMED, now=ts)
        return EventOutcome(
            accepted=True,
            reason="ok",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
            details={"press_dispatch_swap_count": dispatch_swap_floor},
        )

    def observe_press_consumed(
        self,
        key: str,
        *,
        timestamp: Optional[float] = None,
    ) -> EventOutcome:
        """Record that the original key dispatcher returned for the armed press."""
        canonical = normalize_key(key)
        if not self.active:
            # Handle duplicate notification arriving after completion/termination cleanly.
            was_prev = any(
                rt.step.key == canonical and rt.press_consumed for rt in self._steps
            )
            self._record_anomaly(
                "press_consumed_while_inactive",
                key=canonical,
                status=self._status,
                duplicate=was_prev,
            )
            return EventOutcome(
                accepted=False,
                duplicate=was_prev,
                reason="duplicate_press_consumed" if was_prev else "scheduler_not_active",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        rt = self._current()
        if canonical != rt.step.key:
            self._record_anomaly(
                "press_consumed_key_mismatch",
                expected_key=rt.step.key,
                observed_key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                reason="key_mismatch",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if rt.press_consumed:
            self._record_anomaly(
                "duplicate_press_consumed",
                key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                duplicate=True,
                reason="duplicate_press_consumed",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if not rt.press_armed or not rt.press_dispatched:
            prev_dup = (
                not rt.press_armed
                and rt.index > 0
                and self._steps[rt.index - 1].step.key == canonical
                and self._steps[rt.index - 1].press_consumed
            )
            self._record_anomaly(
                "duplicate_previous_press_consumed"
                if prev_dup
                else "press_consumed_without_dispatch",
                key=canonical,
                step_index=rt.index,
                press_armed=rt.press_armed,
                press_dispatched=rt.press_dispatched,
            )
            return EventOutcome(
                accepted=False,
                duplicate=prev_dup,
                reason="duplicate_press_consumed" if prev_dup else "press_not_dispatched",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        ts = float(self._clock()) if timestamp is None else float(timestamp)
        rt.press_consumed = True
        rt.press_consumed_timestamp = ts
        self._record_event(
            "original_press_consumed",
            step_index=rt.index,
            key=canonical,
            timestamp=ts,
        )

        if rt.step.action == "click":
            self._enter_phase(WAIT_RELEASE_CONSUMED, now=ts)
        else:
            # press_only step completes upon press consumption.
            self._complete_current_step(now=ts)

        return EventOutcome(
            accepted=True,
            reason="ok",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
        )

    def observe_release_dispatched(
        self,
        key: str,
        *,
        swap_count: Optional[int] = None,
        timestamp: Optional[float] = None,
    ) -> EventOutcome:
        """Optional informational observation of original release dispatch."""
        canonical = normalize_key(key)
        if not self.active:
            self._record_anomaly(
                "release_dispatched_while_inactive",
                key=canonical,
                status=self._status,
            )
            return EventOutcome(
                accepted=False,
                reason="scheduler_not_active",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )
        rt = self._current()
        if canonical != rt.step.key or not rt.release_armed:
            self._record_anomaly(
                "unexpected_release_dispatched",
                expected_key=rt.step.key,
                observed_key=canonical,
                release_armed=rt.release_armed,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                reason="release_not_armed_or_key_mismatch",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )
        if rt.release_dispatched:
            self._record_anomaly(
                "duplicate_release_dispatched",
                key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                duplicate=True,
                reason="duplicate_release_dispatched",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )
        rt.release_dispatched = True
        ts = float(self._clock()) if timestamp is None else float(timestamp)
        self._record_event(
            "original_release_dispatched",
            step_index=rt.index,
            key=canonical,
            swap_count=self._last_swap_count if swap_count is None else swap_count,
            timestamp=ts,
        )
        return EventOutcome(
            accepted=True,
            reason="ok",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
        )

    def observe_release_consumed(
        self,
        key: str,
        *,
        timestamp: Optional[float] = None,
    ) -> EventOutcome:
        """Record that the original key dispatcher returned for the armed release."""
        canonical = normalize_key(key)
        if not self.active:
            was_prev = any(
                rt.step.key == canonical and rt.release_consumed for rt in self._steps
            )
            self._record_anomaly(
                "release_consumed_while_inactive",
                key=canonical,
                status=self._status,
                duplicate=was_prev,
            )
            return EventOutcome(
                accepted=False,
                duplicate=was_prev,
                reason="duplicate_release_consumed" if was_prev else "scheduler_not_active",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        rt = self._current()
        if canonical != rt.step.key:
            # Check if this is a late duplicate for the immediately preceding step.
            prev_dup = (
                rt.index > 0
                and self._steps[rt.index - 1].step.key == canonical
                and self._steps[rt.index - 1].release_consumed
                and not rt.release_armed
            )
            self._record_anomaly(
                "duplicate_previous_release_consumed"
                if prev_dup
                else "release_consumed_key_mismatch",
                expected_key=rt.step.key,
                observed_key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                duplicate=prev_dup,
                reason="duplicate_release_consumed" if prev_dup else "key_mismatch",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if rt.release_consumed:
            self._record_anomaly(
                "duplicate_release_consumed",
                key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                duplicate=True,
                reason="duplicate_release_consumed",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        if not rt.release_armed:
            # Also catch duplicate release after step already advanced to same key.
            prev_dup = (
                rt.index > 0
                and self._steps[rt.index - 1].step.key == canonical
                and self._steps[rt.index - 1].release_consumed
            )
            self._record_anomaly(
                "duplicate_previous_release_consumed"
                if prev_dup
                else "release_consumed_before_arm",
                key=canonical,
                step_index=rt.index,
            )
            return EventOutcome(
                accepted=False,
                duplicate=prev_dup,
                reason="duplicate_release_consumed" if prev_dup else "release_not_armed",
                status=self._status,
                pending_wait_reason=self._pending_wait_reason,
            )

        ts = float(self._clock()) if timestamp is None else float(timestamp)
        rt.release_consumed = True
        rt.release_consumed_timestamp = ts
        self._record_event(
            "original_release_consumed",
            step_index=rt.index,
            key=canonical,
            timestamp=ts,
        )
        self._complete_current_step(now=ts)
        return EventOutcome(
            accepted=True,
            reason="ok",
            status=self._status,
            pending_wait_reason=self._pending_wait_reason,
        )

    def _complete_current_step(self, *, now: float) -> None:
        rt = self._current()
        self._completed_steps += 1
        is_final = rt.index == len(self._steps) - 1
        if is_final:
            if rt.step.target is not None:
                rt.final_step_target_ignored = True
            self._status = "completed"
            self._pending_wait_reason = None
            self._phase_deadline = None
            self._stop_reason = "completed"
            self._unsafe_release_unconfirmed = False
            self._record_event(
                "sequence_completed",
                completed_steps=self._completed_steps,
                timestamp=now,
            )
            return

        if rt.target_satisfied:
            self._current_index += 1
            self._enter_phase(WAIT_INPUT_READY, now=now)
        else:
            self._enter_phase(WAIT_TARGET_FRAME, now=now)

    def next_action(self) -> Optional[ArmCommand]:
        """Return the next ``ArmCommand`` to send to ``KeyProbe``, or ``None`` if waiting/done."""
        if not self.active:
            return None

        self._refresh_readiness_from_probe()
        rt = self._current()

        if rt.step.action == "release_only":
            if rt.release_armed:
                return None
            if not self._has_required_initial_swap() or not self._input_readiness.ready:
                return None
            rt.release_armed = True
            rt.release_armed_count += 1
            cmd = ArmCommand(
                step_index=rt.index,
                key=rt.step.key,
                action=0,
                arm_options=dict(rt.step.arm_options),
                reason="arm_single_release",
            )
            self._record_event("command_emitted", command=cmd.to_dict())
            self._enter_phase(WAIT_RELEASE_CONSUMED)
            return cmd

        # Emit press when waiting_input_ready and gate + initial swap are satisfied.
        if self._pending_wait_reason == WAIT_INPUT_READY and not rt.press_armed:
            if not self._has_required_initial_swap() or not self._input_readiness.ready:
                return None
            rt.press_armed = True
            rt.press_armed_count += 1
            cmd = ArmCommand(
                step_index=rt.index,
                key=rt.step.key,
                action=1,
                arm_options=dict(rt.step.arm_options),
                reason="arm_step_press",
            )
            self._record_event("command_emitted", command=cmd.to_dict())
            self._enter_phase(WAIT_PRESS_DISPATCH)
            return cmd

        # Emit matching release immediately after press consumed, even if scene/modal
        # changed or input_ready became False. Never emit more than one release per press.
        if (
            rt.step.action == "click"
            and rt.press_consumed
            and not rt.release_armed
            and not rt.release_consumed
        ):
            rt.release_armed = True
            rt.release_armed_count += 1
            cmd = ArmCommand(
                step_index=rt.index,
                key=rt.step.key,
                action=0,
                arm_options={},
                reason="arm_matching_release",
            )
            self._record_event("command_emitted", command=cmd.to_dict())
            return cmd

        return None

    def timeout_hint(self, now: Optional[float] = None) -> float:
        """Maximum seconds until the caller must re-check scheduler deadlines."""
        if not self.active:
            return 30.0
        ts = float(self._clock()) if now is None else float(now)
        deadlines = [d for d in (self._phase_deadline, self._sequence_deadline) if d is not None]
        if not deadlines:
            return 30.0
        return max(0.001, min(deadlines) - ts)

    def check_timeout(self, now: Optional[float] = None) -> None:
        """Raise ``KeySequenceTimeoutError`` if the active wait phase or sequence deadline expired."""
        if not self.active:
            return
        self._refresh_readiness_from_probe()
        ts = float(self._clock()) if now is None else float(now)
        phase_expired = self._phase_deadline is not None and ts >= self._phase_deadline
        seq_expired = self._sequence_deadline is not None and ts >= self._sequence_deadline
        if not (phase_expired or seq_expired):
            return

        rt = self._current()
        wait_reason = self._pending_wait_reason or WAIT_INPUT_READY
        diagnostics: Dict[str, Any] = {
            "wait_reason": wait_reason,
            "timeout_scope": "phase" if phase_expired else "sequence",
            "step_index": rt.index,
            "key": rt.step.key,
            "phase_elapsed_seconds": round(ts - self._phase_started_at, 6),
            "phase_timeout_seconds": self._timeout_for(wait_reason, rt.step),
            "sequence_elapsed_seconds": round(ts - self._started_at, 6),
            "last_swap_count": self._last_swap_count,
            "last_swap": self._last_swap.to_dict() if self._last_swap is not None else None,
            "last_working_layout": (
                self._last_working_layout.to_dict()
                if self._last_working_layout is not None
                else None
            ),
            "input_readiness": self._input_readiness.to_dict(),
            "step_target": rt.step.target.to_dict() if rt.step.target is not None else None,
            "unmatched_post_dispatch_swaps": list(rt.unmatched_post_dispatch_swaps),
        }
        self._finish_terminal("timed_out", f"timeout:{wait_reason}", extra_timeout=diagnostics)
        self._record_event(
            "scheduler_timed_out",
            wait_reason=wait_reason,
            unsafe_release_unconfirmed=self._unsafe_release_unconfirmed,
            diagnostics=diagnostics,
        )
        raise KeySequenceTimeoutError(
            wait_reason,
            step_index=rt.index,
            key=rt.step.key,
            unsafe_release_unconfirmed=self._unsafe_release_unconfirmed,
            diagnostics=diagnostics,
        )

    def cancel(self, reason: str = "cancelled_by_caller") -> Dict[str, Any]:
        """Cancel the sequence; never emits new presses and reports unconfirmed releases."""
        if not self.active:
            return self.report()
        rt = self._current()
        prior_wait = self._pending_wait_reason
        self._finish_terminal("cancelled", reason)
        self._record_event(
            "scheduler_cancelled",
            reason=reason,
            step_index=rt.index,
            key=rt.step.key,
            prior_wait_reason=prior_wait,
            unsafe_release_unconfirmed=self._unsafe_release_unconfirmed,
        )
        return self.report()

    def fail(self, reason: str) -> Dict[str, Any]:
        """Abort the sequence due to an external/probe error; reports unconfirmed releases."""
        if not self.active:
            return self.report()
        rt = self._current()
        prior_wait = self._pending_wait_reason
        self._finish_terminal("failed", reason)
        self._record_event(
            "scheduler_failed",
            reason=reason,
            step_index=rt.index,
            key=rt.step.key,
            prior_wait_reason=prior_wait,
            unsafe_release_unconfirmed=self._unsafe_release_unconfirmed,
        )
        return self.report()

    def report(self) -> Dict[str, Any]:
        """Return a JSON-serializable audit report of the scheduler state and evidence."""
        return {
            "scope": (
                "Event-driven key-sequence scheduler; read-only with respect to guest "
                "RAM/state; never treats working-layout updates or layoutless swaps as "
                "proven scene transitions, and never claims a menu opened"
            ),
            "guest_ram_or_state_mutated_by_scheduler": False,
            "menu_opened_verified_by_scheduler": False,
            "status": self._status,
            "complete": self._status == "completed",
            "stop_reason": self._stop_reason,
            "pending_wait_reason": self._pending_wait_reason,
            "keys": [rt.step.key for rt in self._steps],
            "total_steps": len(self._steps),
            "completed_steps": self._completed_steps,
            "current_index": self._current_index,
            "last_swap_count": self._last_swap_count,
            "last_swap": self._last_swap.to_dict() if self._last_swap is not None else None,
            "last_working_layout": (
                self._last_working_layout.to_dict()
                if self._last_working_layout is not None
                else None
            ),
            "working_layout_observations": self._working_layout_observations,
            "input_readiness": self._input_readiness.to_dict(),
            "unsafe_release_unconfirmed": self._unsafe_release_unconfirmed,
            "unconfirmed_release_details": self._unconfirmed_release_details,
            "timeout_details": self._timeout_details,
            "steps": [rt.to_dict() for rt in self._steps],
            "anomalies": list(self._anomalies),
            "events": list(self._events),
        }
