#!/usr/bin/env python3
"""Audit original queue76 command 0x2b offline. Does not execute the handler."""
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
    entry = p[0x675eba8 + 0x2b - BASE]
    assert entry == 0xed and 0x675eba8 + 2 * entry == 0x675ed82
    assert p[0x675ed82 - BASE:0x675ed84 - BASE].hex() == "31e0"
    assert u(0x675f1cc) == 0x090c92e8
    assert p[0x675eb94 - BASE:0x675eb96 - BASE].hex() == "0025"
    assert p[0x675ede8 - BASE:0x675edf0 - BASE].hex() == "f84880f8c6506be0"
    ranges = [
        ("outer type-zero dispatcher", 0x675f154, 0x675f162),
        ("type-zero tail", 0x675f18a, 0x675f192),
        ("source11d dispatch", 0x675eed2, 0x675eee6),
        ("source dispatch return bridge", 0x675ef12, 0x675ef16),
        ("source dispatch return", 0x675ef80, 0x675ef84),
        ("code dispatcher r5 initialization and TBB", 0x675eb8e, 0x675eba8),
        ("selected code2b branch", 0x675ed82, 0x675ed84),
        ("entire code2b body", 0x675ede8, 0x675edf0),
        ("handled return", 0x675eec8, 0x675eece),
        ("task loop handled cleanup decision", 0x675efbc, 0x675efd6),
    ]
    lines = [f"Program SHA256 {EXPECTED_SHA}",
             "Exact request {115,11d,2b,0,0}; table byte ed -> 675ed82 -> 675ede8.",
             "No handler execution; selected instruction ranges only."]
    for label, first, end in ranges:
        c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        c.detail = True
        lines.append(f"\n{label}: {first:#010x}..{end:#010x}")
        for i in c.disasm(p[first - BASE:end - BASE], first):
            suffix = ""
            if i.mnemonic.startswith("ldr") and "[pc," in i.op_str:
                lit = ((i.address + 4) & ~3) + i.operands[-1].mem.disp
                suffix = f" ; [{lit:#010x}]={u(lit):#010x}"
            lines.append(f"{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}{suffix}")
    (HERE / "command-2b-disassembly.txt").write_text("\n".join(lines) + "\n")

    snapshot_path = EMU / "menu-smp/results/local-ui-service-first/ram.bin"
    with snapshot_path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        read = lambda a: struct.unpack_from("<I", m, a)[0]
        obj = read(0x8e1b94c + (0x76 - 1) * 4)
        capacity, array, read_index, count = [read(obj + x) for x in (4, 12, 20, 24)]
        assert capacity <= 0x10000 and count <= capacity
        queue = []
        for index in range(count):
            envelope = read(array + 4 * ((read_index + index) % capacity))
            queue.append({"position": index, "envelope": hex(envelope),
                          "words": [hex(read(envelope + i * 4)) for i in range(5)],
                          "pool_prefix": hex(read(envelope - 4))})
        expected = ["0x115", "0x11d", "0x2b", "0x0", "0x0"]
        matching = [row for row in queue if row["words"] == expected]
        assert len(matching) == 1 and matching[0]["position"] == 1
        assert matching[0]["pool_prefix"] == hex(read(0x8e1e1a4 + 5 * 4))
        snapshot = {"path": str(snapshot_path), "queue_object": hex(obj),
                    "count": count, "capacity": capacity, "read_index": read_index,
                    "messages": queue, "byte_0x090c93ae": m[0x90c93ae],
                    "unused_context_words": [hex(read(0xa604474 + x * 4)) for x in range(2)]}

    contract = {
        "scope": "Offline original-code and saved-RAM audit only; no VM, guest mutation or allowlist change",
        "program": str(PROGRAM), "program_sha256": EXPECTED_SHA,
        "exact_request_words": expected,
        "snapshot": snapshot,
        "receive": {"same_as": "ui-service-contract.json receive/peek",
                    "entry_thumb": "0x067ea67b", "arguments": ["0x76", "0x0a603680", "0x0"],
                    "success_return": 0, "copies_bytes": 20,
                    "original_pool6_release": True, "caller_extra_free": False},
        "dispatch": {"entry_thumb": "0x0675f155", "r0": "0x0a603680",
                     "type_halfword": 0, "source": "0x11d", "code": "0x2b",
                     "tbb_base": "0x0675eba8", "tbb_entry": "0xed",
                     "tbb_target": "0x0675ed82", "body": "0x0675ede8..0x0675edee",
                     "initialized_r5": 0, "store": {"address": "0x090c93ae", "width_bytes": 1, "value": 0},
                     "return": 1, "extra_cleanup": "None: original task loop immediately receives next"},
        "ack": {"generated": False, "send_calls": [],
                "success_meaning": "Handled means original byte store and return only, not an ACK"},
        "dependencies": {"peer_shared_ram_or_ready": False, "mmio": False,
                         "handler_calls": [], "payload_words_used": False,
                         "ordinary_ram": ["0x090c93ae"],
                         "dispatcher_unused_loads": ["0x0a604474", "0x0a604478"],
                         "required": ["Original task66 context", "Initialized RTOS queue76 and pool6",
                                      "Mapped ordinary RAM and correct original program identity"]},
        "limits": [
            "The byte's broader application meaning is not identified; this contract only establishes exact effect",
            "Current snapshot already has byte zero; serving this request is not expected to alter that snapshot value",
            "This request cannot directly emit the missing code35 acknowledgement at GUI step436",
            "Do not skip the queued code35 predecessor to reach code2b; re-peek the real live head",
            "A future experiment must retain the prior context/stack/queue/pool guards and only run original code",
            "Require original receive success and pool release success, exact copied tuple, selected body and dispatch return1",
            "A byte-zero outcome alone is not execution evidence because the original value may already be zero",
            "No runtime test or expanded allowlist has been performed by this audit"
        ]
    }
    (HERE / "command-2b-contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    print(json.dumps({"verified": True, "runtime_executed": False,
                      "handler_effect": "original STRB 0 to 0x090c93ae", "sends_ack": False,
                      "snapshot_queue_position": matching[0]["position"]}, indent=2))


if __name__ == "__main__":
    main()
