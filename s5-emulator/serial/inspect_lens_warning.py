#!/usr/bin/env python3
"""Audit original modal4a and its sampled serial-input source, without execution."""
import hashlib
import json
import mmap
import struct
from pathlib import Path

import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
program = (ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base = 0x06280000
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
run = HERE.parent/'menu-smp/results/local-requested-lcd-refresh'
result = json.loads((run/'result.json').read_text())
state = result['original_ui_state_at_stop']
assert state['source_scene_08cb10d0'] == state['render_scene_0b392e98'] == '0x70de'
assert state['modal_id_08a92898'] == '0x4a'
assert state['source_layout_08cb10c8_hex'] == state['render_layout_0b392e90_hex']
registers = result['rtc']['registers']
packed = sum(registers[f'0x{address:04x}'] << (index*8)
             for index,address in enumerate((0x448,0x44a,0x44c,0x44e)))
assert packed == 0x024a7710
sample = 1 ^ ((packed >> 24) & 1)
assert sample == 1
saved_run = HERE.parent/'menu-smp/results/local-terminal-menu-click-tx'
with (saved_run/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    word = lambda address: struct.unpack_from('<I', ram, address)[0]
    saved = {'packed_status': hex(word(0x08aa0974)),
             'raw_plus_16': ram[0x08e20f74+0x16],
             'current_logical_plus_30': hex(word(0x08ca9624+0x30)),
             'processed_logical_plus_30': hex(word(0x08ca967c+0x30)),
             'modal_id': hex(struct.unpack_from('<H', ram, 0x08a92898)[0])}
    assert word(0x08aa0974) == packed
    assert saved['raw_plus_16'] == sample
    assert word(0x08ca9624+0x30) & (1 << 23)
    assert word(0x08ca9624+0x30) & (1 << 30)
    assert saved['modal_id'] == '0x4a'
    ram.close()
report = {
    'scope': 'Read-only original instructions and prior results; no guest mutation, new key, GPIO, serial input, or acknowledgment.',
    'program_sha256': sha,
    'actual_render': {
        'run': str(run.resolve()),
        'frame': str((run/'graphics/frame-0003.png').resolve()),
        'observed_text': 'The lens is not attached properly. Do not push lens release button while lens is attached.',
        'state': state,
        'conclusion': 'Source and rendered scene70de both carry modal4a. The warning overlays the successfully selected time-zone scene.',
    },
    'actual_serial_registers': {f'0x{a:04x}': registers[f'0x{a:04x}'] for a in (0x448,0x44a,0x44c,0x44e)},
    'historical_saved_ram': {'run': str(saved_run.resolve()), **saved},
    'original_chain': [
        '06549df8 calls06752fb0 to read registers0448,044a,044c,044e into four consecutive bytes, then stores the packed word at08aa0974.',
        '06544d12..4d20 loads halfword[08aa0974+2], extracts bit8, XORs1, and stores raw_input+16. This is packed bit24, equivalently register044e bit0.',
        '0654409a calls06543ec0 to convert raw_input+16 to boolean;065440a2 places it in logical_input+30 bit23.',
        '06544120..415a compares that bit with its previous value08aa8200; transitions reset timestamp08aa81c0. A stable1 with elapsed value greater than1000 from0648523a sets logical_input+30 bit30.',
        'If source bit23 changes to0, this same original path clears bit30. No host-side flag clear is needed.',
        '0664ce4e compares bit30 with the last processed copy; when input processing is allowed by0663d45c, it updates the processed copy.',
        'For bit30=1,0664ce84 calls06693306(1), whose06693324 calls068147b8(4a) to add the original modal.',
        'For bit30=0,0664ce92 calls06693306(0), which removes an existing4a via0681488e; both paths request native redraw.',
    ],
    'minimal_input_contract_candidate': {
        'location': 'Serial peer register0x044e, bit0 only',
        'current_register': registers['0x044e'],
        'released_read_value_if_other_bits_unchanged': registers['0x044e'] | 1,
        'polarity_at_original_sampler': 'bit0=0 -> raw1 -> sustained warning condition; bit0=1 -> raw0 -> warning condition cleared by original code',
        'scope': 'Explicit external input environment, not a successful lens protocol/attachment response. Preserve all other register bits and normal native initialization.',
        'not_yet_proved': 'The physical pin/chip name and read-versus-write semantics of register044e still require peer-model audit. No generic GPIO-high profile follows from this evidence.',
    },
    'limits': ['1000 is the original timestamp threshold; this report does not assume its wall-clock unit.',
               'A released warning input does not establish a connected working lens or a functioning sensor/DSP.',
               'The candidate input change has not been executed by this audit.'],
}
(HERE/'lens-warning-evidence.json').write_text(json.dumps(report, indent=2)+'\n')
cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
cs.detail = True
ranges = [(0x06549df8,0x06549e46),(0x06544d10,0x06544d26),
          (0x0654409a,0x065440a8),(0x06543ec0,0x06543ecc),
          (0x06544118,0x0654415c),(0x0664ce4e,0x0664ce9e),
          (0x06693306,0x0669332a)]
lines = [f'Original SHA256={sha}. Known Thumb blocks; no guest executed.']
for start,end in ranges:
    lines.append('\nRANGE '+hex(start))
    for ins in cs.disasm(program[start-base:end-base],start):
        suffix = ''
        if ins.mnemonic.startswith('ldr') and len(ins.operands)>1:
            operand = ins.operands[1]
            if operand.type == capstone.arm.ARM_OP_MEM and operand.mem.base == capstone.arm.ARM_REG_PC:
                literal = ((ins.address+4)&~3)+operand.mem.disp
                suffix = f'; [{literal:08x}]={struct.unpack_from("<I",program,literal-base)[0]:08x}'
        lines.append(f'{ins.address:08x} {ins.bytes.hex():10} {ins.mnemonic:9} {ins.op_str} {suffix}')
(HERE/'lens-warning.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Original serial044e bit0 -> sustained condition -> modal4a verified; no input changed.')
