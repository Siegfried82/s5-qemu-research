#!/usr/bin/env python3
"""Read-only original EGL client-image pbuffer alias/lifetime audit."""
import hashlib
import json

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA


def main():
    p = PROGRAM.read_bytes()
    assert hashlib.sha256(p).hexdigest() == EXPECTED_SHA
    post = (PROGRAM.parent / '05_postboot1.bin').read_bytes()
    blobs = [(BASE, p), (0x07267000, post)]

    def rd(a, n):
        for b, d in blobs:
            if b <= a and a + n <= b + len(d):
                return d[a-b:a-b+n]
        raise ValueError(hex(a))

    # Exclude embedded format TBB bytes and literal pools.
    ranges = [
        ('named five-word wrapper', 0x064c2878, 0x064c288e),
        ('client image validation, retention, dimensions', 0x064c245c, 0x064c261c),
        ('surface allocation and registration', 0x064c27b8, 0x064c2878),
        ('image dimensions and object lookup', 0x064d307a, 0x064d3112),
        ('image reference retain/release', 0x064d3112, 0x064d3174),
        ('client image buffer access', 0x064d327e, 0x064d32bc),
        ('surface image pointer retention', 0x064da8ce, 0x064da956),
        ('client image storage binding', 0x064da960, 0x064da9ac),
        ('surface destruction releases image', 0x064dadbe, 0x064dade2),
        ('surface reference retain/release leaves', 0x064da1b0, 0x064da1d0),
        ('image reference release leaf', 0x065e676a, 0x065e67a8),
        ('buffer descriptor aliases existing addresses', 0x066107fc, 0x0661086e),
        ('eglDestroySurface validation and deferred destruction', 0x064c20f8, 0x064c2180),
        ('original allocation and null-attribute call', 0x072905a6, 0x07290638),
        ('caller success/error cleanup', 0x07290644, 0x072906ba),
        ('restore previous current context and destroy surface', 0x072906ba, 0x072906d8),
        ('client image backing allocation', 0x072907fa, 0x07290870),
        ('make-current wrapper with failure restoration', 0x072a323a, 0x072a3296),
        ('DMP image padded-storage dimensions', 0x065e7224, 0x065e7266),
        ('DMP image construction and storage size validation', 0x065e7b42, 0x065e7c3e),
        ('DMP image metadata creation and storage binding', 0x065e7c68, 0x065e7cb4),
        ('image descriptor format and pitch', 0x065e73ae, 0x065e7434),
        ('format decoder prefix before TBB bytes', 0x065e7318, 0x065e7330),
        ('format2 selected case and internal format0', 0x065e734e, 0x065e7356),
        ('32bit pixel size leaf', 0x065e61d4, 0x065e61d8),
    ]
    c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    c.detail = True
    lines = ['Original program SHA256: ' + EXPECTED_SHA,
             'Read-only original instruction evidence. No guest state or runtime changes.']
    for label, a, b in ranges:
        lines.append(f'\n{label}: {a:#x}..{b:#x}')
        for i in c.disasm(rd(a, b-a), a):
            note = ''
            if i.mnemonic.startswith('ldr') and '[pc,' in i.op_str:
                lit = ((i.address+4) & ~3) + i.operands[-1].mem.disp
                note = f' ; [{lit:#x}]={int.from_bytes(rd(lit, 4), "little"):#x}'
            lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:10} {i.op_str}{note}')
    (HERE / 'client-pbuffer-disassembly.txt').write_text('\n'.join(lines) + '\n')

    snapshot = EMU / 'menu-smp/results/local-frame-query/result.json'
    j = json.loads(snapshot.read_text())
    cpu = j['graphics_error']['cpu']
    regs = j['final_registers'][cpu]
    assert regs['r15'] == '0x64c2878' and regs['r14'] == '0x7290605'
    assert regs['r1'] == '0x3096'
    events = [json.loads(x) for x in
              (snapshot.parent / 'graphics/calls.jsonl').read_text().splitlines()]
    image = next(e for e in events if e.get('name') == 'captured_image'
                 and e.get('handle') == regs['r2'])
    assert 0x065e7330 + 2 * rd(0x065e7330 + 2, 1)[0] == 0x065e7352
    assert 0x065e61be + 2 * rd(0x065e61be, 1)[0] == 0x065e61d4
    report = {
        'scope': 'Read-only original ARM ABI, storage and reference-lifetime evidence',
        'program_sha256': EXPECTED_SHA,
        'postboot1_sha256': hashlib.sha256(post).hexdigest(),
        'runtime_snapshot': str(snapshot), 'observed_cpu': cpu,
        'observed_registers': regs, 'observed_image_capture': image,
        'api': {
            'name': 'eglCreatePbufferFromClientBuffer', 'entry': '0x064c2878',
            'arguments': ['r0=EGLDisplay', 'r1=buffer type', 'r2=client image handle',
                          'r3=EGLConfig', '[entrySP]=attribute-list pointer'],
            'return': 'EGLSurface or zero',
            'actual_buffer_type': '0x3096 (EGL_OPENVG_IMAGE)',
            'actual_attributes': {'value': 0,
                                  'evidence': '72905ca r6=0;72905fa STR r6,[sp];7290600 BL64c2878'},
            'dimensions': '64d307a reads signed16 image+38 (width) and +3a (height); these determine the surface, not new width/height attributes.',
        },
        'native_storage_contract': [
            '64c2558→64d30e2 obtains the original VG image object from the current VG context image map.',
            '64c255e→64d3112 increments image[0] reference count while holding original lock.',
            '64c25a4→64d307a gets original image width/height;64c25ba→64d3094 gets image format at+1c.',
            '64c27ec calls64da79c with the same retained image as fourth argument.',
            '64da90e/64da914 store this same image pointer into surface+68.',
            'For client image surface types12/32/52,64da966→64d327e reads image+5c and image+70. If+70 absent, the original image backing helper65e7dca supplies it.',
            '64da974/64da978/64da97c convert that buffer using66107fc and store it atsurface+74. No pixel-copy routine occurs in this binding path.',
            '64dadbe recognizes types12/32/52 and64dadd2 loads surface+68;64dadd4→64d3140→65e676a releases image reference.',
            'eglDestroySurface removes public handle and decrements surface reference before final64dadbe, preserving current-surface deferred lifetime.',
        ],
        'caller_lifecycle': [
            '72905c6→72907fa creates client image; local[r4] stores its image handle, local[r4+4] stores original external backing allocation.',
            '72907fa rounds allocation width to16 and height to8, allocates4bytes/pixel aligned80, optionally clears it, then65da992 creates format2 image using this allocation.',
            '72905d0..72905ee saves current display, draw surface, read surface and context.',
            '7290600 creates client-image surface using null attributes.',
            '7290612→72a323a→64c3478 makes the new surface both draw and read, preserving saved context.',
            '72906ba helper restores saved display/draw/read/context via72a323a, then72906ce destroys the temporary surface. The image handle remains owned by the caller.',
        ],
        'dmp_external_image_pitch': {
            'entry': '0x065da992',
            'original_evidence': '65e7b42 sets fifth helper argument0, calls65e7224(width,height,&storageWidth,&storageHeight,0).',
            'storage_width': 'width==8 ? 8 : (width+15)&~15',
            'storage_height': '(height+7)&~7',
            'format2': '65e7318 TBB entry2 goes7352: premultiplied flag1, internal format0;65e61b6 TBB entry0 goes61d4 and returns32bits/pixel.',
            'pitch': '65e73fe stores storageWidth at image+3c;65e7406 stores storageHeight at+3e;65e7404..740e computes(bytesPerPixel*storageWidth) and stores image+48.',
            'size_validation': '65e7be0..7bf2 requires supplied allocation size exactly storageWidth*storageHeight*bitsPerPixel/8.',
            'observed_format2_examples': [
                {'width': 10, 'height': 56, 'storage_width': 16, 'storage_height': 56,
                 'pitch': 64, 'allocation_bytes': 3584},
                {'width': 64, 'height': 40, 'storage_width': 64, 'storage_height': 40,
                 'pitch': 256, 'allocation_bytes': 10240},
            ],
            'warning': 'Do not infer row pitch as visibleWidth*4 or allocationSize/visibleHeight. Height padding and the special width8 case matter. Other image APIs with explicit stride retain their own contracts.',
        },
        'implementation_boundary': {
            'required': 'A surface and its source image share live pixel storage; a one-time copy is insufficient. Keep separate image/surface handles and retain storage while either referenced owner remains alive.',
            'ordering': 'Current-surface drawing completes before the original context is restored and the temporary surface is destroyed.',
            'no_guest_side_effects': 'This audit does not mutate QEMU, firmware, GUI flags, events or RAM.',
            'limits': 'Actual null-attribute, format2,64x40 path is established. Additional attribute combinations, alternate image formats and non-current shared-context edge cases need their own support/validation, not silent success.',
        },
        'primary_reference': 'https://registry.khronos.org/OpenVG/specs/openvg_1_0_1.pdf',
        'validation_level': 'Exact original static path plus actual API stop/image capture; no native GPU execution by this audit',
    }
    (HERE / 'client-pbuffer-contract.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'source_verified': True, 'actual_null_attributes': True,
                      'shared_native_image_reference': True,
                      'dimensions': [image['width'], image['height']],
                      'guest_executed': False}, indent=2))


if __name__ == '__main__':
    main()
