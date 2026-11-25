#!/usr/bin/env python3
"""Compare the normalized peer fingerprint with primary decoder sources.

Runs Teakra's actual disassembler. Separately applies GNU MeP opcode masks;
the latter is a table comparison, not an executed GNU MeP disassembler.
Does not execute DSP code, contact hardware, or alter QEMU/firmware.
"""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess

from analyze import HERE, ROOT, CODE_SHA


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    code = ROOT / "serial/embedded-peer-code.bin"
    assert digest(code) == CODE_SHA
    probe = HERE / "teakra-probe"
    if not probe.exists():
        raise SystemExit("Build the optional primary Teakra probe as documented in README.md")
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    cases = [
        ("vector", 0x0, 0x4, "a700 002a a700 0101"),
        ("absolute_transfer", 0x2a, 0x2e, "d7ee 3300 0000 1000"),
        ("relative_call", 0x1048, 0x104c, "d7ea 3360 0000 3241"),
        ("return_stub", 0x1060, 0x1062, "0000 6200"),
        ("immediate_and_address", 0x42da, 0x42e0, "a4e0 1234 d7e6 db00 0000 21e8"),
    ]
    teak = []
    for name, start, end, normalized in cases:
        result = subprocess.run([str(probe), str(code), hex(start), hex(end), "4"],
                                text=True, capture_output=True, check=True)
        teak.append({"case": name, "word_pc": hex(start),
                     "normalized_words": normalized, "decoder_output": result.stdout})

    control = out / "teakra-positive-control.bin"
    # Known Teakra encodings from its decoder.h, not peer firmware.
    control.write_bytes(struct.pack("<4H", 0, 0x4180, 0x1000, 0x4580))
    result = subprocess.run([str(probe), str(control), "0", "4", "1"],
                            text=True, capture_output=True, check=True)
    assert "nop" in result.stdout and "br" in result.stdout and "ret" in result.stdout
    assert "0x01000" in result.stdout or "0x1000" in result.stdout or "0x00001000" in result.stdout

    source = HERE / "references/mep-opc.c"
    text = source.read_text()
    formats = {m.group(1): (int(m.group(2)), int(m.group(3)), int(m.group(4), 0))
               for m in re.finditer(r"static const CGEN_IFMT (ifmt_\w+) ATTRIBUTE_UNUSED = \{\s*(\d+),\s*(\d+),\s*(0x[0-9a-f]+)", text)}
    entries = []
    for match in re.finditer(r"&\s*(ifmt_\w+),\s*\{\s*(0x[0-9a-f]+)", text):
        comment = text.rfind("/*", 0, match.start())
        name = text[comment + 2:text.find("*/", comment)].strip()
        format_bits, length, mask = formats[match.group(1)]
        entries.append({"syntax": name, "format": match.group(1),
                        "format_bits": format_bits, "length_bits": length,
                        "mask": mask, "opcode": int(match.group(2), 0),
                        "source_line": text.count("\n", 0, match.start()) + 1})
    mep = []
    for value, width in ((0xa700, 16), (0xa4e0, 16), (0x6200, 16),
                         (0xd7ea3360, 32), (0xd7ee3300, 32), (0xd7e6db00, 32)):
        matches = [row.copy() for row in entries
                   if row["length_bits"] == width and value & row["mask"] == row["opcode"]]
        for row in matches:
            row["mask"] = hex(row["mask"])
            row["opcode"] = hex(row["opcode"])
        mep.append({"input_word_or_word_pair": hex(value), "bits_compared": width,
                    "all_matching_table_entries": matches})

    report = {
        "source_code_sha256": digest(code),
        "input_normalization": "LE16 at original halfword index (logical_word_pc XOR 3)",
        "scope": "Offline primary-source comparison only; no DSP execution",
        "teakra": {
            "method": "Compiled and ran upstream disassembler.cpp with decoder.h",
            "primary_source": "https://github.com/wwylele/teakra",
            "decoder_sha256": digest(HERE / "references/teakra-master/src/decoder.h"),
            "disassembler_sha256": digest(HERE / "references/teakra-master/src/disassembler.cpp"),
            "positive_control_words": ["0000", "4180", "1000", "4580"],
            "positive_control_output": result.stdout,
            "normalized_peer_cases": teak,
            "conclusion": "No coherent match to this XpertTeak decoder after normalization; no exclusion claim for other CEVA generations",
        },
        "mep": {
            "method": "Applied exact masks and opcodes from GNU's generated table; machine/ISA feature attributes not evaluated; all syntactic matches retained",
            "not_performed": "No GNU disassembler binary was built or run",
            "primary_source": "https://gnu.googlesource.com/binutils-gdb/+/refs/heads/master/opcodes/mep-opc.c",
            "source_sha256": digest(source), "comparisons": mep,
            "conclusion": "Standard core table matches are beqz/add/movu, not a coherent decoding of the peer's correlated control-flow fingerprint",
        },
        "remaining_identification": {
            "named_isa_confirmed": False,
            "needed_artifact_options": [
                "SoC integration/register manual identifying the processor at code SRAM 0x7b020000, data SRAM 0x7b000000, reset register 0x7c14071c and entry register 0x7c140778",
                "Original peer ELF/COFF, map file or compiler/SDK project carrying an architecture/core ID",
                "Matching core's programmer/ISA reference WITH binary opcode encodings, register map, flags, branch/link/delay-slot behavior and memory addressing rules",
            ],
            "required_opcode_fingerprint": ["a700 u16", "d7ee 3300 u32", "d7ea 3360 s32", "a4e0 u16", "6200", "c0a6 8020", "d7e6 db00 address32"],
            "candidate_manuals_only_if_core_identity_is_supported": [
                "CEVA-TeakLite-III or TeakLite-4 specific revision architecture/programmer reference, not the old XpertTeak/Oak table or marketing brief",
                "UniPhier IPP/DPP processor-specific ISA encoding reference, not a platform overview",
            ],
            "stop_condition": "Do not widen generic searches or infer the processor brand from peripheral addresses; do not execute unknown words as NOPs or write ready/magic",
        },
    }
    (out / "primary-decoder-comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"positive_control_passed": True, "normalized_peer_cases": len(teak),
                      "mep_mask_cases": len(mep), "isa_identified": False,
                      "report": str(out / "primary-decoder-comparison.json")}, indent=2))


if __name__ == "__main__":
    main()
