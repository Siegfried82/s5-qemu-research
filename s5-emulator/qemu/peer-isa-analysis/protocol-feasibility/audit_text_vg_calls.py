#!/usr/bin/env python3
"""Offline look-ahead for original postboot1 text path OpenVG calls.

Only writes analysis artifacts. Does not change bindings, renderer or QEMU.
"""
import ast
from collections import defaultdict
import hashlib
import json
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA

APIS = [
    (0x65D9BC6, 'vgRemovePathCapabilities', ['path', 'capabilities'], 'void', 0x64E166C,
     'Clears supplied bits from validated path object+20 with BIC; does not clear geometry.', 'text'),
    (0x65D9962, 'vgGetParameterfv', ['object', 'paramType', 'count', 'float_values_ptr'], 'void', None,
     'Text72a6dde queries paint color1a01/count4 into stack and reads floats. Wrapper dispatches by object type with float-output flag1.', 'text'),
    (0x65D9C00, 'vgAppendPath', ['dstPath', 'srcPath'], 'void', 0x64E17D0,
     'Checks source capability bit0/destination bit1, copies source segments and converts/appends coordinates to destination.', 'text_helper'),
    (0x65D9D28, 'vgPathTransformedBounds', ['path', 'minX_float_ptr', 'minY_float_ptr', 'width_float_ptr', 'height_float_ptr'], 'void', 0x64E2F9A,
     'Checks capability bit11, transforms four bounding-box corners with context path matrix, computes min/max and stores four floats.', 'text_optional_mask'),
    (0x65DA454, 'vgMask', ['mask', 'operation', 'x', 'y', 'width', 'height'], 'void', 0x65EB1E6,
     'Validates operation1500..1505 and positive width/height. Text calls mask0,operation1501 and rectangle while1130 MASKING is enabled.', 'text_optional_mask'),
    (0x65DA4B8, 'vgRenderToMask', ['path', 'paintModes', 'operation'], 'void', 0x64E3434,
     'Shares path draw core but passes mask-operation and distinct render-to-mask selector1; text uses paintModes2,operation1505.', 'text_optional_mask'),
    (0x65D9D02, 'vgPathBounds', ['path', 'minX_float_ptr', 'minY_float_ptr', 'width_float_ptr', 'height_float_ptr'], 'void', 0x64E2EC2,
     'Checks capability bit10; stores original minX/minY and max-min without applying matrix; empty path yields zero origins and negative widths.', 'other_postboot1'),
    (0x65D9C44, 'vgModifyPathCoords', ['path', 'startIndex', 'numSegments', 'pathData_ptr'], 'void', 0x64E19D0,
     'Checks capability bit2, segment-index/count and data alignment; replaces coordinates for existing segment range.', 'other_postboot1'),
    (0x65D9BE2, 'vgGetPathCapabilities', ['path'], 'r0 bitfield', 0x64E164A,
     'Returns validated path object+20; invalid path reports1000 and returns0.', 'no_direct_postboot1_call'),
    (0x65D9CA4, 'vgPathLength', ['path', 'startSegment', 'numSegments'], 'r0 IEEE754 float bits', 0x64E2AEC,
     'Checks capability bit7 and segment bounds, subtracts cumulative arc-length values; error return is float -1. Wrapper explicitly VMOVs result to r0.', 'no_direct_postboot1_call'),
    (0x65D9C80, 'vgInterpolatePath', ['dstPath', 'startPath', 'endPath', 'amount_IEEE754_word'], 'r0 VGboolean', 0x64E1EEE,
     'Validates three path handles and capabilities destination bit6/source bit5; fourth public word is read as float amount by lower routine.', 'no_direct_postboot1_call'),
]

RANGES = [
    ('actual text tail and next draw routine', 0x72A8BD0, 0x72A8E30),
    ('application append/transform/draw/bounds wrappers', 0x72A3342, 0x72A345E),
    ('text paint-alpha query', 0x72A6DDE, 0x72A6E40),
    ('text optional mask rendering', 0x72A74A0, 0x72A760E),
    ('path capability and append wrappers', 0x65D9BC6, 0x65D9C1C),
    ('modify/transform/interpolate/length/point/bounds wrappers', 0x65D9C44, 0x65D9D4E),
    ('parameter-float-vector wrapper', 0x65D9962, 0x65D9A14),
    ('mask and render-to-mask wrappers', 0x65DA454, 0x65DA536),
    ('capability getter/remover lower functions', 0x64E164A, 0x64E1692),
    ('append lower function', 0x64E17D0, 0x64E18C0),
    ('modify lower validation/body excluding pool', 0x64E19D0, 0x64E1A86),
    ('interpolate handle/capability validation', 0x64E1EEE, 0x64E1F88),
    ('length complete lower function', 0x64E2AEC, 0x64E2BA4),
    ('bounds and transformed bounds before pool', 0x64E2EC2, 0x64E30E6),
    ('mask argument validation', 0x65EB1E6, 0x65EB244),
]


