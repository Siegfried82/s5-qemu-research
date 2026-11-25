#!/usr/bin/env python3
"""Read-only verification of the first original metadata frame under TEST IRQs.

Does not execute QEMU or modify guest state. Requires the lab venv only for
optional Capstone disassembly output. Snapshot consistency is distinguished
from per-instruction execution traces and from sensor/pixel capture.
"""
import argparse
import hashlib
import json
import mmap
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT.parent/'s5-v29-offline/unpacked-v29/03_program.bin'
PROGRAM_SHA = 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
BASE = 0x06280000
SOURCE, WORK, SIZE = 0x08c51d9c, 0x0a603fb8, 0x220


def sha(data):
    return hashlib.sha256(data).hexdigest()


def u32(data, offset=0):
    return struct.unpack_from('<I', data, offset)[0]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', type=Path, default=ROOT/'menu-smp/results/local-frame-pairs2')
    p.add_argument('--output', type=Path, default=ROOT/'display/service-loop-analysis')
    args = p.parse_args()
    program = PROGRAM.read_bytes()
    if sha(program) != PROGRAM_SHA:
        raise ValueError('Requires verified original S5 v2.9 program')
    args.output.mkdir(parents=True, exist_ok=True)
    raw_result = (args.snapshot/'result.json').read_bytes()
    result = json.loads(raw_result)
    with (args.snapshot/'ram.bin').open('rb') as f:
        ram = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        source, work = ram[SOURCE:SOURCE+SIZE], ram[WORK:WORK+SIZE]
        diffs = [{'offset':hex(i), 'source':a, 'work':b}
                 for i,(a,b) in enumerate(zip(source, work)) if a != b]
        ctx = work[0xbc:0xec]
        flags = u32(ctx, 0x2c)
        getter_global = u32(program, 0x065640c0-BASE)
        assert getter_global == 0x08ab1900
        getter_enabled = ram[getter_global+6]
        queue = u32(ram, 0x08e1b94c+(0x77-1)*4)
        queue_info = {k:u32(ram, queue+o) for k,o in
                      [('capacity',4),('array',0xc),('write',0x10),('read',0x14),('count',0x18)]}
        queue_info['object'] = hex(queue)
        envelope = list(struct.unpack_from('<5I',ram,0x08c4ffe0))
        flag_19 = ram[flags+0x19] if 0x08000000 <= flags < len(ram)-0x30 else None
        report = {
            'scope':'Existing snapshot plus original opcode verification; no new QEMU execution',
            'source_program':{'path':str(PROGRAM),'sha256':PROGRAM_SHA},
            'snapshot':str(args.snapshot.resolve()),'result_sha256':sha(raw_result),
            'explicit_irq_test':result.get('frame_test_inputs'),
            'frame_source_trace':result.get('frame_irq_test_probe'),
            'envelope':{'address':'0x08c4ffe0','words':[hex(x)for x in envelope],
                        'expected_original_first_code0':envelope==[0,0x2001,0,SOURCE,0]},
            'queue77_after':queue_info,
            'payload':{'source':hex(SOURCE),'work':hex(WORK),'bytes':SIZE,
                       'source_sha256':sha(source),'work_sha256':sha(work),
                       'source_nonzero_bytes':sum(x!=0 for x in source),
                       'work_nonzero_bytes':sum(x!=0 for x in work),
                       'matching_bytes':SIZE-len(diffs),'differences':diffs,
                       'copy_opcode':'675eff8 loads message+0xc, r2=0x220, r0=0a603fb8, tail66c2598'},
            'context':{'offset':'0xbc','bytes':0x30,'source_work_equal':source[0xbc:0xec]==ctx,
                       'words':[hex(x)for x in struct.unpack('<12I',ctx)],
                       'flags_pointer':hex(flags),'flags_are_ram':0x08000000<=flags<0x20000000,
                       'flag_byte_19':flag_19,
                       'original_default_source':'649f964 uses literal649fa48=08aac02b and stores it at context+2c',
                       'correction':'The observed flags live in RAM. A ROM literal supplies their address; this is not a ROM flags table.'},
            'work_218_explanation':{
                'source':hex(u32(source,0x218)),'work':hex(u32(work,0x218)),
                'writer':'66d19d2 passes work+218 to676c3a8;676c3b2 stores return from676c2fc',
                'getter_chain':'676c2fc ->6811134 ->6563e3c',
                'getter_global':hex(getter_global),'enabled_byte_6':getter_enabled,
                'disabled_branch':'6563e44 CBZ ->6563e76 MOV r0,-1; return via6563e72/74',
                'matches_original_postprocess':getter_enabled==0 and u32(work,0x218)==0xffffffff,
                'interpretation':'Original consumer output field, not evidence of a failed 0x220 copy'},
            'readiness':{'counter_address':'0x08aac26c','counter':struct.unpack_from('<H',ram,0x08aac26c)[0],
                         'path':'66d19c2 ->66d179c;649f6c2 checks context+2c byte19; zero permits66d16fe',
                         'counter_instruction':'66d1784 adds1;66d1788 stores halfword',
                         'evidence_limit':'Counter and metadata state plus original control path; no independent per-instruction trace was captured by this inspector'},
            'graphics':{'draws':result['graphics_draws'],'swaps':result['graphics_swaps'],
                        'scope':result['graphics_frame_scope']},
            'sensor_pixels_proven':False,'original_capture_proven':False,'menu_proven':False,
        }
        ram.close()
    report['copy_consistent_after_original_postprocess'] = (
        [d['offset'] for d in diffs]==['0x218','0x219','0x21a','0x21b']
        and report['context']['source_work_equal']
        and report['work_218_explanation']['matches_original_postprocess'])
    for name,data in [('first-frame-source.bin',source),('first-frame-work.bin',work)]:
        (args.output/name).write_bytes(data)
    try:
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
    except ImportError:
        report['disassembly_written'] = False
    else:
        lines=[]; c=Cs(CS_ARCH_ARM,CS_MODE_THUMB)
        for address,size in [(0x0675eff8,12),(0x066d19c2,0x1e),(0x0676c3a8,0x10),
                             (0x0676c398,0x10),(0x06811134,4),(0x06563e3c,0x40),
                             (0x0649f964,0x12),(0x0649f6c2,0x12),(0x066d1774,0x26)]:
            lines.append('\nREGION '+hex(address))
            for i in c.disasm(program[address-BASE:address-BASE+size],address):
                lines.append(f'{i.address:08x} {i.bytes.hex():12} {i.mnemonic:9} {i.op_str}')
        (args.output/'first-frame-original.disasm.txt').write_text('\n'.join(lines)+'\n')
        report['disassembly_written']=True
    (args.output/'first-frame-verification.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'copy_consistent':report['copy_consistent_after_original_postprocess'],
                      'matching_bytes':SIZE-len(diffs),'poll_count':report['readiness']['counter'],
                      'queue77':queue_info,'graphics':report['graphics']},indent=2))


if __name__=='__main__':
    main()
