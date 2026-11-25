#!/usr/bin/env python3
"""Read-only reconstruction of the stock S5 RTOS -> CPU3 Linux handoff.

Does not launch QEMU, modify firmware, or write any guest state.
Use the lab venv for optional Capstone disassembly output.
"""
import argparse
import hashlib
import json
import mmap
from pathlib import Path
import struct
import sys
import zlib

HERE = Path(__file__).resolve().parent
EMU = HERE.parent
PARTS = EMU.parent / "s5-v29-offline/unpacked-v29"
sys.path.insert(0, str(EMU))
from s5 import fdt_parse

EXPECTED = {
    "00_loader1.bin": "e009d688fb152a70d90fb4df88705b29f441e3dc50c57e4ea1fddf1a49c19d32",
    "02_loader3.bin": "67d44d7c2bb2091b7a56ca5118345e0ee604e6b2c6c31769cd6de06ae752d838",
    "03_program.bin": "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe",
    "27_zboot.bin": "014dad699b15c269e9905ccfbffe1f73963bbef1301298ba230bb9cf8b34fd9f",
    "28_dtb.bin": "f1a4d16edaadbda1940fa95994acd5d69a3e90f5c25daaa68c7151535df8e188",
    "29_zimage.bin": "72b6633d7f20009c5067af6faa5549d52a581c4a55915695e616d666220f7ef0",
    "30_rootfs1.bin": "4a41552e007d5048e32c542899324935c762ae8583b818712cb78b028ba49bf0",
    "31_rootfs2.bin": "4121e7d7503c68d108453ece3016553f5d1ec90cf7bcc79aaf5e3b4234147215",
}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def word(b, a):
    return struct.unpack_from("<I", b, a)[0]


