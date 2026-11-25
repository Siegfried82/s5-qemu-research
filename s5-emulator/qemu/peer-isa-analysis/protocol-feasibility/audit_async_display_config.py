#!/usr/bin/env python3
"""Record an original asynchronous display wait and its later natural completion."""
import hashlib
import json
import mmap
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA


def main():
    p = PROGRAM.read_bytes()
    assert hashlib.sha256(p).hexdigest() == EXPECTED_SHA
    old = EMU / 'menu-smp/results/local-path-transform'
    latest = EMU / 'menu-smp/results/local-shared-image-stride'
    new = json.loads((latest / 'result.json').read_text())
    t = 0x08e0009c
    with (old / 'ram.bin').open('rb') as f:
        ram = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        u = lambda a: struct.unpack_from('<I', ram, a)[0]
        assert u(t + 0xd8) == 0x0646e3a7
        stack = {hex(a): hex(u(a)) for a in range(u(t+8), u(t+16)+1, 4)}
        observed = {
            'snapshot': str(old / 'ram.bin'), 'tcb': hex(t),
            'task_id': hex(u(t+0xc0)), 'entry': hex(u(t+0xd8)),
            'saved_sp': hex(u(t+8)), 'wait_type': hex(u(t+0x100)),
            'wait_id': hex(u(t+0x104)), 'wait_object': hex(u(t+0x108)),
            'timeout': hex(u(t+0x4c)), 'stack_words': stack,
            'registered_final_callback': hex(u(0x08a9d634)),
            'pending_input_byte_08aa0908': ram[0x08aa0908],
            'secondary_stage_08aa090c': u(0x08aa090c),
        }
    state = new['original_ui_state_at_stop']
    assert state['secondary_transition_stage_08aa090c'] == 1
    assert 'graphics_error' not in new
    report = {
        'scope': 'Offline original ARM async configuration contract. Earlier blocked snapshot is not evidence of a permanent hardware fault.',
        'program_sha256': EXPECTED_SHA,
        'earlier_observation': observed,
        'contract': {
            'config_entry': '0x0646e564',
            'arguments': ['r0=pointer to 0x44-byte config, not numeric config value0x44b',
                          'r1=mode (actual1)', 'r2=callback (actual0x0682e88d)'],
            'queue_id': '0x10', 'worker_task_id': '0x14 (decimal20)',
            'worker_entry': '0x0646e3a7',
            'enqueue': '646e48c copies0x44 bytes into08bdd3c0 slots, stores mode08bdd514, callback08bdd528 and request type08bdd53c; sends word0 to queue0x10.',
            'process': '646e458 sets busy, stores active callback08a9d634, invokes646d5e8. Only after that returns does646e472 BLX the saved callback.',
            'actual_wait_call_chain': ['646e464→646d5e8 (saved646e469)',
                                      '646dc68→646d4aa(2) (saved646dc6d)',
                                      '646d522→64bd04e→657b8a4 (saved646d527)'],
            'event_id': '0x0b', 'event_mask': '0x1',
            'lower_submission': '646dc18→6483120 with callback646d3d7; callback stored08ab4db0.648327e sets event0x6 mask1; consumer entry6483061 processes configuration.',
            'lower_callback_case2': '646d3d6 TBB case2→646d3f6 stores r1 byte08a9d62b and r2 byte08a9d62e, then sets event0x0b mask1 via64bd024. It does not call the final input callback directly.',
            'following_waits': 'After this mask1 wait,646dc7c waits case0x0f/eventmask40 and646dc88 waits case4/eventmask2. More original display work must finish before final callback.',
            'final_callback': '682e88c writes0 to pending byte08aa0908; upper original state machine can then advance secondary stage.',
        },
        'later_runtime': {
            'result': str(latest / 'result.json'), 'stop_reason': new['stop_reason'],
            'graphics_error': new.get('graphics_error'),
            'ui_state': state,
            'key_status': [r.get('status') for r in new['software_key_probe']['requests']],
            'meaning': 'With the subsequent graphics API implementations complete, original secondary stage naturally reaches1 and the real key route is consumed. No new event/callback/ready injection was required for this wait.',
        },
        'decision': 'No device-model change or direct callback/event manipulation justified by the earlier snapshot.',
        'guest_modified_by_this_audit': False,
    }
    (HERE / 'async-display-config-contract.json').write_text(json.dumps(report, indent=2)+'\n')
    ranges = [
        ('async config entry', 0x646e564, 0x646e5d2),
        ('queue worker and original completion call', 0x646e3a6, 0x646e48c),
        ('enqueue metadata and notification', 0x646e48c, 0x646e4f8),
        ('lower submit and consecutive waits', 0x646dbf4, 0x646dca4),
        ('callback case2 and case4', 0x646d3f6, 0x646d422),
        ('case2 event wait', 0x646d512, 0x646d534),
        ('final input callback', 0x682e88c, 0x682e894),
    ]
    c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    lines = ['Original SHA256: '+EXPECTED_SHA, 'Read-only audit; all IDs written in hexadecimal.']
    for label, a, b in ranges:
        lines.append(f'\n{label}: {a:#x}..{b:#x}')
        lines += [f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:10} {i.op_str}'
                  for i in c.disasm(p[a-BASE:b-BASE], a)]
    (HERE / 'async-display-config-disassembly.txt').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'old_wait_event': '0x0b', 'queue': '0x10',
                      'latest_secondary_stage': 1, 'device_fix_required': False}, indent=2))


if __name__ == '__main__':
    main()
