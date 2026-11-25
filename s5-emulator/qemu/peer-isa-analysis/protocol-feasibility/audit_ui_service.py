#!/usr/bin/env python3
"""Reproduce a narrow original-ARM message-service contract, offline only.

No QEMU connection, process execution, firmware patch, mailbox or RAM write.
The range labels are manually audited function/basic-block boundaries. This is
not a general decompiler. Use the project's lab/.venv Python for Capstone.
"""
import hashlib
import json
import mmap
from pathlib import Path
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB

HERE = Path(__file__).resolve().parent
EMU = HERE.parents[2]
PROGRAM = EMU.parent / "s5-v29-offline/unpacked-v29/03_program.bin"
SNAPSHOT = EMU / "menu-smp/results/isolated-display-gradient/ram.bin"
BASE = 0x06280000
EXPECTED_SHA = "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"

# Ends exclude known literal pools and the code36 TBB table. Other cases have
# intentionally not been decoded or approved for the local-service experiment.
RANGES = [
    ("task66 prologue and receive loop", 0x675ef84, 0x675efe0, False),
    ("receive wrapper", 0x67ea67a, 0x67ea6d4, False),
    ("RTOS receive validation", 0x6722e1c, 0x6722e64, False),
    ("RTOS receive object lookup", 0x6722c60, 0x6722c8a, False),
    ("RTOS receive queued pointer", 0x6722d1e, 0x6722d48, False),
    ("RTOS receive return", 0x6722e06, 0x6722e1c, False),
    ("pool release validation", 0x65a2be8, 0x65a2c44, False),
    ("message dispatch type zero", 0x675f154, 0x675f168, False),
    ("message dispatch type-zero tail", 0x675f18a, 0x675f192, False),
    ("message dispatch source", 0x675eed2, 0x675ef48, False),
    ("message dispatch source return", 0x675ef80, 0x675ef84, False),
    ("code dispatcher before TBB data", 0x675eb8e, 0x675eba8, False),
    ("code36 selected target", 0x675ec7a, 0x675ec80, False),
    ("dispatcher handled return", 0x675eec8, 0x675eece, False),
    ("code36 mode selection", 0x653b7e6, 0x653b824, False),
    ("ordinary host-state byte", 0x653efea, 0x653eff0, False),
    ("original reply helper", 0x6759710, 0x6759778, False),
    ("reply local IRQ disable", 0x6680752, 0x668076c, False),
    ("reply IRQ state restore", 0x66807d4, 0x66807e4, False),
    ("reply barrier leaf", 0x66c2230, 0x66c2238, False),
    ("task/interrupt discriminator", 0x65477bc, 0x65477ca, False),
    ("system state per CPU", 0x68f8db8, 0x68f8de0, True),
    ("task-context queue send", 0x657b7f4, 0x657b82e, False),
    ("interrupt-context queue send", 0x65968d4, 0x659690e, False),
    ("RTOS queue send", 0x66f254c, 0x66f2716, False),
]


