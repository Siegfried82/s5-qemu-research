#!/usr/bin/env python3
"""Read-only ABI evidence for original native windows and host EGL surface bridging."""
from pathlib import Path
import argparse
import hashlib
import json
import mmap
import struct
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--snapshot', type=Path, default=ROOT / 's5-emulator/menu-smp/results/isolated-display-context')
parser.add_argument('--output', type=Path, default=HERE / 'native-surface-abi.json')
args = parser.parse_args()
program_path = ROOT / 's5-v29-offline/unpacked-v29/03_program.bin'
post_path = ROOT / 's5-v29-offline/unpacked-v29/05_postboot1.bin'
program, post = program_path.read_bytes(), post_path.read_bytes()
events = [json.loads(x) for x in (args.snapshot / 'graphics/calls.jsonl').read_text().splitlines()]
observed = [e for e in events if e.get('name') == 'observed_native_window']
assert observed, 'A real observed native window is required; synthetic handles are not substituted'
with (args.snapshot / 'ram.bin').open('rb') as f:
    ram = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda a: struct.unpack_from('<I', ram, a)[0]
    u16 = lambda a: struct.unpack_from('<H', ram, a)[0]
    s16 = lambda a: struct.unpack_from('<h', ram, a)[0]
    windows = []
    for event in observed:
        a = int(event['handle'], 0)
        assert ram[a:a+4] == b'DWD\0'
        buffers = []
        for field in [0x18, 0x1c]:
            d = u32(a+field)
            if not d:
                continue
            buffers.append({'native_offset': hex(field), 'descriptor': hex(d),
                            'allocation_base': hex(u32(d)), 'cpu_alias': hex(u32(d+4)),
                            'query_address': hex(u32(d+8)), 'bytes': u32(d+0xc),
                            'stride_bytes': u32(d+0x10), 'flags': hex(u32(d+0x14)),
                            'descriptor_hex': ram[d:d+0x28].hex()})
        window = {'handle': hex(a), 'observer': event,
                  'object_hex': ram[a:a+0x38].hex(),
                  'width': u16(a+0xa), 'height': u16(a+0xc), 'stride_bytes': u16(a+0xe),
                  'internal_format': u32(a+0x10), 'bytes_per_buffer': u32(a+0x2c),
                  'aligned_width': u16(a+0x30), 'aligned_height': u16(a+0x32),
                  'buffer_count': s16(a+0x34), 'current_index': s16(a+0x36),
                  'query_e010_next_index': (s16(a+0x36)+1) % s16(a+0x34),
                  'buffers': buffers}
        assert (window['width'], window['height']) == (event['width'], event['height'])
        assert all(x['bytes'] == window['bytes_per_buffer'] for x in buffers)
        assert all(x['stride_bytes'] == window['stride_bytes'] for x in buffers)
        windows.append(window)
    mapper = {'address': '0x08ab4408', 'base_start': hex(u32(0x08ab440c)),
              'alias_start': hex(u32(0x08ab4410)), 'bytes': u32(0x08ab4414),
              'to_alias_function': '0x064d28de',
              'formula': 'alias_start + (base_address - base_start), after range validation'}

