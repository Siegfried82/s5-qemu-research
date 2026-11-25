#!/usr/bin/env python3
"""Reproduce embedded-coprocessor byte evidence; does not decode its unknown ISA."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = 0x06280000
EXPECTED = "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"
MAGICS = (0x1234, 0x5678, 0x9012, 0x3456, 0x7890)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def occurrences(data, needle, alignment=1):
    return [hex(i) for i in range(0, len(data)-len(needle)+1, alignment)
            if data[i:i+len(needle)] == needle]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", type=Path,
                        default=ROOT.parent / "s5-v29-offline/unpacked-v29/03_program.bin")
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    program = args.program.read_bytes()
    if sha(program) != EXPECTED:
        raise SystemExit("Original program SHA-256 mismatch; refusing address assumptions")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"program": str(args.program.resolve()), "program_sha256": sha(program),
              "program_base": hex(BASE), "isa_identified": False, "blobs": []}
    for name, address, target, size in (
        ("code", 0x06a2d918, 0x7b020000, 0x10000),
        ("data", 0x06a3d918, 0x7b000000, 0x20000),
    ):
        data = program[address-BASE:address-BASE+size]
        assert len(data) == size
        nonzero = [i for i, byte in enumerate(data) if byte]
        record = {"name": name, "source": hex(address), "destination": hex(target),
                  "bytes": size, "sha256": sha(data), "prefix64_hex": data[:64].hex(),
                  "nonzero_bytes": len(nonzero),
                  "first_nonzero": hex(nonzero[0]) if nonzero else None,
                  "last_nonzero": hex(nonzero[-1]) if nonzero else None,
                  "frequent_le16": [{"value": hex(value), "count": count} for value, count
                                     in Counter(struct.unpack("<%dH" % (size//2), data)).most_common(20)],
                  "magic_occurrences": {hex(value): {
                      "le16_aligned": occurrences(data, struct.pack("<H", value), 2),
                      "le32_aligned": occurrences(data, struct.pack("<I", value), 4)}
                      for value in MAGICS}}
        if name == "data":
            record["mailbox_0x4000_first_0x20_hex"] = data[0x4000:0x4020].hex()
        else:
            record["code_0x8500_0x8600_hex"] = data[0x8500:0x8600].hex()
        (args.output / (name + ".bin")).write_bytes(data)
        report["blobs"].append(record)
    (args.output / "blob-evidence.json").write_text(json.dumps(report, indent=2) + "\n")
    try:
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
    except ImportError:
        print("Byte evidence written. Capstone unavailable; ARM disassembly omitted.")
    else:
        decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        lines = []
        for start, end in ((0x065491e4, 0x06549448), (0x064b12f6, 0x064b1400)):
            lines.append(f"\nARM host driver 0x{start:08x}..0x{end:08x} (Thumb; embedded core ISA unknown)")
            for insn in decoder.disasm(program[start-BASE:end-BASE], start):
                lines.append(f"{insn.address:08x}: {insn.bytes.hex():<10} {insn.mnemonic:<8} {insn.op_str}")
        (args.output / "arm-host-disassembly.txt").write_text("\n".join(lines) + "\n")
    print(json.dumps({"verified": True, "output": str(args.output.resolve()),
                      "isa_identified": False}, indent=2))


if __name__ == "__main__":
    main()
