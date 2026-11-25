#!/usr/bin/env python3
"""Read-only identification of the original vgTransformPath wrapper."""
import hashlib
import json
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA


def main():
    p = PROGRAM.read_bytes()
    sha = hashlib.sha256(p).hexdigest()
    assert sha == EXPECTED_SHA
    c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    c.detail = True
    ranges = [("two-argument original wrapper", 0x65D9C64, 0x65D9C80),
              ("context lookup and mark", 0x65DAAB4, 0x65DAAD4),
              ("path validation, allocation and transform setup", 0x64E4698, 0x64E480C)]
    lines = [f"Source SHA256: {sha}",
             "Selected original Thumb instructions; no execution or patch."]
    for label, a, b in ranges:
        lines.append(f"\n{label}: {a:#x}..{b:#x}")
        for i in c.disasm(p[a-BASE:b-BASE], a):
            note = ""
            if i.mnemonic.startswith("ldr") and "[pc," in i.op_str:
                lit = ((i.address + 4) & ~3) + i.operands[-1].mem.disp
                note = f" ; [{lit:#x}]={struct.unpack_from('<I', p, lit-BASE)[0]:#x}"
            lines.append(f"{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:10} {i.op_str}{note}")
    (HERE / 'transform-path-disassembly.txt').write_text('\n'.join(lines) + '\n')
    snapshot = EMU / 'menu-smp/results/local-software-status/result.json'
    j = json.loads(snapshot.read_text())
    regs = j['final_registers'][2]
    assert regs['r15'] == '0x65daab4' and regs['r14'] == '0x65d9c6f'
    contract = {
        'source_sha256': sha, 'snapshot': str(snapshot), 'guest_mutated': False,
        'identification': 'vgTransformPath',
        'classification': 'Inference from exact wrapper ABI plus capability bits, path append and matrix operations, cross-checked against the primary OpenVG specification',
        'entry_thumb': '0x065d9c65', 'abi': {'r0': 'VGPath dstPath', 'r1': 'VGPath srcPath', 'return': 'void'},
        'context_call': '0x065d9c6a -> 0x065daab4; return PC0x065d9c6e',
        'saved_arguments_at_stop': {'dst_r5': regs['r5'], 'src_r4': regs['r4'], 'sp': regs['r13']},
        'downstream': '0x064e4698(context, dstPath, srcPath)',
        'evidence': [
            'Wrapper saves r0 into r5 and r1 into r4, obtains context, moves r5/r4 into r1/r2 and tailcalls64e4698.',
            'Two calls64e137e validate destination and source path handles; invalid handles cause error1000.',
            '64e46ce checks source object+20 bit3, then64e46d6 checks destination+20 bit4; failure causes1003.',
            'Source segment pointer/count are+44/+48; destination storage is expanded by the source count at64e474c.',
            '64e47c4..cc selects context state+3a4 matrix via64e11f2 before iterating source path segments.',
            'The call is path transformation, not a glyph draw API; incidental r2/r3 at the context stop are not additional public arguments.'
        ],
        'primary_spec': {
            'url': 'https://registry.khronos.org/OpenVG/specs/openvg_1_0_1.pdf',
            'section': '8.6.8; PDF page77, printed page68; capability enum PDF page69',
            'paraphrase': 'Transform appends a source copy using the path matrix. Source capability8 and destination capability16 are required. Source and destination may coincide. The function has two path-handle arguments and no return value.'
        },
        'scope_limit': 'No renderer implementation or full original function execution in this audit. Graphics backend owner separately validates rendering semantics.'
    }
    (HERE / 'transform-path-contract.json').write_text(json.dumps(contract, indent=2) + '\n')
    print(json.dumps({'source_verified': True, 'observed_dst': regs['r5'], 'observed_src': regs['r4'],
                      'identified': 'vgTransformPath', 'guest_executed': False}, indent=2))


if __name__ == '__main__':
    main()