result = {
    'scope': 'Original native allocation in an explicitly isolated display experiment; no object or task fields modified by this extractor',
    'snapshot': str(args.snapshot / 'ram.bin'),
    'program_sha256': hashlib.sha256(program).hexdigest(),
    'postboot1_sha256': hashlib.sha256(post).hexdigest(),
    'observed_windows': windows, 'address_mapping': mapper,
    'entry_abis': {
        '0x064deeba': {'arguments': ['r0 display argument', 'r1 width', 'r2 height',
                                    'r3 public pixel-format selector', '[entrySP] optional EGL_NONE-terminated attributes'],
                       'return': 'r0 actual guest DWD window pointer, or0 on failure',
                       'observed_caller': '0x0728fe58 choosesr3=1 when EGL_CONFIG_ID=6; other config IDs choose6',
                       'format_conversion': {'1': 0, '21': 1, '2': 2, '6': 3, '4': 4, '26': 5, '28': 6, '29': 7}},
        '0x064c46c4': {'arguments': ['r0 display (not used)', 'r1 EGLsurface', 'r2 u32*outNative'],
                       'return': 'BOOL r0=1 iff surface nonzero and surface.u32+8==1; then*outNative=surface.u32+0x68',
                       'bridge': 'The host software EGLsurface can map to the already observed real native window; it must not be dereferenced as a guest driver object'},
        '0x064de7da': {'arguments': ['r0 display (not used)', 'r1 nativeDWD', 'r2 attribute', 'r3 u32*out'],
                       'return': 'BOOL1 on successful original query,0 on failure; output initialized0',
                       '0xe011': 'native.u32+0x18 buffer descriptor ->0x06610d5a ->descriptor.u32+8',
                       '0xe012': 'native.u32+0x1c buffer descriptor ->0x06610d5a ->descriptor.u32+8',
                       'preserve_original': 'After mapping the EGLsurface to actualnative, these helpers can use original descriptors and allocator'},
        '0x064de686': {'arguments': ['r0 display (not used)', 'r1 nativeDWD', 'r2 attribute', 'r3 u32*out'],
                       '0xe010': '(s16(native+0x36)+1) % s16(native+0x34)',
                       'return': 'BOOL1 on this successfulquery,0 on failure; writesnext index to*out',
                       'important': 'This observed E010 query is buffer index, NOT pixel format'},
        '0x064d53be': {'arguments': ['r0 nativeDWD', 'r1 optional zero-terminated low-levelattrs'],
                       'behavior': 'For count2 increments currentindex modulo2, then unlocks native mutex+4',
                       'limit': 'Do not call as a standalone inferior-call without satisfying its matching lock/present protocol'},
    },
    'native_fields': {'size': '0x38', '+0': "magic'DWD\\0'", '+4': 'mutexhandle', '+8': 'u16referencecount',
                      '+0xa': 'u16width', '+0xc': 'u16height', '+0xe': 'u16stridebytes', '+0x10': 'u32internalformat',
                      '+0x18': 'buffer0descriptor', '+0x1c': 'buffer1descriptor',
                      '+0x20/+0x24': 'additional plane descriptors for internal formats6/7',
                      '+0x28': 'lockedCPUalias', '+0x2c': 'u32bytesPerBuffer',
                      '+0x30': 'u16alignedWidth', '+0x32': 'u16alignedHeight',
                      '+0x34': 's16bufferCount', '+0x36': 's16currentIndex'},
    'buffer_descriptor_fields': {'size': '0x28', '+0': 'allocationbase', '+4': 'CPUalias', '+8': 'query/baseaddress',
                                 '+0xc': 'bytes', '+0x10': 'stridebytes', '+0x14': 'flags'},
    'application_surface_fields': {'builder': '0x0728fe58', '+0': 'actualnativewindow', '+4': 'EGLsurfacehandle',
                                   '+8': 'u16width', '+0xa': 'u16height', '+0xc': 'u16 initialized0',
                                   '+0xe': 'u16next buffer index fromE010', '+0x10': 'E011bufferbase', '+0x14': 'E012bufferbase'},
    'allocation': {'creator': '0x064d4ad2', 'bits_per_pixel_internal0': 32,
                   'alignment': 'width to16pixels, height to8pixels, each at least32, for observed defaultattributes',
                   'default_buffers': 2,
                   'pixel_channel_order': 'Not established by this structure/layout analysis; do not infer from32bpp alone'},
    'custom_surface_attribute_0x3fdd': {
        'observed_request': '0x0728feba calls eglSurfaceAttrib(attr3fdd,value3fde)',
        'setter': '0x064c2aaa validates3fde/3fdf and config+0x48 bits20/21 respectively, stores surface+0xc8',
        'additional_state': 'Marksdirty0x100. For native windows and3fde, queries0x064dfe8e; validDWD returns1 through0x064d4fa4, stores surface+0xd4 and marksdirty0x800',
        'query_helper_0x064d2ebe': {'out_u8_0': 'surface+0xc8==3fde',
                                   'out_u8_1': 'surface+0xd4==1',
                                   'out_u8_2': 'surface+0xc4==3094 (preserved)',
                                   'out_u32_4': 'surface+0xbc descriptor',
                                   'out_u32_8': 'surface+0xc0'},
        'consumer': '0x064d2f34: modeflag1 and nonzero callerargument choose descriptor->CPUalias then0x06503e3c clippedrowcopy; othermode takes0x06505b24',
        'rowcopy_evidence': '0x06503ef2 calls original memcpy0x066c22b4 per row, with optional reversed source row order',
        'interpretation': 'The observed mode supports a CPU-accessible linearbuffer copy path; exact officialextension name is not established',
        'bridge_contract': 'Record and support the observed mode with actuallinear buffer synchronization; do not silently discard unknownvalues',
    },
    'limitations': ['1024x768 native allocation does not override original viewport/plane geometry',
                    'No panel scanout or pixel-order validation in this report',
                    'EGL/VG software rendering remains hostHLE; original native allocator reallyexecuted'],
}
args.output.write_text(json.dumps(result, indent=2)+'\n')
lines = [f'Original program SHA256={result["program_sha256"]}', f'Original postboot1 SHA256={result["postboot1_sha256"]}',
         'Thumbdisassembly; manually selected code ranges may contain literal/table data at ends.']
for blob, base, ranges in [
    (program,0x06280000,[(0x64deeba,0x178),(0x64c46c4,0x20),(0x64de7da,0xb4),
                        (0x64de686,0x152),(0x64d5148,0x134),(0x64d4fd0,0x178),
                        (0x64d4ad2,0x1e4),(0x64d541e,0x1a),(0x6610754,0xa8),
                        (0x6610d16,0x84),(0x64d28c6,0x5e),(0x64d53be,0x60),
                        (0x64c2aaa,0xa0),(0x64d2ebe,0x44),(0x64d2f34,0x118),
                        (0x64dfe8e,0x68),(0x64d4fa4,0x2c),(0x6503e3c,0xc4)]),
    (post,0x07267000,[(0x728fdf8,0x60),(0x728fe58,0xf0)]),
]:
    md = capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
    for a,n in ranges:
        lines.append('\nRANGE '+hex(a))
        for i in md.disasm(blob[a-base:a-base+n],a):
            lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}')
args.output.with_suffix('.disasm.txt').write_text('\n'.join(lines)+'\n')
print(f'Saved {len(windows)} real nativewindow(s) and originalABI evidence; dimensions/stride/size assertions passed.')
