#!/usr/bin/env python3
"""Extract original software-key script tables and static dispatch evidence.

No command is submitted to QEMU and no GPIO/event is injected.
"""
from pathlib import Path
import hashlib, json, struct
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / 's5-v29-offline/unpacked-v29/03_program.bin'
BASE = 0x06280000
program = SOURCE.read_bytes()
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'

def name_at(address):
    offset = address - BASE
    return program[offset:program.index(b'\0', offset)].decode('ascii')

keys = []
for index in range(125):
    at = 0x06f1003c + 8 * index
    value, pointer = struct.unpack_from('<II', program, at - BASE)
    case = 0x066436a0 + 2 * program[0x066436a0 - BASE + index]
    keys.append({'index': index, 'entry': hex(at), 'value': hex(value),
                 'name_pointer': hex(pointer), 'name': name_at(pointer),
                 'dispatcher_case': hex(case)})

actions = []
for index in range(4):
    at = 0x06f10424 + index * 8
    value, pointer = struct.unpack_from('<II', program, at - BASE)
    actions.append({'argument': index, 'entry': hex(at),
                    'internal_action': value, 'name': name_at(pointer)})

assert keys[7]['name'] == 'KEY_CENTER' and keys[7]['value'] == '0x10'
assert keys[15]['name'] == 'KEY_Q_MENU' and keys[15]['value'] == '0x2000'
assert keys[58]['name'] == 'KEY_SET' and keys[58]['value'] == '0x200000'
assert [a['name'] for a in actions] == ['OPEN', 'TRIGGER', 'REPEAT', 'ONE_SHOT']

result = {
    'scope': 'Static original firmware evidence only. No key/event/GPIO injection or menu-open claim.',
    'source': str(SOURCE), 'sha256': sha,
    'keys': keys, 'actions': actions,
    'original_parser': {
        'address_thumb': '0x0664bf95', 'argument_registers': {'r0': 'argc', 'r1': 'argv pointer array'},
        'minimum_argc': 3,
        'argv1': 'Exact case-sensitive table name, or decimal table index',
        'argv2': 'Numeric action argument, not internal action value; key cases require0..3',
        'optional_argv3': 'Numeric delay passed to original0x066c23e8 when nonzero',
        'queue_call': '0x067f0dda',
        'queue_registers': {'r0': '0x115', 'r1': 0, 'r2': '0x11d', 'r3': 0},
        'queue_stack_arguments': ['0x0011f002', 'pointer to two u32 payload words', 8, '0xffffffff'],
        'payload_words': ['table_index', 'action_argument'],
        'string_test': 'KEY_CENTER, action1 is a candidate press; KEY_CENTER, action0 is release. This is an invocation description, not a tested shell command.',
    },
    'original_message_handler': {
        'receiving_task': 'uiTSK285, entry0x06475dd5; in pulse-timer-60s run_count149 and waiting on queue0x115',
        'queue_receive': '0x06475df0 calls0x067ea67a(r0=0x115,r1=message_buffer,r2=-1)',
        'receiver_dispatch': '0x06475e20 ->0x06475c6e ->0x06475886; key/control dispatch is attempted before other message classes',
        'entry': '0x06475886', 'message_code_offset': 8, 'payload_pointer_offset': 12,
        'match': '0x064758a0 compares0x0011f002;0x064759c6 calls0x06643680(payload)',
        'buffer_release': '0x06475c04 calls0x067f0dd0 after dispatch',
    },
    'key_center': {
        'table_index': 7, 'mask': '0x10', 'bank': 0,
        'case': '0x06643776', 'handler': '0x066435a0',
        'action_lookup': 'Reads one byte from0x06f10424 + action_argument*8',
        'state': 'Copies existing0x58-byte input state from0x08ca9624 and sets current/trigger/repeat/release fields',
        'state_dispatch': '0x06643648 calls0x066431de; mode/state guards remain original',
        'ui_key_mapping': '0x0663f02e tests bank0 trigger bit4 and calls0x066bddc8(r0=7,r1=0)',
        'ui_event': '0x066bddc8 passes event0x0f000002 and (action<<16)|key to0x066beea6',
        'alternate_live_handler': 'bank0 table0x06f10554 {mask0x10,trigger1,flags1,handler0x06646e85}; original handler can call0x066be02a(0) at0x06647092 after state checks',
        'menu_meaning': 'Candidate for camera MENU/SET from center-key role and routing, not yet runtime-confirmed. Physical GPIO mapping not established.',
    },
    'separate_keys': {
        'KEY_Q_MENU': {'index': 15, 'mask': '0x2000', 'bank': 0, 'handler_table': '0x06f10584', 'handler': '0x06649983'},
        'KEY_SET': {'index': 58, 'mask': '0x200000', 'bank': 1, 'case': '0x0664378c', 'handler': '0x0664353a'},
    },
    'restrictions': [
        'Do not label a software-key invocation as GPIO or physical key emulation.',
        'Table index, bit mask, internal action and final UI key code are different namespaces.',
        'No runtime safety/liveness of calling this API from an arbitrary halted task is established; use a suitable original task context and bounded probe.',
        'Do not bypass original input guards by calling a final menu-open transition directly.',
    ],
}
(HERE / 'key-event-evidence.json').write_text(json.dumps(result, indent=2)+'\n')

ranges = [
    (0x06475dd4,0x50), (0x06475c6e,0x1a),
    (0x0664bf94,0x94), (0x0664c054,0x16),
    (0x06475886,0x30), (0x06475980,6), (0x064759c4,8), (0x06475c04,8),
    (0x06643680,0x20), (0x06643776,0x2c),
    (0x066435a0,0xe0), (0x066431de,0x9a),
    (0x0663f02e,0x10), (0x066bddc8,0xc),
    (0x0663fe8c,0x14), (0x0663ff94,0x1e),
    (0x06646e84,0x1b0), (0x0664704c,0x4e), (0x066be02a,4),
]
lines = [f'Original program SHA256={sha}, Thumb; literal pools may decode as instructions.']
for address, size in ranges:
    lines.append('\nRANGE '+hex(address))
    md = capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
    md.detail = True
    for ins in md.disasm(program[address-BASE:address-BASE+size],address):
        suffix = ''
        if ins.mnemonic.startswith('ldr') and len(ins.operands)>1:
            operand=ins.operands[1]
            if operand.type==capstone.arm.ARM_OP_MEM and operand.mem.base==capstone.arm.ARM_REG_PC:
                at=((ins.address+4)&~3)+operand.mem.disp
                suffix=f'; literal[{at:08x}]={struct.unpack_from("<I",program,at-BASE)[0]:08x}'
        lines.append(f'{ins.address:08x}: {ins.bytes.hex():10} {ins.mnemonic:9} {ins.op_str} {suffix}')
(HERE/'key-events.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Extracted125 original script entries,4 action mappings, and static dispatch evidence; no injection performed.')