def readable_fdt(n):
    props = {}
    for key, value in n["props"].items():
        if value and all(x == 0 or 32 <= x < 127 for x in value):
            props[key] = value.rstrip(b"\0").decode("ascii").split("\0")
        elif len(value) % 4 == 0:
            props[key] = [hex(x) for x in struct.unpack(">%dI" % (len(value)//4), value)]
        else:
            props[key] = {"hex": value.hex()}
    return {"name": n["name"], "properties": props,
            "children": [readable_fdt(c) for c in n["children"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=EMU / "menu-smp/results/isolated-display-gradient")
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    blobs = {}
    for name, expected in EXPECTED.items():
        blobs[name] = (PARTS / name).read_bytes()
        if sha(blobs[name]) != expected:
            raise ValueError("Version-specific analysis requires verified original " + name)
    p = blobs["03_program.bin"]
    z = blobs["29_zimage.bin"]
    gz = z.index(b"\x1f\x8b\x08")
    decoder = zlib.decompressobj(31)
    kernel = decoder.decompress(z[gz:]) + decoder.flush()
    assert decoder.eof and sha(kernel) == "84f99f8b9d4200a30fe2968e8a3fa29aea6d00aa90c153929e399208d1d3048a"
    zreladdr = word(z, 0x900)
    assert zreladdr == 0x80208000 and word(p, 0x644cf98-0x6280000) == 0x80128008
    assert word(p, 0x6ff1240-0x6280000+2*4) == 2
    dtb = blobs["28_dtb.bin"]
    reserve_off = struct.unpack_from(">I", dtb, 16)[0]
    reservations = []
    while True:
        a, size = struct.unpack_from(">QQ", dtb, reserve_off)
        if a == size == 0:
            break
        reservations.append({"address": hex(a), "bytes": size})
        reserve_off += 16
    report = {
        "scope": "Static original-byte analysis plus read-only existing snapshot; no CPU3 execution by this inspector",
        "sources": [{"path": str(PARTS/name), "sha256": expected,
                     "bytes": len(blobs[name])} for name, expected in EXPECTED.items()],
        "original_dtb": readable_fdt(fdt_parse(dtb)),
        "dtb_reservations": reservations,
        "rtos_release": {
            "load_sequence": "0644ce34 -> 0644cd28; after successful reads 0644ce54 -> 0644cf44",
            "entry": "0x80120000", "mailbox": "0x80128000",
            "store_instruction": "0x0644cf48", "sgi_wrapper": "0x0658baa8",
            "logical_irq": 2, "absolute_sgi": 2, "destination_physical_cpu": 3,
            "target_mask": "0x8", "gicd_sgir_value": "0x00080002",
            "gicd_write": "0643616c: [70001000 + (sender MPIDR & 3)*8000 + f00]",
            "not_a_reset_register": True,
        },
        "original_loader": {
            "loader1_nonzero_core_gicc_setup": "0000016c..0000019c",
            "cpu3_gicc": "0x7001a000", "control": 1, "priority_mask": "0xf0",
            "loader3_dispatch": "0x00000330", "cpu3_mailbox_init": "0x00000370",
            "cpu3_wait": "0x0000037c", "wfi": "0x00000380",
            "mailbox_empty": "0xffffffff", "branch_to_published_entry": "0x000003b4",
            "pending_clear": "STRB ff to 70001f10 + CPU*8000 (CPENDSGIR0 byte0 only; do not reinterpret as SGI2 acknowledgement)",
            "stack_used_on_cpu3_wait_path": False,
            "warning": "Entering 330/370 after the existing release would overwrite the published mailbox; establish wait state before RTOS publishes",
        },
        "zboot": {
            "mpidr_test": "(MPIDR & 0xf03) == 3",
            "cpu3_write": {"address": "0x80200000", "value": 3},
            "kernel_entry": "0x82300000", "r0": 0, "r1": 3, "r2": "0x822ff000",
            "mmu_and_data_cache": "disabled by original zboot before kernel jump",
            "other_core_path": "waits mailbox80200000==3, then WFI; later branches when mailbox changes from3",
            "original_dtb_cpu": "physical MPIDR3; do not apply single-Linux demo cpu@0/cpu-offset edits",
        },
        "memory": {
            "coherent_alias": "physical80000000+n shares backing with low n on current s5-rtos experiment",
            "linux_ram": ["0x80200000", "0x83100000"],
            "low_alias_linux_ram": ["0x00200000", "0x03100000"],
            "zimage_decompression_destination": hex(zreladdr),
            "decompressed_kernel_end_exclusive": hex(zreladdr+len(kernel)),
            "kernel_bytes": len(kernel), "kernel_sha256": sha(kernel),
            "linux_initial_pgd": ["0x80204000", "0x80208000"],
            "decompressor_stack_initial": hex(0x82300000+word(z, 0x25c)),
            "decompressor_heap_end": hex(0x82300000+word(z, 0x25c)+0x10000),
            "helper_pgd": ["0x00100000", "0x00104000"],
            "helper_old_cpu3_park": "0x00010800",
            "direct_known_linux_overlap_with_helpers": False,
            "rtos_program_start": "0x06280000",
            "limits": "Bounds describe verified initial image/DTB allocation region, not proof every future driver DMA/IPC access is safe",
        },
        "linux_gic": {
            "original_reg_base": ["0x70001000", "0x70002000"], "cpu_offset": "0x8000",
            "physical_cpu3_aliases": ["0x70019000", "0x7001a000"],
            "compiled_driver": "c045ef8c gic_init_bases; c045f050..78 uses physical cpu map times offset",
            "distribution_init": "c045f20c..25c: only IRQs whose existing ITARGETSR includes this CPU mask get prioritya0/ICENABLER writes; no unconditional global re-routing in this path",
            "banked_init": "c018c43c clears PPIs, enables SGIs, sets banked priorities; c018c298 enables CPU3 interface",
            "remaining_shared_risk": "Check real guest trace: writable shared distributor, IPC60000, unmodeled SDIO/clock; no claim Linux boots in SMP yet",
        },
    }
    results = json.loads((args.snapshot/"result.json").read_text())
    with (args.snapshot/"ram.bin").open("rb") as f:
        ram = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        loaded = []
        for name, a in [("27_zboot.bin",0x120000),("28_dtb.bin",0x22ff000),
                        ("29_zimage.bin",0x2300000),("30_rootfs1.bin",0x3100000),
                        ("31_rootfs2.bin",0x3400000)]:
            original = blobs[name]
            live = ram[a:a+len(original)]
            loaded.append({"component": name, "physical": hex(a|0x80000000),
                           "bytes": len(original), "exact": original == live,
                           "guest_sha256": sha(live),
                           "equal_except_last4": original[:-4] == live[:-4],
                           "original_last4": original[-4:].hex(), "guest_last4": live[-4:].hex()})
        report["snapshot"] = {
            "directory": str(args.snapshot.resolve()),
            "result_sha256": sha((args.snapshot/"result.json").read_bytes()),
            "components": loaded,
            "rtos_release_mailbox": hex(word(ram,0x128000)),
            "linux_secondary_mailbox": hex(word(ram,0x200000)),
            "rtos_linux_state_08a79f64": ram[0x8a79f64:0x8a79f6c].hex(),
            "cpu3": results["final_registers"][3],
            "gicc3": results["gic"]["cpus"][3],
            "sgi2": next(x for x in results["gic"]["enabled_or_asserted_irqs"] if x["id"] == 2),
            "rootfs_last_word_explanation": "Original0644cd7c/0644cdaa stores version*063b0000 at833ffffc/83dffffc after successful load; not an emulator patch",
        }
        ram.close()
    (args.output/"evidence.json").write_text(json.dumps(report, indent=2)+"\n")
    try:
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
    except ImportError:
        print("Evidence written; use lab/.venv/bin/python for disassembly")
    else:
        sections = [
            ("loader1-gicc",blobs["00_loader1.bin"],0,CS_MODE_ARM,[(0x16c,0x1a0)]),
            ("loader3-cpu3",blobs["02_loader3.bin"],0,CS_MODE_ARM,[(0x330,0x3b8)]),
            ("rtos-linux-handoff",p,0x6280000,CS_MODE_THUMB,
             [(0x644cd28,0x644cdb0),(0x644ce28,0x644ce72),(0x644cf44,0x644cf52),
              (0x658baa8,0x658bad6),(0x643614c,0x6436174)]),
            ("zboot",blobs["27_zboot.bin"],0x80120000,CS_MODE_ARM,[(0x80120000,0x80120150)]),
            ("zimage",z,0x82300000,CS_MODE_ARM,[(0x823000a0,0x82300238),(0x8230031c,0x82300370)]),
            ("linux",kernel,0xc0008000,CS_MODE_ARM,[(0xc000807c,0xc0008124),
              (0xc018c250,0xc018c344),(0xc018c43c,0xc018c47c),(0xc045ef8c,0xc045f26c),
              (0xc04531a4,0xc0453230),(0xc00b71f8,0xc00b7240),
              (0xc003746c,0xc00375c0),(0xc0011424,0xc0011460),
              (0xc0346738,0xc0346744)]),
        ]
        for name,b,base,mode,ranges in sections:
            cs=Cs(CS_ARCH_ARM,mode)
            lines=[]
            for a,end in ranges:
                lines.append(f"\n{a:08x}..{end:08x}")
                for i in cs.disasm(b[a-base:end-base],a):
                    lines.append(f"{i.address:08x} {i.bytes.hex():10} {i.mnemonic:8} {i.op_str}")
            (args.output/(name+".disasm.txt")).write_text("\n".join(lines)+"\n")
    print(json.dumps({"evidence":str(args.output/"evidence.json"),
                      "snapshot_mailbox":report["snapshot"]["rtos_release_mailbox"],
                      "known_helper_overlap":False},indent=2))


if __name__ == "__main__":
    main()
