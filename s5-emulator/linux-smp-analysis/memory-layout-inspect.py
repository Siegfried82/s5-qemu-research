#!/usr/bin/env python3
"""Reconstruct the loader's 64 section-writing loops without running a guest."""
import argparse
import hashlib
import json
import mmap
from pathlib import Path
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM

HERE = Path(__file__).resolve().parent
EMU = HERE.parent
PARTS = EMU.parent / "s5-v29-offline/unpacked-v29"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def u32(data,offset):
    return struct.unpack_from("<I",data,offset)[0]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot",type=Path,default=EMU/"menu-smp/results/local-ui-mode35")
    args = p.parse_args()
    loader = (PARTS/"02_loader3.bin").read_bytes()
    program = (PARTS/"03_program.bin").read_bytes()
    assert sha(loader) == "67d44d7c2bb2091b7a56ca5118345e0ee604e6b2c6c31769cd6de06ae752d838"
    assert sha(program) == "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"
    c = Cs(CS_ARCH_ARM,CS_MODE_ARM)
    c.detail = True
    tables = {}
    blocks = []
    for index in range(64):
        pc = 0x4cc+index*0x30
        instructions = list(c.disasm(loader[pc:pc+0x30],pc))
        assert len(instructions)==12
        expected = ["mov","mov","mov","ldr",None,"lsr","add","add","str","add","subs","bpl"]
        for i,name in zip(instructions,expected):
            if name:
                assert i.mnemonic == name
        assert instructions[0].op_str.startswith("r3, #")
        start = instructions[0].operands[1].imm & 0xffffffff
        assert instructions[1].op_str == "r5, #0xff"
        assert instructions[2].op_str == "r6, #0x100000"
        assert instructions[3].operands[1].type == ARM_OP_MEM
        table = u32(loader,instructions[3].address+8+instructions[3].operands[1].mem.disp)
        attr_instruction = instructions[4]
        if attr_instruction.mnemonic == "ldr":
            attribute = u32(loader,attr_instruction.address+8+attr_instruction.operands[1].mem.disp)
        else:
            assert attr_instruction.mnemonic=="movw" and attr_instruction.operands[1].type==ARM_OP_IMM
            attribute = attr_instruction.operands[1].imm
        assert [i.op_str for i in instructions[5:11]] == [
            "r1, r3, #0x14", "r1, r2, r1, lsl #2", "r7, r3, r4",
            "r7, [r1]", "r3, r3, r6", "r5, r5, #1"]
        assert instructions[11].operands[0].imm == pc+0x14
        dest = tables.setdefault(table,{})
        for section in range(start>>20,(start>>20)+256):
            assert section not in dest
            dest[section] = ((section<<20)+attribute)&0xffffffff
        blocks.append({"pc":hex(pc),"table":hex(table),"va_start":hex(start),
                       "va_end_exclusive":hex(start+0x10000000),"attribute":hex(attribute)})
    assert set(tables) == {0x86270000,0x86274000,0x86278000,0x8627c000}
    encoded = []
    for address,values in tables.items():
        assert set(values)==set(range(4096))
        data = struct.pack("<4096I",*[values[i] for i in range(4096)])
        encoded.append(data)
        (HERE/f"memory-layout-original-pgd-{address:08x}.bin").write_bytes(data)
    assert all(data==encoded[0] for data in encoded)
    for section,desc in tables[0x86270000].items():
        assert desc & 0xfff00000 == section<<20
        assert desc & 3 == 2
    conversion_table = [struct.unpack_from("<4I",program,0x6ff11b0-0x6280000+i*16) for i in range(4)]
    assert conversion_table == [(0x40000000,0,0,0x80000000),
        (0x40000000,0x40000000,0xdead,0xc0000000),
        (0,0xdead,0xdead,0xdead),(0,0xdead,0xdead,0xdead)]
    report = {
        "scope":"Read-only original-byte and RAM-snapshot analysis. No QEMU/boot/graphics changes; no guest execution.",
        "loader_sha256":sha(loader),"program_sha256":sha(program),
        "loader_table_builder":{"entry":"0x4cc","return":"0x10cc","decoded_loops":64,
            "each_loop_sections":256,"total_descriptor_writes":16384,"all_four_tables_equal":True,
            "all_descriptors_identity_sections":True,"table_sha256":sha(encoded[0]),"loops":blocks},
        "loader_cp15":{"ttbr0":"86270000 + physical_core*4000, via original4b0",
            "dacr":"ffffffff at4c0..4c4","enable":"SCTLR OR5 atfc..10c / 2bc..2c8",
            "note":"Exact original cacheability attributes retained as raw descriptors; no claim all inherited SCTLR bits were reconstructed"},
        "conversion_table_address":"0x06ff11b0",
        "conversion_table":[{"size":hex(row[0]),"field1":hex(row[1]),"field2":hex(row[2]),"field3":hex(row[3])} for row in conversion_table],
        "conversion":"66808c6 sets source field1, destinationfield3;6680838..89c validates containing range and returns source offset plus destination base",
        "second_bank":"[40000000,80000000) translates to [c0000000,100000000); field2 is deliberatelyDEAD/unavailable, not a low512MiB alias",
        "snapshot":str(args.snapshot.resolve()),"addresses":[],
        "conclusion":"No VA-to-low512MiB translation in original loader. Explicit2GiB software backing with high80000000..ffffffff and low0..3fffffff aliases is consistent with these original address ranges, but physical camera DRAM capacity is unverified.",
        "limits":["The helper's512MiB RAM backing cannot serviceffa91000 even though its identitysection descriptor exists",
                  "Do not mask with1fffffff: distinct original second-bank addresses would corrupt first-bank RAM",
                  "Do not map all40000000..7fffffff over existingMMIO",
                  "Static capacity/address-conversion declarations do not identify physical DRAM chips or prove all target apertures"]}
    with (args.snapshot/"ram.bin").open("rb") as f:
        ram=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)
        for va in [0x7d159000,0xfd159000,0x7fa91080,0xffa91000,0x1d159000,0x9d159000]:
            old = tables[0x86270000][va>>20]
            current = u32(ram,0x100000+(va>>20)*4)
            report["addresses"].append({"virtual":hex(va),"loader_descriptor":hex(old),
                "loader_physical":hex((old&0xfff00000)|(va&0xfffff)),
                "synthetic_descriptor":hex(current),"synthetic_physical":hex((current&0xfff00000)|(va&0xfffff)),
                "current512MiB_backing_contains":va<0x20000000 or 0x80000000<=va<0xa0000000})
        report["original_runtime_conversion_table_matches"] = ram[0x6ff11b0:0x6ff11f0] == program[0x6ff11b0-0x6280000:0x6ff11f0-0x6280000]
        report["fault_arena"]={"address":"0x09049728","words":[hex(x) for x in struct.unpack_from("<8I",ram,0x9049728)]}
        report["allocation_descriptor"]={"address":"0x09051738","words":[hex(x) for x in struct.unpack_from("<8I",ram,0x9051738)]}
        ram.close()
    for name,data,base,mode,ranges in [
        ("loader",loader,0,CS_MODE_ARM,[(0x4b0,0x10e8)]),
        ("conversion",program,0x6280000,CS_MODE_THUMB,[(0x6680838,0x66808ce),(0x66c1b4c,0x66c1b7e),(0x646bca2,0x646bd14)]),
    ]:
        lines=[]
        for start,end in ranges:
            for i in Cs(CS_ARCH_ARM,mode).disasm(data[start-base:end-base],start):
                lines.append(f"{i.address:08x} {i.bytes.hex():10} {i.mnemonic:8} {i.op_str}")
        (HERE/f"memory-layout-{name}.disasm.txt").write_text("\n".join(lines)+"\n")
    (HERE/"memory-layout-evidence.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"all_four_tables_identity":True,"descriptor_writes":16384,
        "table_sha256":sha(encoded[0]),"second_bank_declared":True,
        "actual_physical_dram_capacity_verified":False},indent=2))


if __name__=="__main__":
    main()
