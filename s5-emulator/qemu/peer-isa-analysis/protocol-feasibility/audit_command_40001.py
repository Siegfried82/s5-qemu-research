#!/usr/bin/env python3
"""Read-only contract audit for an original fixed-slot worker response."""
import hashlib
import json
import mmap
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA


def main():
    p = PROGRAM.read_bytes()
    assert hashlib.sha256(p).hexdigest() == EXPECTED_SHA
    u = lambda a: struct.unpack_from("<I", p, a - BASE)[0]
    tables = []
    for base, index, byte, target in [(0x675ea04, 0, 0x24, 0x675ea4c),
                                     (0x66d13bc, 0, 5, 0x66d13c6),
                                     (0x651dc48, 0, 0x28, 0x651dc98),
                                     (0x651e5d8, 1, 4, 0x651e5e0)]:
        assert p[base + index - BASE] == byte and base + 2 * byte == target
        tables.append({"base": hex(base), "index": index, "byte": hex(byte), "target": hex(target)})
    assert u(0x651e704) - 0x14 == 0x8e9dac4
    assert u(0x651e6f4) - 0x18 == 0x8aab920
    assert u(0x651e70c) - 0x23 == 0x40001
    assert u(0x6f49a40) == 0x7f and u(0x6f48fa8) == 0x4a
    ranges = [
        ("outer dispatch type/source guards", 0x675f154, 0x675f168),
        ("type1 source4a tail", 0x675f192, 0x675f1a4),
        ("code40001 dispatcher before table", 0x675e9e8, 0x675ea04),
        ("code40001 selected call", 0x675ea4c, 0x675ea56),
        ("handled return", 0x675eaa4, 0x675eaa8),
        ("controller byte selector before table", 0x66d13b0, 0x66d13bc),
        ("entire selected controller-byte body", 0x66d13c6, 0x66d13ca),
        ("original task66 command40001 send", 0x66d0594, 0x66d05bc),
        ("task74 classifier source42", 0x651dd80, 0x651dd92),
        ("task74 code classifier before table", 0x651dc30, 0x651dc48),
        ("task74 classification result1", 0x651dc98, 0x651dc9c),
        ("task74 dispatcher", 0x651e4a8, 0x651e4be),
        ("task74 secondary dispatcher before table", 0x651e5c8, 0x651e5d8),
        ("task74 selected worker call", 0x651e5e0, 0x651e5ea),
        ("worker response and fixed slot", 0x651e662, 0x651e6d4),
        ("original response constructor", 0x651d838, 0x651d860),
        ("original receive copy/release", 0x67ea6be, 0x67ea6d4),
        ("pool ownership mismatch validation", 0x65a2be8, 0x65a2c40),
        ("pool ownership mismatch return", 0x65a2cdc, 0x65a2ce4),
    ]
    lines = [f"Original SHA256 {EXPECTED_SHA}",
             "Offline selected paths; TBB bytes are reported separately and excluded from instruction ranges."]
    for label, start, end in ranges:
        c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        c.detail = True
        lines.append(f"\n{label}: {start:#x}..{end:#x}")
        for i in c.disasm(p[start - BASE:end - BASE], start):
            note = ""
            if i.mnemonic.startswith("ldr") and "[pc," in i.op_str:
                literal = ((i.address + 4) & ~3) + i.operands[-1].mem.disp
                note = f" ; [{literal:#010x}]={u(literal):#010x}"
            lines.append(f"{i.address:08x} {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}{note}")
    (HERE / "command-40001-disassembly.txt").write_text("\n".join(lines) + "\n")
    snapshot_path = EMU / "menu-smp/results/local-ui-mode35-2g/ram.bin"
    with snapshot_path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        read = lambda a: struct.unpack_from("<I", m, a)[0]
        obj = read(0x8e1b94c + (0x76 - 1) * 4)
        array, rd, count = read(obj + 12), read(obj + 20), read(obj + 24)
        envelope = read(array + 4 * rd)
        words = [read(envelope + i * 4) for i in range(5)]
        assert envelope == 0x8e9dac4 and words == [0x7f, 0x4a, 0x40001, 0x8aab920, 1]
        assert read(envelope - 4) == 0 and read(0x8e1e1a4 + 5 * 4) != 0
        snapshot = {"path": str(snapshot_path), "queue_object": hex(obj), "count": count,
                    "read_index": rd, "envelope": hex(envelope), "prefix": 0,
                    "words": [hex(x) for x in words], "pool6_object": hex(read(0x8e1e1a4 + 5 * 4)),
                    "controller_state": read(0x8aac2e4), "controller_byte15": m[0x8aac2f9],
                    "payload_word": read(0x8aab920)}
    contract = {
        "scope": "Offline original-code and saved-RAM audit; no guest/model/probe or allowlist change",
        "program_sha256": EXPECTED_SHA, "snapshot": snapshot, "tbb_paths": tables,
        "exact_request_words": ["0x7f", "0x4a", "0x40001", "0x08aab920", "0x1"],
        "ownership": {"kind": "original fixed static message slot, not a pool6 allocation",
                      "slot": "0x08e9dac4", "bytes": 20, "observed_prefix": 0,
                      "producer": "0x0651e662, selected response construction at 0x0651e6b4..0x0651e6c6",
                      "slot_derivation": "literal[0x0651e704]=0x08e9dad8 minus0x14",
                      "payload_derivation": "literal[0x0651e6f4]=0x08aab938 minus0x18",
                      "code_derivation": "literal[0x0651e70c]=0x40024 minus0x23",
                      "constructor": "0x0651d838: original ROM constants7f/4a plus code/payload/type; calls original RTOSsend",
                      "heap_release": "Original receive calls pool6release, whose prefix mismatch returns -17; wrapper ignores it",
                      "expected_pool_release_return": "0xffffffef", "caller_extra_free": False,
                      "general_nonpool_messages_authorized": False},
        "receive": {"entry_thumb": "0x067ea67b", "arguments": ["0x76", "0x0a603680", 0],
                    "copy_bytes": 20, "expected_return": 0,
                    "note": "Use exact static-slot ownership guard instead of the prior heap-only prefix/release0 guard"},
        "dispatch": {"entry_thumb": "0x0675f155", "r0": "0x0a603680",
                     "type": 1, "source": "0x4a", "code": "0x40001",
                     "selected_call": "0x0675ea50 -> 0x066d13b0(r0=9,r1=1)",
                     "selected_body": "0x066d13c6 STRB r1,[r2,#0x15]; 0x066d13c8 BX LR",
                     "effect": {"address": "0x08aac2f9", "width_bytes": 1, "value": 1},
                     "expected_return": 1, "payload_dereferenced": False,
                     "ack_sent": False, "mmio_accessed": False, "peer_shared_ram_accessed": False,
                     "additional_payload_or_envelope_cleanup": False},
        "producer_chain": [
            "Original task66 0x066d05aa..0x066d05ac requests code40001 through0x06759778, then clears controller+15",
            "Task74/queue7f original worker0x0651e044 classifies source42 via0x0651dd58->0x0651dc30 as internal1",
            "0x0651e4a8->0x0651e5c8 selected case1 calls0x0651e662",
            "Worker writes result0 at0x08aab920 and uses original fixed response slot0x08e9dac4",
            "0x0651d838 sends the actual response to destination from original request+0 (queue76)"
        ],
        "runtime_guards": [
            "Preserve all prior actual-task66, stack/context and exact live-queue-head guards",
            "Require exact tuple and fixed address08e9dac4; do not skip a different queued message",
            "Require prefix0 differs from nonzero valid pool6 object; expect original release -17 without heap mutation",
            "Require original receive return0 and exact copied20B, then execute only original dispatch",
            "Observe original selector arguments9/1 and actual STRB at66d13c6; no host controller-byte write",
            "Require dispatch return1 and restore original waiting task context only at normal return",
            "No ACK milestone applies to this handler; later controller progression is separate evidence",
            "Do not rerun the producer or fabricate its response; this is consumption of the actual queued reply"
        ],
        "validation_level": "Static contract and existing snapshot identity, not yet a runtime service experiment"
    }
    (HERE / "command-40001-contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    print(json.dumps({"verified": True, "static_slot": hex(envelope),
                      "expected_release": "0xffffffef", "effect": contract["dispatch"]["effect"],
                      "runtime_executed": False}, indent=2))


if __name__ == "__main__":
    main()
