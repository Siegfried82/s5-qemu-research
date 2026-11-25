#!/usr/bin/env python3
"""Opt-in original software-key queue call; never a physical GPIO emulator.

This module does not launch QEMU or inject on import/construction. Integration:

    probe = KeyProbe(emu)
    # Only after an observed EGL swap and an explicit key request:
    probe.arm("KEY_CENTER", 1, swaps_observed=graphics.backend.swaps)
    # Before each resume: timeout=min(existing_timeout, probe.timeout_hint())
    # On every stop, with that CPU selected:
    probe.check_timeout()
    if probe.handle(pc, cpu_index):
        continue

The caller retains its outer process timeout and exception/graphics handling.
Report ``probe.report`` verbatim. ``consumed`` means the original key dispatcher
returned, not that a menu opened. A timeout during an inferior call requires
stopping the experiment; never restore its saved registers onto another task.
Only QEMU code breakpoints Z0 are used. No firmware instruction, TCB, event flag
or input-state field is patched. A temporary 32-byte argument area is below the
current original task stack pointer and is restored on a verified return.
"""
import hashlib
import re
import struct
import time
import xml.etree.ElementTree as ET

LOOP_ENTRY = 0x06475dea
QUEUE_API = 0x067f0dda
POOL_COPY_DONE = 0x067f0e42
RECEIVE_RETURN = 0x06475df4
KEY_DISPATCH = 0x064759c6
KEY_DISPATCH_RETURN = 0x064759ca
INPUT_HANDLER_BANK0 = 0x066435a0
INPUT_HANDLER_BANK1 = 0x0664353a
GUI_KEY_EMITTER = 0x066bddc8
GUI_EVENT = 0x066beea6
CENTER_LIVE_HANDLER = 0x06646e84
CENTER_TRANSITION = 0x066be02a
INPUT_TASK_ID = 285
MESSAGE_CODE = 0x0011f002

# Index, bank, mask. Names/indexes are from original06f1003c, not invented keys.
KEYS = {
    "KEY_UP": (3, 0, 4), "KEY_DOWN": (4, 0, 8),
    "KEY_LEFT": (5, 0, 1), "KEY_RIGHT": (6, 0, 2),
    "KEY_CENTER": (7, 0, 0x10), "KEY_PLAY": (8, 0, 0x20),
    "KEY_Q_MENU": (15, 0, 0x2000), "KEY_SET": (58, 1, 0x200000),
}
GUARDS = (0x08ab4e80, 0x08ab4e7c, 0x08ab4e68)
KEY_ROUTE = {
    0x066431de: "raw_state_route_entry",
    0x06643212: "alternate_handler_gate_return",
    0x06643224: "transition_gate_return",
    0x0664322c: "secondary_transition_gate_return",
    0x06643234: "live_state_gate_return",
    0x0664323a: "current_mode_gate_return",
    0x0663eb5a: "generic_key_route_entry",
    0x0663fe34: "live_key_table_route_entry",
    0x0663fe42: "live_route_inhibit_return",
    0x0663fe68: "notification_inhibit_return",
    0x0663fe70: "live_route_setting_return",
    0x0663fe82: "live_route_service_return",
    0x0663fed0: "center_table_trigger_matched",
    0x0663fee4: "center_table_live_state_return",
    0x0663ff54: "center_table_input_guard_return",
    0x0663ff6a: "center_table_alternate_guard_return",
    0x0663ff70: "center_table_busy_guard_return",
    0x0663ff84: "center_table_mode_guard_return",
    0x0663ff94: "center_table_handler_call",
    0x0682d0e0: "original_modal_key_dispatch",
    0x0682e248: "original_timezone_prompt_key_handler",
    0x068b728e: "original_timezone_setup_entry",
    0x068b7b3c: "original_timezone_scene_setup",
}
KEY_ROUTE_PAYLOAD = (0x066431de, 0x0663eb5a, 0x0663fe34)
KEY_ROUTE_CENTER_TABLE = frozenset(pc for pc in KEY_ROUTE if 0x0663fed0 <= pc <= 0x0663ff94)
OBSERVERS = (POOL_COPY_DONE, RECEIVE_RETURN, KEY_DISPATCH, KEY_DISPATCH_RETURN,
             INPUT_HANDLER_BANK0, INPUT_HANDLER_BANK1, GUI_KEY_EMITTER,
             GUI_EVENT, CENTER_LIVE_HANDLER, CENTER_TRANSITION) + tuple(KEY_ROUTE)