def main():
    program = PROGRAM.read_bytes()
    assert hashlib.sha256(program).hexdigest() == EXPECTED_SHA
    post_path = PROGRAM.parent / '05_postboot1.bin'
    post = post_path.read_bytes()
    blobs = [(BASE, program), (0x7267000, post)]
    def rd(addr, length):
        for base, data in blobs:
            if base <= addr and addr + length <= base + len(data):
                return data[addr-base:addr-base+length]
        raise ValueError(hex(addr))
    c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    c.detail = True
    refs = defaultdict(list)
    # Narrow direct BL candidate scan. Application sites in selected text ranges
    # are independently decoded below; unvisited branches remain static evidence.
    for offset in range(0, len(post)-3, 2):
        h, k = struct.unpack_from('<HH', post, offset)
        if h & 0xf800 != 0xf000 or k & 0xd000 != 0xd000:
            continue
        s, j1, j2 = (h >> 10) & 1, (k >> 13) & 1, (k >> 11) & 1
        disp = (s << 24 | (1 ^ (j1 ^ s)) << 23 | (1 ^ (j2 ^ s)) << 22 |
                (h & 0x3ff) << 12 | (k & 0x7ff) << 1)
        if s:
            disp -= 1 << 25
        pc = 0x7267000 + offset
        target = pc + 4 + disp
        if 0x65d9280 <= target < 0x65daab4:
            refs[target].append(pc)
    assert 0x72A8CF8 in refs[0x65D9BC6]
    assert 0x72A6DE8 in refs[0x65D9962]
    assert refs[0x65D9CA4] == [] and refs[0x65D9C80] == []
    source = EMU / 'display/graphics_hle.py'
    text = source.read_text()
    tree = ast.parse(text)
    core = next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CORE' for t in n.targets))
    rows = []
    for entry, name, args, ret, lower, evidence, priority in APIS:
        rows.append(dict(name=name, entry=hex(entry), entry_thumb=hex(entry|1),
            argument_words=args, argc_words=len(args), return_abi=ret,
            abi_note='r0..r3 hold first four words; remaining words are at entrySP+0,+4,...; scalar floating-point values use integer-register words in these original wrappers',
            lower_entry=hex(lower) if lower else None, evidence=evidence,
            priority=priority, direct_postboot1_call_sites=[hex(x) for x in refs[entry]],
            present_in_core_at_audit=entry in core,
            entry_bytes_16=rd(entry,16).hex()))
    out = ['Original program SHA256: '+EXPECTED_SHA,
           'Original postboot1 SHA256: '+hashlib.sha256(post).hexdigest(),
           'Selected static Thumb evidence only; no guest invocation or renderer changes.']
    for label, a, b in RANGES:
        out.append(f'\n{label}: {a:#x}..{b:#x} (end exclusive)')
        for i in c.disasm(rd(a,b-a), a):
            note = ''
            if i.mnemonic.startswith('ldr') and '[pc,' in i.op_str:
                lit=((i.address+4)&~3)+i.operands[-1].mem.disp
                note=f' ; [{lit:#x}]={int.from_bytes(rd(lit,4),"little"):#x}'
            out.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:10} {i.op_str}{note}')
    (HERE/'text-vg-lookahead-disassembly.txt').write_text('\n'.join(out)+'\n')
    report = dict(scope='Read-only ahead-of-stop original API identification; no new binding, emulation or success injection',
        program_sha256=EXPECTED_SHA, postboot1_sha256=hashlib.sha256(post).hexdigest(),
        binding_source=str(source), binding_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        binding_comparison_note='Live development snapshot only; another agent may add bindings later.',
        api_identification='Names inferred from matching wrapper ABI, checked enums/capabilities and actual application uses; not original exported symbol strings.',
        primary_references=['https://www.khronos.org/files/openvg-quick-reference-card.pdf',
                            'https://registry.khronos.org/OpenVG/specs/openvg_1_0_1.pdf'],
        references_note='Official API signatures/capability names corroborate local instruction evidence. Mask APIs use OpenVG1.1 reference card page2; path bounds/length/interpolation page3.',
        APIs=rows,
        conclusion='Prioritize actual text remove-capabilities and paint-float query; optional outline uses transformed bounds and masking. No direct postboot1 BL to path length or interpolation was found; do not spend runtime implementation effort on those solely because they are neighboring wrappers.',
        unresolved_actual_targets={hex(t):[hex(x)for x in sites] for t,sites in refs.items()
            if t not in core and t not in {x[0] for x in APIS}},
        caution='Static paths are conditional, not a prediction that the next live frame will take them. Unknown APIs remain unsupported until their behavior is implemented and tested.')
    (HERE/'text-vg-lookahead-contract.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'source_verified':True,'identified_APIs':len(rows),
                      'with_direct_postboot1_calls':sum(bool(x['direct_postboot1_call_sites'])for x in rows),
                      'guest_executed':False},indent=2))


if __name__=='__main__':
    main()
