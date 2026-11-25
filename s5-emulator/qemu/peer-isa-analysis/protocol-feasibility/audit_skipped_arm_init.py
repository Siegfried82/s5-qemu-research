#!/usr/bin/env python3
"""Read-only audit of initialization still pending at task66's first peer wait.

No QEMU connection, guest invocation, firmware patch or readiness fabrication.
Function labels are manually audited; this is not a whole-program decompiler.
Run with s5-emulator/lab/.venv/bin/python -B.
"""
import hashlib
import json
import mmap
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA

RANGES = [
    ("task66 initializer call and postinit", 0x675EF84, 0x675EFE0),
    ("outer initializer complete function", 0x676EDE0, 0x676EE3C),
    ("nested init and completion-byte consumer", 0x65C592C, 0x65C595E),
    ("nested caller and independent tail", 0x65850F4, 0x6585102),
    ("first peer wait and pointer publication", 0x65491E4, 0x65493C0),
    ("immediately pending peer-dependent init", 0x657B94C, 0x657BA88),
    ("peer-dependent stores", 0x663AD9A, 0x663ADDE),
    ("pending continuation before literal pool", 0x657BB98, 0x657BD4A),
    ("pending continuation after literal pool", 0x657BD80, 0x657BE0E),
    ("another pending init reaches shared pointer", 0x66CBDF2, 0x66CBEE0),
    ("host callback-ring initialization", 0x6715C74, 0x6715CB6),
    ("command submit then callback registration", 0x6715CBC, 0x6715D36),
    ("peer ring submit complete function", 0x6639B14, 0x6639B6C),
    ("registered callback26", 0x66CDA4A, 0x66CDA58),
    ("callback26 numeric state tail", 0x66F9AC2, 0x66F9AF8),
    ("registered callback36", 0x6715E40, 0x6715E4C),
    ("independent host init complete function", 0x6586DEE, 0x6586E4A),
    ("its state-zeroing leaf", 0x651805C, 0x6518064),
    ("its timer-read wrapper", 0x66C19E2, 0x66C19E8),
    ("timer read", 0x67076D4, 0x67076E4),
    ("timer read implementation", 0x6554D72, 0x6554D9C),
    ("independent pointer/state init complete function", 0x658B890, 0x658B8D4),
    ("calibration table init complete function excluding pool", 0x676EFDC, 0x676F3CA),
    ("calibration search helper complete function", 0x65B25C0, 0x65B263A),
    ("second peer wrapper complete function", 0x6658C2A, 0x6658C38),
    ("second peer upload and wait complete function", 0x64AB520, 0x64AB5CE),
    ("second peer post-upload signal wrapper", 0x64AB808, 0x64AB830),
    ("outer final status setter complete function", 0x659C898, 0x659C8A2),
    ("pending hardware init prefix, not complete function", 0x652E666, 0x652E734),
    ("pending SPI0 command wrapper", 0x6546F68, 0x6546F6E),
    ("pending SPI0 command body", 0x6546E20, 0x6546E80),
]


