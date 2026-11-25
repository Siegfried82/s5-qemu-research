#!/usr/bin/env python3
"""Read-only audit of the native asynchronous input lock and its completion."""
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
historical = HERE.parent/'menu-smp/results/local-path-transform'
with (historical/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    word = lambda address: struct.unpack_from('<I', ram, address)[0]
    snapshot = {
        'secondary_stage': word(0x08aa090c),
        'pending_callback_byte': ram[0x08aa0908],
        'configuration_substep': hex(word(0x08aa0918)),
        'secondary_substep': hex(word(0x08c98888)),
        'active_callback': hex(word(0x08a9d634)),
    }
    assert snapshot == {'secondary_stage': 2, 'pending_callback_byte': 1,
                        'configuration_substep': '0x8b4', 'secondary_substep': '0x870',
                        'active_callback': '0x682e88d'}
    tasks = []
    for tcb in (0x08e0009c, 0x08dff67c):
        assert ram[tcb:tcb+4] == b'DRHT'
        sp = word(tcb+8)
        end = word(tcb+16)
        tasks.append({
            'tcb': hex(tcb), 'id': word(tcb+0xc0), 'entry': hex(word(tcb+0xd8)),
            'saved_sp': hex(sp), 'wait_type': word(tcb+0x100),
            'event_id': word(tcb+0x104), 'event_object': hex(word(tcb+0x108)),
            'saved_stack_words': [{'address': hex(address), 'value': hex(word(address))}
                                  for address in range(sp, end, 4)
                                  if word(address) != 0],
        })
    ram.close()

completed_run = HERE.parent/'menu-smp/results/local-shared-image-stride'
completed = json.loads((completed_run/'result.json').read_text())
key = completed['software_key_probe']['requests'][0]
assert key['input_route_gate']['secondary_stage'] == 1
assert key['input_route_gate']['wait_steps'] == 11
report = {
    'scope': 'Original static code plus prior saved execution. No guest calls, state writes, IRQs, or new peripherals.',
    'program_sha256': sha,
    'historical_snapshot': str(historical.resolve()),
    'historical_snapshot_limit': 'Graphics execution stopped early. Its waiting tasks do not establish a permanent stall or missing peripheral.',
    'historical_state': snapshot,
    'historical_tasks': tasks,
    'original_control_flow': [
        'Task285 loop06475e24 calls066add00, whose first call is0682f0e8.',
        '0682f0e8 selects0682eeaa(08c98880) for secondary_stage2.',
        '0682eeaa initially writes2 at08aa090c and invokes0682e9e2(1).',
        '0682e894 sets byte08aa0908=1 and passes callback0682e88d to0646e564(config44B,1,callback).',
        '0682e9e2 records configuration substep8b4 while the pending byte is nonzero;0682eeaa records substep870.',
        '0646e48c copies the request to its original static queue and signals originalqueue16.',
        'Task20 entry0646e3a6 consumes that request and stores callback at08a9d634.',
        '0646e464 calls0646d5e8; only its return permits0646e472 BLX callback.',
        'Callback0682e88c writes0 to08aa0908. The next native UI loop resumes the state machine.',
        '0682ef10 writes secondary_stage1;0682ef30 writes substep888 after remaining native calls.',
    ],
    'nested_historical_wait': {
        'task20': 'Saved chain0646e469 ->0646dc6d ->0646d527 ->0657b8f5; event11 mask1 via0646d4aa(2).',
        'submission': '0646dc18 calls06483120 with callback0646d3d7, then waits for its case2 notification.',
        'task12': '06483120 sets event6 bit0; entry06483060 consumes it and calls06482d76.',
        'task12_saved_chain': '064830e3 ->06482d9b ->064813fb ->0657b8f5; event6 mask0x80.',
        'callback2_location': 'If the original path progresses,06482f24 sets r0=2 and06482f28 BLX invokes the registered callback.',
        'limits': 'No claim here that event86 is the source or that the historical waits require a model change.',
    },
    'later_observed_progress': {
        'run': str(completed_run.resolve()),
        'gate': key['input_route_gate'],
        'state': completed['original_ui_state_at_stop'],
        'conclusion': 'The native lock became ready without a direct callback, event-field write, or stage override. The original key handler subsequently confirmed the time-zone prompt.',
        'callback_observation_limit': 'This later result directly samples stage1 and downstream key calls; it does not contain a separate breakpoint trace at every internal callback.',
    },
}
(HERE/'secondary-input-stage-evidence.json').write_text(json.dumps(report, indent=2)+'\n')
cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
cs.detail = True
# Selected code blocks omit embedded literal pools and TBB data.
ranges = [(0x06475e20,0x06475e28),(0x066add00,0x066add0e),
          (0x0682e88c,0x0682e892),(0x0682e894,0x0682e8ae),
          (0x0682e9d4,0x0682ea18),(0x0682eeaa,0x0682ef36),
          (0x0682f0e8,0x0682f100),(0x0682f106,0x0682f10e),
          (0x0646e458,0x0646e478),(0x0646e48c,0x0646e4f8),
          (0x0646e5bc,0x0646e5d2),(0x06483248,0x06483286),
          (0x06483060,0x064830e4),(0x06482d76,0x06482d9c),
          (0x06482efa,0x06482f2a)]
lines = [f'Original SHA256={sha}. Selected Thumb code, no execution or injected state.']
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
(HERE/'secondary-input-stage.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Historical asynchronous wait and later natural input-ready transition verified.')