def main():
    p = PROGRAM.read_bytes()
    sha = hashlib.sha256(p).hexdigest()
    assert sha == EXPECTED_SHA, "Original source identity changed"

    def u32(a):
        return struct.unpack_from("<I", p, a - BASE)[0]

    constants = {hex(a): hex(u32(a)) for a in
                 (0x6f492b0, 0x6f49a28, 0x6f48f88, 0x6f49c64)}
    assert list(constants.values()) == ["0x11d", "0x76", "0x42", "0x115"]
    tbb_base, selector = 0x675eba8, 0x36
    entry = p[tbb_base + selector - BASE]
    target = tbb_base + 2 * entry
    assert entry == 0x69 and target == 0x675ec7a
    lines = [f"Original program SHA256: {sha}",
             "Offline selected ARM/Thumb ranges; labels/semantics are audited annotations.",
             f"TBB table data: base={tbb_base:#x}, selector={selector:#x}, "
             f"entry={entry:#x}, target={target:#x}; table is not instructions."]
    for label, a, b, arm in RANGES:
        c = Cs(CS_ARCH_ARM, CS_MODE_ARM if arm else CS_MODE_THUMB)
        c.detail = True
        lines.append(f"\n{label}: {a:#010x}..{b:#010x} {'ARM' if arm else 'Thumb'}")
        for i in c.disasm(p[a - BASE:b - BASE], a):
            note = ""
            if i.mnemonic.startswith("ldr") and "[pc," in i.op_str:
                lit = ((i.address + (8 if arm else 4)) & ~3) + i.operands[-1].mem.disp
                if BASE <= lit < BASE + len(p) - 4:
                    note = f" ; [{lit:#010x}]={u32(lit):#010x}"
            lines.append(f"{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}{note}")
    (HERE / "ui-service-disassembly.txt").write_text("\n".join(lines) + "\n")

    snapshot = {}
    if SNAPSHOT.exists():
        with SNAPSHOT.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            def u(a):
                return struct.unpack_from("<I", m, a)[0]
            for q in (0x76, 0x115):
                obj = u(0x8e1b94c + (q - 1) * 4)
                cap, array, write, read, count = [u(obj + x) for x in (4, 12, 16, 20, 24)]
                row = dict(object=hex(obj), capacity=cap, array=hex(array),
                           write_index=write, read_index=read, count=count,
                           waiting_receivers=hex(u(obj + 28)), waiting_senders=hex(u(obj + 32)))
                if count:
                    head = u(array + 4 * read)
                    row.update(head=hex(head), words=[hex(u(head + 4 * i)) for i in range(5)],
                               allocation_prefix=hex(u(head - 4)))
                snapshot[hex(q)] = row
            snapshot.update(max_queues=u(0x8aa7fe0), reply_ring_index=u(0x8aab9d4),
                            pool6_object=hex(u(0x8e1e1a4 + 5 * 4)),
                            task66_buffer_words=[hex(u(0xa603680 + i * 4)) for i in range(5)],
                            system_state_by_cpu=[hex(u(0x209c0 + i * 4)) for i in range(4)])
            assert snapshot["0x76"]["words"] == ["0x115", "0x11d", "0x36", "0x1", "0x0"]
            assert snapshot["0x76"]["allocation_prefix"] == snapshot["pool6_object"]

    contract = {
        "scope": "Static contract and pre-existing snapshot audit only; not a successful runtime experiment",
        "program": str(PROGRAM), "program_sha256": sha,
        "snapshot": str(SNAPSHOT), "snapshot_observations": snapshot,
        "original_rom_constants": constants,
        "exact_request_words": ["0x115", "0x11d", "0x36", "0x1", "0x0"],
        "task66": {"entry_thumb": "0x0675ef85", "original_peer_wait_pc": "0x065491ee",
                   "static_context": "0x0a603680", "context_bytes": 0xe08,
                   "prologue_zeroes_context_before_incomplete_init": True},
        "peek": {"queue": "0x76", "object": "u32(0x08e1b94c + (queue-1)*4)",
                 "capacity_offset": 4, "array_offset": 12, "write_index_offset": 16,
                 "read_index_offset": 20, "count_offset": 24,
                 "waiting_receivers_offset": 28, "waiting_senders_offset": 32,
                 "head_envelope": "u32(u32(object+12) + 4*u32(object+20))",
                 "selected_path_requires_waiting_senders_zero": True},
        "receive": {"entry_thumb": "0x067ea67b", "r0_queue": "0x76",
                    "r1_original_destination": "0x0a603680", "r2_timeout": 0,
                    "success_return": 0, "empty_return": -2, "other_failure_return": -1,
                    "copied_bytes": 20, "release_call": "0x065a2be8(pool=6,envelope)",
                    "release_return_ignored_by_wrapper": True,
                    "caller_must_not_release_again": True},
        "dispatch": {"entry_thumb": "0x0675f155", "r0": "0x0a603680",
                     "type_guard": "u16(message+0x10)==0", "source_guard": "u32(message+4)==0x11d",
                     "code_guard": "u32(message+8)==0x36", "mode_guard": "u32(message+12)==1",
                     "tbb_table": hex(tbb_base), "tbb_entry": hex(entry), "selected_target": hex(target),
                     "mode_handler_thumb": "0x0653b7e7", "handled_return": 1,
                     "handled_does_not_prove_reply_send_success": True,
                     "caller_cleanup": "None for this zero-payload handled request; original loop receives next"},
        "reply": {"created_by_original_firmware_only": True,
                  "helper_thumb": "0x06759711", "helper_arguments": ["0x36", "0x1", "0x0"],
                  "original_generated_words": ["0x76", "0x42", "0x36", "0x0", "0x1"],
                  "destination_queue": "0x115", "ring": "0x08e9f518", "slot_bytes": 20,
                  "ring_index_address": "0x08aab9d4", "ring_slots": 128,
                  "send_timeout": 0, "send_return_observation_pc": "0x06759766",
                  "required_send_return_r0": 0,
                  "send_failure": "Original helper invokes 0x06666240(error=0x100f)",
                  "send_ownership": "Underlying RTOS passes the original ring pointer; no probe allocation or free"},
        "dependency_result": {
            "exact_mode1_path_references_peer_shared_ram": False,
            "exact_mode1_path_references_mmio": False,
            "ordinary_state_byte_read": "0x08a9da70; both values take identical reply path when mode=1",
            "dependencies": ["Initialized RTOS queues and pool6", "Actual task66 task context and stack",
                             "Original reply ring index in [0,127]", "Working SMP scheduler and locks"],
            "validation_level": "Static positive evidence for this single message; runtime still required"},
        "guards": [
            "Separate opt-in VM experiment; preserve strict baseline",
            "Stop at actual task66 PC 0x065491ee, not an IRQ sample or arbitrary task",
            "u32(0x209c0 + cpu_index*4)==0 and current TCB is task66",
            "Save all GPRs/CPSR/SP/LR; use existing task stack and 8-byte call alignment",
            "Validate live queue map, bounds, nonzero count and no waiting senders; exact head tuple only",
            "Require pool6 ownership prefix for this original envelope",
            "Receive exactly once with timeout0 and require return0 plus exact copied tuple",
            "Run only original dispatch and require original send return0 as well as handled return1",
            "No direct ACK, event-bit write, TCB rewrite, peer-ready/magic/shared-pointer fabrication",
            "After both calls return, restore saved CPU call context to peer wait; retain original queue/ring side effects",
            "Do not rewind scheduler/RAM after a partially completed call; on error/timeout stop this VM",
            "No claims about other modes/codes, DSP startup, full boot or physical camera behavior"
        ]
    }
    (HERE / "ui-service-contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    print(json.dumps({"source_sha256": sha, "tbb_target": hex(target),
                      "snapshot_verified": bool(snapshot), "runtime_executed": False,
                      "outputs": ["ui-service-contract.json", "ui-service-disassembly.txt"]}, indent=2))


if __name__ == "__main__":
    main()