def main():
    p = PROGRAM.read_bytes()
    sha = hashlib.sha256(p).hexdigest()
    assert sha == EXPECTED_SHA
    c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    c.detail = True
    def u32(a):
        return struct.unpack_from("<I", p, a - BASE)[0]
    def disasm(a, b):
        return list(c.disasm(p[a-BASE:b-BASE], a))
    def calls(a, b):
        return [i.operands[0].imm for i in disasm(a, b) if i.mnemonic == "bl"]

    outer = calls(0x676EDE0, 0x676EE3C)
    assert outer == [0x671268C, 0x64CB15A, 0x6518898, 0x652930C,
                     0x67A288C, 0x661EAC8, 0x66FAFB0, 0x67030C2,
                     0x651F978, 0x6511C44, 0x65C8A0C, 0x653EFEA,
                     0x6728CD2, 0x65C592C, 0x658B890, 0x676EFDC, 0x6658C2A]
    assert calls(0x676EFDC, 0x676F3CA) == [0x65B25C0]
    assert calls(0x6586DEE, 0x6586E4A) == [0x651805C, 0x66C2238, 0x66C19E2]
    assert not calls(0x658B890, 0x658B8D4)
    assert not calls(0x6715C74, 0x6715CB6)
    lines = [f"Original program SHA256: {sha}",
             "Offline selected Thumb ranges, no guest execution or mutation.",
             "Literal annotations are source words, not snapshot runtime values."]
    for label, a, b in RANGES:
        lines.append(f"\n{label}: {a:#010x}..{b:#010x} (end exclusive)")
        for i in disasm(a, b):
            note = ""
            if i.mnemonic.startswith("ldr") and "[pc," in i.op_str:
                lit = ((i.address + 4) & ~3) + i.operands[-1].mem.disp
                if BASE <= lit <= BASE + len(p) - 4:
                    note = f" ; [{lit:#010x}]={u32(lit):#010x}"
            lines.append(f"{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:10} {i.op_str}{note}")
    (HERE / "skipped-arm-init-disassembly.txt").write_text("\n".join(lines) + "\n")

    snapshot_path = EMU / "menu-smp/results/local-frame-paced-menu-timeout10/ram.bin"
    observations = {}
    if snapshot_path.exists():
        with snapshot_path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            fields = [("nested_init_complete", 0x8A670EA, 1),
                      ("peer_published_subpointers", 0x8A9DBDC, 28),
                      ("peer_first_pointer", 0x8A9EA94, 4),
                      ("second_peer_pointer", 0x8AA94E8, 4),
                      ("outer_host_pointer", 0x8A9F368, 8),
                      ("independent_host_structure", 0x8BF2FCE, 16),
                      ("outer_final_status", 0x8AA04B0, 16),
                      ("calibration_halfwords_prefix", 0x8C9F8C0, 16),
                      ("calibration_words_prefix", 0x8CA1854, 16),
                      ("callback_ring_controls", 0x8A9ED0E, 8),
                      ("callback_ring_first_entry", 0x91A25E0, 28),
                      ("later_postinit_enabled", 0x8AA02AF, 1),
                      ("timed_startup_sequence", 0x8AA7F18, 8)]
            for name, a, n in fields:
                observations[name] = {"address": hex(a), "bytes": m[a:a+n].hex()}

    evidence = {
        "scope": "Offline original ARM control-flow audit and read-only existing snapshot; no runtime invocation",
        "program": str(PROGRAM), "program_sha256": sha,
        "snapshot": str(snapshot_path), "snapshot_fields": observations,
        "conclusion": "The partial task66 loop omits substantial peer-dependent initialization and several ARM-only initializers. The audited independent initializers do not send GUI scene messages or install OSD routes. No specific missing queue101 producer has been established by this audit.",
        "already_returned_before_wait": [hex(a) for a in outer[:11]],
        "already_returned_note": "Follows the observed call stack to 65491ee. These are not candidate missing calls; re-running the outer initializer repeats already completed state resets.",
        "wait_stack": ["675efae calls676ede0", "676ee24 calls65c592c",
                       "65c5936 calls65850f4", "65850f6 calls657ba88",
                       "657bb98 calls65491e4", "65491ee polls7b004000"],
        "uncompleted_peer_stages": {
            "65491e4": "First-ready, pointer publication and magic checks, host shared configuration, phase2 wait, additional pointer publication and6638964 shared initialization remain incomplete.",
            "657b94c": "First pending call. 663ad9a writes through u32(08a9dbdc);663b0f8/6638736/6549524 also use peer state. Unsafe with null published pointers.",
            "66cbdf2": "Contains ARM tables but calls663add0 at66cbedc, which writes through u32(08a9dbe8)+1c. Not independent.",
            "6715cbc": "Command submission6639b14 occurs before local callback registration. Submit reads u32(08a9dbe8)+6eec and u32(08a9dbec)+200; ring slots are8 bytes and64 entries. No live peer/ring contract means this cannot be treated as a pure callback installer.",
            "remaining_commands": ["26", "36", "81", "82", "85", "86", "87", "8f conditional", "a2 conditional", "a9", "ab", "b2", "b3", "bd"],
            "callback26": "66cda4a stores three halfwords at08bef62c and selects numeric constants via66f9ac2. No queue/OSD call on this audited callback path.",
            "callback36": "6715e40 stores three halfwords at08a9ed10. No queue/OSD call.",
            "hardware_tail": "6639824->652e666 configures SSP2/GPIO and calls other hardware helpers;6639828->6546f68->6546e20 sends SPI0 commandE5 for0x200. These are not GUI route initializers. Full transitive hardware effects are not claimed audited."
        },
        "independent_function_candidates": [
            {"entry_thumb": "0x06715c75", "return_pc": "0x06715cb4", "arguments": "none consumed",
             "effects": "Clear08a9ed0e and3 halfwords08a9ed10; reset127 host callback-ring entries,28 bytes each, at091a25e0",
             "dependencies": "RAM only; destructive if any callbacks already registered", "gui_scene_producer": False},
            {"entry_thumb": "0x06586def", "return_pc": "0x06586e48", "arguments": "none consumed",
             "effects": "Initialize08a9ee10/08a9ee44/08a9ee54,08bed6cc,08bf2fce,090c5d30+9c4..9ca; record timer1 VALUE",
             "dependencies": "Loaded EEPROM byte063069e0, writable host state, valid initialized timer1; timer read66c19e2->67076d4->6554d72",
             "gui_scene_producer": False, "does_not_set_nested_completion_byte": True},
            {"entry_thumb": "0x0658b891", "return_pc": "0x0658b8d2", "arguments": "none consumed",
             "effects": "Publish091a543c at08a9f36c; reset counters/bytes at091a566c and08c33378", "dependencies": "RAM only", "gui_scene_producer": False},
            {"entry_thumb": "0x0676efdd", "return_pc": "0x0676f084", "arguments": "none consumed",
             "effects": "Fill4042 halfwords at08c9f8c0 with0800; build4042 int32 calibration entries at08ca1854 from original tables/EEPROM",
             "dependencies": "Original06f0dcf0,062e6400,06307c00 and0x228 bytes stack; interpolation branch requires valid ordered input table; helper65b25c0 is a bounded table search",
             "gui_scene_producer": False}
        ],
        "completion_setters_are_not_standalone_repair": {
            "65c593e": "Writes byte08a670ea=1 only after65850f4 returns. It enables65c5942's subsequent services. Calling or imitating this store before its dependent initialization would falsely mark completion.",
            "659c898": "Final outer tail only sets u32(08aa04b8)=1, after6658c2a. Its original order does not license setting it independently to hide a missing peer."
        },
        "second_peer": {
            "path": "676ee30->6658c2a->64ab520",
            "descriptor_sources": ["06a5d918", "06a6d918"], "destinations": ["7b060000", "7b040000"],
            "descriptor_lengths": ["0x10000", "0x10000"], "extra_descriptor_field_offset8": "0x2000; not assumed the first upload byte length",
            "wait_pc": "0x064ab574", "wait_condition": "u32(7b044200)!=0",
            "published_pointer": "u32(08aa94e8)=7b040000+2*u32(7b044200)",
            "host_shared_writes": ["+0=7f081c10", "+4=7f081c1c", "+8=7f081c28", "+c=7f080014", "+10=7f080c00", "+828=u32(08aa94f0)", "+81e halfword=0"],
            "host_response": "7b044204=1 then enable GIC183/185",
            "not_safe_to_skip": "Unknown peer layout and work; neither ready word nor shared pointer may be invented"
        },
        "experiment_boundary": "If a later explicit experiment runs an independent candidate, call its original Thumb entry in valid task context, preserve stack/register ABI, stop on unknown accesses, and report only that function's observed side effects. Do not resume657bb9c, run676ee28 across the second peer, synthesize completion bytes, or claim complete676ede0/fullboot.",
        "limits": ["No live calls or guest writes in this audit", "No new DSP protocol model", "Negative GUI finding applies to the audited independent functions/callbacks, not all descendants of peer-dependent initialization", "Snapshot zero values corroborate missing state but alone do not identify the GUI scene blocker"]
    }
    (HERE / "skipped-arm-init-contract.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"source_verified": True, "selected_ranges": len(RANGES),
                      "snapshot_fields": len(observations), "guest_executed": False,
                      "outputs": ["skipped-arm-init-contract.json", "skipped-arm-init-disassembly.txt"]}, indent=2))


if __name__ == "__main__":
    main()
