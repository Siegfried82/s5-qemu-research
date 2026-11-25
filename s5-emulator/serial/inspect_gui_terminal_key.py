#!/usr/bin/env python3
"""Read saved guest RAM and original code: terminal transition and key routing.

Never starts QEMU, submits a key, changes a guest flag, or calls firmware.
"""
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
                    default=HERE.parent/'menu-smp/results/local-frame-paced-menu-timeout10')
args = parser.parse_args()
program = (ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base = 0x06280000
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
with (args.snapshot/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    word = lambda address: struct.unpack_from('<I', ram, address)[0]
    ui = {'step': hex(word(0x08cb0ee0)), 'transition_stage': word(0x08aa1120),
          'state': word(0x08a928b4), 'mode': word(0x08a928bc),
          'pending_mode': hex(word(0x08aa1124)),
          'mode35_ack_mask': hex(ram[0x08aa1103]),
          'secondary_input_gate_08aa090c': word(0x08aa090c),
          'held_bank0_08aa0ff4': hex(word(0x08aa0ff4)),
          'modal_id_08a92898': hex(struct.unpack_from('<H', ram, 0x08a92898)[0]),
          'notification_20': ram[0x08af70b8+0x20],
          'notification_23': ram[0x08af70b8+0x23]}
    ram.close()
report = {
    'scope': 'Original bytes and saved RAM only; no new guest execution or input',
    'snapshot': str(args.snapshot.resolve()), 'program_sha256': sha, 'ui': ui,
    'terminal_step': {
        'classification': '0x4a0 is the successful terminal marker, not a new waiting condition',
        'original_sequence': [
            '066c0b3a writes0xff to current acknowledgement byte08aa1103',
            '066c0b40 writes0 to transition stage08aa1120',
            '066c0b42 calls066bff46 to commit the requested mode and associated original state',
            '066c0b46 calls066be132 ->066beea6(event0f000015,param0,param0)',
            '066c0b4a..4e writesstep0x4a0;066c0b50 returns1 through066c0960',
        ],
        'limits': 'A terminal mode transition does not prove a rendered menu or complete DSP initialization',
    },
    'raw_input_gate': {
        'caller': '066431de ->066bfd22',
        'reject_if': 'U32[08aa1124]!=0xff OR U32[08aa1120] not in{0,1}',
        'next_gate': '066adcfc ->0682e5d0 rejects U32[08aa090c] not in{0,1}',
        'runtime_limit': 'Saved final values are not proof of their earlier values at key dispatch',
    },
    'key_center_action': {
        'original_index': 7, 'mask': '0x10',
        'press': 'Action1/TRIGGER maps to internal0; sets input+4 bit4 and held bank0 bit4',
        'release': 'Action0/OPEN maps to internal2; sets input+0xc bit4 and clears held bank0 bit4',
        'generic_ui_path': '0663f02e tests input+4 bit4 ->066bddc8(7,0) ->066beea6(0f000002,7,0)',
        'live_handler_table': '06f10554={mask10,actions1,flags1,handler06646e85}; actionmask1 selects trigger field+4',
        'release_limit': 'A complete press/release is a valid input comparison, but the identified CENTER trigger paths do not require release to select their handler',
        'physical_identity': 'Original software key only; physical MENU/SET GPIO identity unverified',
    },
    'modal_4a': {
        'handler': '0682de54',
        'intercepts': ['0f000002', '0f000010', '0f000007 only for its guarded246 case'],
        'does_not_intercept': ['0f03002d', '0f000015'],
        'conclusion': 'Do not attribute the observed generic key event being ignored to this modal handler without its actual event-specific branch',
    },
    'next_comparison': {
        'module': '../menu-smp/key_probe.py',
        'optional_gate': 'arm(...,wait_for_transition_complete=True)',
        'required_same_sample': {'step': '0x4a0', 'transition_stage': 0, 'state': 1, 'mode': 1},
        'behavior': 'Only read these original fields at the original task285 loop and single-step while pending; no state completion is synthesized',
        'rearm': 'After consumed_by_original_dispatcher, the same KeyProbe may arm another key/action; request history is retained',
    },
}
(HERE/'gui-terminal-key-evidence.json').write_text(json.dumps(report, indent=2)+'\n')
cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
cs.detail = True
ranges = [(0x066c0b3a,0x066c0b54),(0x066bff46,0x066bff80),
          (0x066be132,0x066be13e),(0x066bfd22,0x066bfd3a),
          (0x066431de,0x06643278),(0x0682e5d0,0x0682e5e2),
          (0x066435a0,0x06643680),(0x0663f02e,0x0663f03c),
          (0x066bddc8,0x066bddd4),(0x0682de54,0x0682df02)]
lines = [f'SHA256 {sha}; known Thumb basic blocks. No guest executed.']
for start, end in ranges:
    lines.append('\nRANGE '+hex(start))
    for ins in cs.disasm(program[start-base:end-base], start):
        suffix = ''
        if ins.mnemonic.startswith('ldr') and len(ins.operands)>1:
            operand = ins.operands[1]
            if operand.type == capstone.arm.ARM_OP_MEM and operand.mem.base == capstone.arm.ARM_REG_PC:
                literal = ((ins.address+4)&~3)+operand.mem.disp
                suffix = f'; [{literal:08x}]={struct.unpack_from("<I",program,literal-base)[0]:08x}'
        lines.append(f'{ins.address:08x} {ins.bytes.hex():10} {ins.mnemonic:9} {ins.op_str} {suffix}')
(HERE/'gui-terminal-key.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps(ui))