class KeyProbeError(RuntimeError):
    """Stop the bounded experiment; do not assume a key was delivered."""


class KeyProbe:
    def __init__(self, emu, *, return_address=0x0001be00,
                 call_timeout=2.0, consumption_timeout=5.0):
        if return_address & 1 or not 0x10000 <= return_address < 0x20000:
            raise ValueError("Reserve an even return trap in helper RAM10000..1ffff")
        if return_address in (0x1bf00, 0x10800):
            raise ValueError("Return trap conflicts with existing boot/delay helpers")
        if not 0 < call_timeout <= 10 or not 0 < consumption_timeout <= 30:
            raise ValueError("Use bounded call/consumption deadlines")
        self.emu, self.return_address = emu, return_address
        self.call_timeout, self.consumption_timeout = call_timeout, consumption_timeout
        self._breakpoints = set()
        self._request = self._saved = None
        self._deadline = None
        self._inside_dispatch = False
        self._fp_layout = None
        self.report = {"scope": "Explicit original software-key input, not GPIO; local display experiment remains partial",
                       "original_instructions_modified": False,
                       "tcb_or_event_fields_modified": False,
                       "requests": [], "menu_opened_verified": False}

    @property
    def breakpoints(self):
        return frozenset(self._breakpoints)

    @property
    def active(self):
        return self._request is not None

    def _u32(self, address):
        return struct.unpack("<I", self.emu.read_mem(address, 4))[0]

    def _task(self, cpu):
        tcb = self._u32(0x20040 + cpu * 4)
        if not 0x08000000 <= tcb <= 0x1ffffeb0:
            raise KeyProbeError("Current task pointer is outside expected guest RAM")
        return tcb

    def _add(self, address):
        if address not in self._breakpoints:
            self.emu.breakpoint_add(address)
            self._breakpoints.add(address)

    def _remove(self, address):
        if address in self._breakpoints:
            self.emu.breakpoint_remove(address)
            self._breakpoints.remove(address)

    def _clear(self):
        for address in tuple(self._breakpoints):
            self._remove(address)

    def arm(self, key, action, *, swaps_observed, wait_for_ui_active=False,
            wait_for_mode35_ack=False, wait_for_transition_complete=False,
            wait_for_input_route_ready=False):
        if self.active:
            raise KeyProbeError("Previous key request has not finished")
        # Accept named subset only; never accept arbitrary parser text/indexes.
        if key in ("UP", "DOWN", "LEFT", "RIGHT", "PLAY", "Q_MENU", "SET"):
            key = "KEY_" + key
        if key not in KEYS or type(action) is not int or action not in (0, 1):
            raise ValueError("Only supported keys and action0(release)/1(press) are accepted")
        if type(swaps_observed) is not int or swaps_observed < 1:
            raise KeyProbeError("Require an actually observed EGL swap before arming")
        if type(wait_for_ui_active) is not bool:
            raise ValueError("wait_for_ui_active must be a boolean")
        if type(wait_for_mode35_ack) is not bool:
            raise ValueError("wait_for_mode35_ack must be a boolean")
        if type(wait_for_transition_complete) is not bool:
            raise ValueError("wait_for_transition_complete must be a boolean")
        if type(wait_for_input_route_ready) is not bool:
            raise ValueError("wait_for_input_route_ready must be a boolean")
        index, bank, mask = KEYS[key]
        self._request = {"key": key, "index": index, "action": action,
                         "bank": bank, "mask": hex(mask), "timeout_argument": 0,
                         "swaps_observed": swaps_observed, "status": "armed",
                         "wait_for_ui_active": wait_for_ui_active,
                         "wait_for_mode35_ack": wait_for_mode35_ack,
                         "wait_for_transition_complete": wait_for_transition_complete,
                         "wait_for_input_route_ready": wait_for_input_route_ready,
                         "queue_accepted": False, "consumed": False, "events": []}
        self.report["requests"].append(self._request)
        self._inside_dispatch = False
        self._deadline = time.monotonic() + self.consumption_timeout
        self._add(LOOP_ENTRY)

    def timeout_hint(self):
        """Maximum seconds until the caller must interrupt/resume-loop again."""
        if self._deadline is None:
            return 30.0
        return max(0.001, self._deadline - time.monotonic())

    def check_timeout(self):
        if self._deadline is not None and time.monotonic() >= self._deadline:
            waiting_ui = self._request.get("status") == "waiting_for_ui_active"
            waiting_ack = self._request.get("status") == "waiting_for_mode35_ack"
            waiting_transition = self._request.get("status") == "waiting_for_transition_complete"
            waiting_route = self._request.get("status") == "waiting_for_input_route_ready"
            self._request["status"] = ("timeout_call_incomplete" if self._saved else
                                       "timeout_waiting_for_ui_active" if waiting_ui else
                                       "timeout_waiting_for_mode35_ack" if waiting_ack else
                                       "timeout_waiting_for_transition_complete" if waiting_transition else
                                       "timeout_waiting_for_input_route_ready" if waiting_route else
                                       "timeout_waiting_for_consumer")
            self._request["unsafe_to_force_context_restore"] = bool(self._saved)
            raise KeyProbeError(self._request["status"])

    def _reject(self, message):
        self._request["status"] = "guard_rejected"
        self._request["error"] = message
        # No inferior call is started after a failed guard.
        self._clear()
        raise KeyProbeError(message)

    def _snapshot_fp(self):
        """Read actual XML numbering; QEMU ARM's g packet may be core-only."""
        if self._fp_layout is None:
            target = self.emu.rsp.read_feature("target.xml")
            includes = re.findall(r'<xi:include\s+href="([^"]+)"', target)
            if includes[:3] != ["arm-core.xml", "arm-neon.xml", "arm-vfp-sysregs.xml"]:
                self._reject("Unexpected target register layout; refuse guessed VFP numbers")
            mapping, number, sources = {}, 0, {}
            for filename in includes[:3]:
                xml = self.emu.rsp.read_feature(filename)
                sources[filename] = hashlib.sha256(xml.encode()).hexdigest()
                for reg in ET.fromstring(xml).iter("reg"):
                    number = int(reg.get("regnum", number))
                    mapping[reg.get("name")] = (number, int(reg.get("bitsize")))
                    number += 1
            names = [f"d{i}" for i in range(32)] + ["fpscr", "fpexc", "fpsid"]
            if any(name not in mapping or mapping[name][1] != (64 if name.startswith("d") else 32)
                   for name in names):
                self._reject("Target XML lacks complete D0..D31/FPSCR/FPEXC/FPSID")
            self._fp_layout = {name: mapping[name] for name in names}
            self.report["register_xml_sha256"] = sources
            self.report["fp_register_numbers"] = {name: value[0] for name, value in self._fp_layout.items()}
        snapshot = {}
        for name, (number, bits) in self._fp_layout.items():
            value = self.emu.rsp.request(f"p{number:x}")
            if len(value) != bits//4 or not re.fullmatch(r"[0-9a-fA-F]+", value):
                self._reject(f"Cannot read complete {name}; no inferior call started")
            snapshot[name] = value.lower()
        return snapshot

    def _restore_fp(self, snapshot):
        # D registers cover the NEON Q aliases. FPSID is recorded/verified only;
        # it is a read-only architectural ID and must never be written.
        for name, value in snapshot.items():
            if name == "fpsid":
                continue
            number = self._fp_layout[name][0]
            if self.emu.rsp.request(f"P{number:x}={value}") != "OK":
                raise KeyProbeError(f"Cannot restore {name}; stop experiment")
        for name, value in snapshot.items():
            number = self._fp_layout[name][0]
            if self.emu.rsp.request(f"p{number:x}").lower() != value:
                raise KeyProbeError(f"Restored {name} differs; stop experiment")

    def _start(self, cpu, regs):
        req, emu = self._request, self.emu
        tcb = self._task(cpu)
        cpsr, sp = regs["cpsr"], regs["r13"]
        checks = {hex(a): self._u32(a) for a in GUARDS + (0x209c0 + cpu*4,)}
        start, end = self._u32(tcb+0xc), self._u32(tcb+0x10)
        req["guards"] = {"cpu": cpu, "tcb": hex(tcb), "task_id": self._u32(tcb+0xc0),
                         "cpsr": hex(cpsr), "stack_start": hex(start), "stack_end": hex(end),
                         "sp": hex(sp), "globals": checks}
        if regs["r15"] != LOOP_ENTRY or self._u32(tcb+0xc0) != INPUT_TASK_ID:
            self._reject("Expected original uiTSK285 queue-loop entry")
        if emu.read_mem(tcb, 4) != b"DRHT" or self._u32(tcb+0xec) != 2:
            self._reject("Input task is not a validated ready/running TCB")
        if (cpsr & 0x3f) != 0x33 or cpsr & (0x80 | 0x0600fc00):
            self._reject("Require Thumb SVC task context, IRQ enabled and no IT block")
        if any(checks.values()):
            self._reject("Interrupt nesting or dispatch-disabled guard is nonzero")
        if sp & 7 or not (0x08000000 <= start <= sp-0x1020 and sp+24 <= end+1 <= 0x20000000):
            self._reject("Original aligned stack must have4KiB free below temporaryargs")
        if self._u32(tcb+0xd8) != 0x06475dd5:
            self._reject("Unexpected input-task entry pointer")
        # Preserve the opaque g packet, plus independently numbered VFP state.
        # Real QEMU9.2 Cortex-A7 returns68 bytes here, excluding all VFP state.
        raw = emu.rsp.request("g")
        if not re.fullmatch(r"[0-9a-fA-F]+", raw) or len(raw) % 2 or len(raw)//2 < 68:
            self._reject("Complete core GDB register packet unavailable")
        fp = self._snapshot_fp()
        call_sp = sp - 32
        old_args = emu.read_mem(call_sp, 32)
        payload = struct.pack("<II", req["index"], req["action"])
        args = struct.pack("<IIII", MESSAGE_CODE, call_sp+16, 8, 0) + payload + b"\0"*8
        self._saved = {"raw_registers": raw, "core": regs, "cpu": cpu, "tcb": tcb,
                       "sp": call_sp, "args_before": old_args, "payload": payload,
                       "fp": fp}
        req.update(status="calling_original_queue", register_packet_bytes=len(raw)//2,
                   register_packet_sha256=hashlib.sha256(bytes.fromhex(raw)).hexdigest(),
                   independent_fp_registers=len(fp), fpsid_read_only=fp["fpsid"],
                   call_sp=hex(call_sp), payload_hex=payload.hex())
        self._remove(LOOP_ENTRY)
        self._add(self.return_address)
        for pc in OBSERVERS:
            self._add(pc)
        emu.write_mem(call_sp, args)
        for name, value in {"sp": call_sp, "r0": 0x115, "r1": 0,
                            "r2": 0x11d, "r3": 0,
                            "lr": self.return_address|1, "pc": QUEUE_API}.items():
            emu.write_reg(name, value)
        self._deadline = time.monotonic() + self.call_timeout

    def _returned(self, cpu, regs):
        saved, req = self._saved, self._request
        if not saved or cpu != saved["cpu"] or self._task(cpu) != saved["tcb"]:
            raise KeyProbeError("Return trap is not in the original calling CPU/task; no restore attempted")
        if self._u32(saved["tcb"]+0xc0) != INPUT_TASK_ID or self._u32(saved["tcb"]+0xd8) != 0x06475dd5:
            raise KeyProbeError("Return task identity changed; no restore attempted")
        if regs["r13"] != saved["sp"] or (regs["cpsr"] & 0x3f) != 0x33:
            raise KeyProbeError("Return stack/mode mismatch; no restore attempted")
        result = regs["r0"]
        self.emu.write_mem(saved["sp"], saved["args_before"])
        if self.emu.rsp.request("G" + saved["raw_registers"]) != "OK":
            raise KeyProbeError("Cannot restore original full register packet; stop experiment")
        self._restore_fp(saved["fp"])
        restored = self.emu.registers()
        if restored != saved["core"]:
            raise KeyProbeError("Restored core registers differ; stop experiment")
        self._saved = None
        self._remove(self.return_address)
        self._remove(POOL_COPY_DONE)
        req.update(return_value=hex(result), registers_restored=True, fp_registers_restored=True,
                   stack_arguments_restored=True,
                   queue_accepted=result == 0,
                   status="waiting_for_original_consumer" if result == 0 else "original_queue_rejected")
        if result:
            self._clear()
            self._request = None
            self._deadline = None
        else:
            self._deadline = time.monotonic() + self.consumption_timeout

    def _step_original(self, pc):
        self._remove(pc)
        try:
            self.emu.step(timeout=min(0.5, self.timeout_hint()))
        finally:
            if self.active:
                self._add(pc)

    def handle(self, pc, cpu):
        """Handle an owned stop on the caller-selected CPU; returnFalse otherwise."""
        pc &= ~1
        if pc not in self._breakpoints:
            return False
        self.check_timeout()
        if self.emu.selected_cpu != cpu:
            raise KeyProbeError("Caller must select the stopped CPU before handle")
        regs = self.emu.registers()
        if pc == LOOP_ENTRY:
            waiting = None
            if self._request["wait_for_ui_active"]:
                state, mode = self._u32(0x08a928b4), self._u32(0x08a928bc)
                gate = self._request.setdefault("ui_active_gate", {"samples": 0, "wait_steps": 0})
                gate.update(samples=gate["samples"]+1, state=state, mode=mode)
                if state == 0 or mode == 6:
                    # Run the next original instruction; do not call the queue,
                    # change UI state, or extend the existing bounded deadline.
                    gate["wait_steps"] += 1
                    waiting = "waiting_for_ui_active"
                else:
                    gate["passed"] = True
            if self._request["wait_for_mode35_ack"]:
                mask = self.emu.read_mem(0x08aa1103, 1)[0]
                gate = self._request.setdefault("mode35_ack_gate", {
                    "address": "0x08aa1103", "required_low_mask": 15,
                    "samples": 0, "wait_steps": 0})
                gate.update(samples=gate["samples"]+1, mask=mask, low_mask=mask & 15)
                if mask & 15 != 15:
                    gate["wait_steps"] += 1
                    waiting = waiting or "waiting_for_mode35_ack"
                else:
                    gate["passed"] = True
            if self._request["wait_for_transition_complete"]:
                values = {"step": self._u32(0x08cb0ee0), "stage": self._u32(0x08aa1120),
                          "state": self._u32(0x08a928b4), "mode": self._u32(0x08a928bc)}
                gate = self._request.setdefault("transition_complete_gate", {
                    "samples": 0, "wait_steps": 0,
                    "required": {"step": 0x4a0, "stage": 0, "state": 1, "mode": 1}})
                gate.update(samples=gate["samples"]+1, **values)
                if values != gate["required"]:
                    gate["wait_steps"] += 1
                    waiting = waiting or "waiting_for_transition_complete"
                else:
                    gate["passed"] = True
            if self._request["wait_for_input_route_ready"]:
                # Exact read-only predicates of original066bfd22 and
                # 066adcfc ->0682e5d0, called by066431de before routing keys.
                stage = self._u32(0x08aa1120)
                pending = self._u32(0x08aa1124)
                secondary = self._u32(0x08aa090c)
                primary_ready = pending == 0xff and stage in (0, 1)
                secondary_ready = secondary in (0, 1)
                gate = self._request.setdefault("input_route_gate", {
                    "samples": 0, "wait_steps": 0,
                    "original_primary_getter": "0x066bfd22",
                    "original_secondary_getter": "0x0682e5d0",
                    "required": "pending_mode==0xff and primary_stage in{0,1} and secondary_stage in{0,1}"})
                gate.update(samples=gate["samples"]+1, primary_stage=stage,
                            pending_mode=pending, secondary_stage=secondary,
                            primary_ready=primary_ready, secondary_ready=secondary_ready)
                if not (primary_ready and secondary_ready):
                    gate["wait_steps"] += 1
                    waiting = waiting or "waiting_for_input_route_ready"
                else:
                    gate["passed"] = True
            if waiting:
                self._request["status"] = waiting
                self._step_original(pc)
                return True
            self._start(cpu, regs)
            return True
        if pc == self.return_address:
            self._returned(cpu, regs)
            return True
        req = self._request
        tcb = self._task(cpu)
        if self._u32(tcb+0xc0) != INPUT_TASK_ID:
            self._step_original(pc)
            return True
        event = None
        if pc == POOL_COPY_DONE and self._saved:
            if tcb == self._saved["tcb"] and regs["r13"] == self._saved["sp"]-0x60:
                pointer = self._u32(regs["r13"]+0x20)
                if self.emu.read_mem(pointer, 8) != self._saved["payload"]:
                    raise KeyProbeError("Original pool copy did not match requestedkey")
                req["original_payload_pointer"] = hex(pointer)
                event = {"kind": "original_pool_copy", "payload": hex(pointer)}
        pointer = int(req.get("original_payload_pointer", "0"), 16)
        if pc == RECEIVE_RETURN and pointer and regs["r0"] == 0:
            words = struct.unpack("<5I", self.emu.read_mem(regs["r13"], 20))
            if words[2] == MESSAGE_CODE and words[3] == pointer:
                event = {"kind": "original_queue_receive", "words": [hex(x) for x in words]}
        if pc == KEY_DISPATCH and pointer and regs["r0"] == pointer:
            if self.emu.read_mem(pointer, 8).hex() != req["payload_hex"]:
                raise KeyProbeError("Payload changed before original key dispatcher")
            self._inside_dispatch = True
            event = {"kind": "original_key_dispatch", "payload": hex(pointer)}
        if self._inside_dispatch:
            if pc in KEY_ROUTE:
                # Observe the original decision, never alter its return value.
                # Table callbacks are shared by many keys; identify CENTER by
                # its exact original table slot, not merely a nonzero mask.
                if pc not in KEY_ROUTE_CENTER_TABLE or regs["r4"] == 0x06f10554:
                    event = {"kind": "original_key_route", "route": KEY_ROUTE[pc],
                             "r0_r2": [hex(regs[f"r{i}"]) for i in range(3)],
                             "table_r4": hex(regs["r4"]),
                             "gate_values": {
                                 "transition_stage": self._u32(0x08aa1120),
                                 "pending_mode": hex(self._u32(0x08aa1124)),
                                 "secondary_stage": self._u32(0x08aa090c),
                                 "input_enable_byte": self.emu.read_mem(0x08aa0f96, 1)[0],
                                 "input_gate_word": self._u32(0x08aa0ff8),
                                 "state": self._u32(0x08a928b4),
                                 "mode": self._u32(0x08a928bc),
                                 "modal_id": struct.unpack("<H", self.emu.read_mem(0x08a92898, 2))[0],
                             }}
                    if pc in KEY_ROUTE_PAYLOAD:
                        pointer = regs["r0"]
                        start, end = self._u32(tcb+0xc), self._u32(tcb+0x10)
                        if not start <= pointer <= end+1-0x58:
                            raise KeyProbeError("Original local key payload is outside the input-task stack")
                        payload = self.emu.read_mem(pointer, 0x58)
                        event.update(local_payload_pointer=hex(pointer), local_payload_hex=payload.hex(),
                                     local_payload_words=[hex(x) for x in struct.unpack("<22I", payload)])
            elif pc in (INPUT_HANDLER_BANK0, INPUT_HANDLER_BANK1):
                event = {"kind": "original_input_state_handler", "mask": hex(regs["r0"]),
                         "internal_action": regs["r1"], "bank": int(pc == INPUT_HANDLER_BANK1)}
            elif pc in (GUI_KEY_EMITTER, GUI_EVENT, CENTER_LIVE_HANDLER, CENTER_TRANSITION):
                event = {"kind": {GUI_KEY_EMITTER:"original_ui_key", GUI_EVENT:"original_ui_event",
                                   CENTER_LIVE_HANDLER:"original_center_live_handler",
                                   CENTER_TRANSITION:"original_state_transition_request"}[pc],
                         "r0_r2": [hex(regs[f"r{i}"]) for i in range(3)],
                         "ui_state_words": [hex(self._u32(0x08a928b0+i)) for i in (0,4,8,12)]}
            elif pc == KEY_DISPATCH_RETURN:
                event = {"kind": "original_key_dispatch_returned", "return_r0": hex(regs["r0"])}
                req.update(consumed=True, status="consumed_by_original_dispatcher")
        if event:
            event.update(pc=hex(pc), cpu=cpu, tcb=hex(tcb))
            if len(req["events"]) >= 64:
                raise KeyProbeError("Key-consumer event limit exceeded")
            req["events"].append(event)
        if req["consumed"]:
            self._clear()
            self._request = None
            self._inside_dispatch = False
            self._deadline = None
        else:
            self._step_original(pc)
        return True

    def close(self):
        """Remove owned breakpoints; never force-restore an unfinished call."""
        self._clear()
        if self._request is not None:
            self._request["closed"] = True
            self._request["close_state"] = "closed_with_incomplete_call" if self._saved else "closed_before_consumption"
            self._request["unsafe_to_force_context_restore"] = bool(self._saved)

