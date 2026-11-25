#!/usr/bin/env python3
"""Audit original GPIO-to-key mapping in code and a saved snapshot, read-only."""
import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path

import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--snapshot', type=Path,
                    default=HERE.parent/'menu-smp/results/local-terminal-menu-click-tx')
args = parser.parse_args()
program = (ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base = 0x06280000
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
with (args.snapshot/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    word = lambda a: struct.unpack_from('<I', ram, a)[0]
    table_bytes = ram[0x08aa89e4:0x08aa89e4+23*8]
    table = [struct.unpack_from('<II', table_bytes, i*8) for i in range(23)]
    assert table[15] == (0x8000, 0x10)
    assert table[13] == (0x2000, 0x400)
    source_offset = program.find(table_bytes)
    assert source_offset == 0xe4939c
    raw = word(0x08e20f74)
    mapped = 0
    for source, destination in table:
        if raw & source:
            mapped |= destination
    if mapped & 0x200:
        mapped |= 0x100
    selected_original_adjustment = ram[0x062b5c26:0x062b5c28] == bytes(2) and mapped & 0x11 == 0x11
    if selected_original_adjustment:
        mapped &= ~0x20  # Original6544226..422e, not a proposed guest change.
    report = {
        'scope': 'Read-only static and saved-RAM evidence; no GPIO override, sampling, or key submitted',
        'program_sha256': sha, 'snapshot': str(args.snapshot.resolve()),
        'original_pipeline': [
            '06544f6a ->06544974 samples GPIO/ADC to08e20f74',
            '06544f9c ->06543fc8 converts the sampled structure to0x58-byte logical input state',
            '06545004 ->06543cfe allocates/copies0x58 bytes and sends originalcode11f001 toqueue115',
            '064759ba..59be ->06642d22 consumes the original raw-input message',
            '06642fa2..fa8 copies the original current input state to08ca9624',
        ],
        'exact_center_pin': {
            'register': '0x7f082814', 'access_bits': 16, 'data_bit': 4,
            'sample_code': '06545536..5544: bit4 clear produces rawmask0x8000; set produces0',
            'mapping_table_ram': '0x08aa89e4', 'mapping_index': 15,
            'mapping': {'raw': '0x8000', 'logical_bank0': '0x10'},
            'named_key': 'Original06f1003c table index7 KEY_CENTER hasmask0x10',
            'explicit_inactive_environment': 'Returning bit4=1 at this register is consistent with releasing this sampled software key; other bits are not established',
            'limits': 'Physical panel label and SoC pad name are not independently verified',
        },
        'second_sampled_pin': {
            'register': '0x7f080420', 'access_bits': 16, 'data_bit': 4,
            'sample_code': '06545544..5552: bit4 clear addsrawmask0x2000',
            'mapping_index': 13, 'logical_bank0': '0x400',
            'physical_identity': 'Not established here; do not assign a button name by guess',
        },
        'mapping_table_source': {'program_offset': hex(source_offset),
                                 'program_virtual_address': hex(base+source_offset),
                                 'original_bytes_match_runtime': True,
                                 'entries': [{'raw': hex(a), 'logical': hex(b)} for a,b in table]},
        'saved_state': {'raw_word': hex(raw), 'converted_bank0': hex(mapped),
                        'original_adjustment_cleared_bit20': selected_original_adjustment,
                        'actual_current_bank0': hex(word(0x08ca9624)),
                        'actual_input_hex': ram[0x08ca9624:0x08ca9624+0x58].hex(),
                        'host_software_held_word': hex(word(0x08aa0ff4))},
        'software_press_scope': {
            'entry': '066435a0(mask10,internal0)',
            'initial_copy': 'Copies previous0x58 bytes from08ca9624',
            'bank0_replacement': ['0x10', '0x10', '0', '0'],
            'proof': '066435ce..35d4 writecurrent,trigger,repeat,release fields explicitly; originalbank0 multi-key value is not retained in this local call',
            'still_copied': 'Other banks and switch fields remain copied from original input state',
            'conclusion': 'GPIO0 explains a held CENTER in background input, but is not by itself proof that this blocks the explicit software CENTER press',
        },
    }
    assert mapped == word(0x08ca9624)
    ram.close()
(HERE/'key-input-source-evidence.json').write_text(json.dumps(report, indent=2)+'\n')
cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
cs.detail = True
# Known instruction ranges, split around literal pools and jump-table bytes.
ranges = [(0x06545536,0x06545554),(0x06543e84,0x06543ec0),
          (0x06544204,0x06544236),(0x06544f6a,0x06545008),
          (0x06543cfe,0x06543d34),(0x06543d84,0x06543d96),
          (0x064759ba,0x064759c4),(0x06642fa2,0x06642fac),
          (0x066435a0,0x06643610),(0x065441cc,0x065441ee),
          (0x06544a44,0x06544ab8),(0x06544bd4,0x06544c12)]
lines = [f'Original program SHA256={sha}. Known Thumb blocks; saved RAM only.']
for start,end in ranges:
    lines.append('\nRANGE '+hex(start))
    for ins in cs.disasm(program[start-base:end-base], start):
        suffix = ''
        if ins.mnemonic.startswith('ldr') and len(ins.operands)>1:
            op = ins.operands[1]
            if op.type == capstone.arm.ARM_OP_MEM and op.mem.base == capstone.arm.ARM_REG_PC:
                literal = ((ins.address+4)&~3)+op.mem.disp
                suffix=f'; [{literal:08x}]={struct.unpack_from("<I",program,literal-base)[0]:08x}'
        lines.append(f'{ins.address:08x} {ins.bytes.hex():10} {ins.mnemonic:9} {ins.op_str} {suffix}')
(HERE/'key-input-producer.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps(report['saved_state']))
