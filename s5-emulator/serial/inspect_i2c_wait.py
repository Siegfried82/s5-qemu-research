#!/usr/bin/env python3
"""Read-only saved-state evidence: original I2C queue, worker and completion event."""
from pathlib import Path
import hashlib, json, mmap, struct
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = 0x06280000
program = (ROOT / 's5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
folder = ROOT / 's5-emulator/menu-smp/results/pulse-timer-boot'
boot = json.loads((folder / 'result.json').read_text())
with (folder / 'ram.bin').open('rb') as f:
    ram = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda a: struct.unpack_from('<I', ram, a)[0]
    def task(t):
        sp, end, args, obj = u32(t + 8), u32(t + 16), u32(t + 0x114), u32(t + 0x108)
        return {
            'tcb': hex(t), 'name': ram[t+0xc4:t+0xd4].split(b'\0')[0].decode(),
            'entry': hex(u32(t + 0xd8)), 'run_count': u32(t + 4),
            'wait_type': u32(t + 0x100), 'event_id': u32(t + 0x104),
            'remaining_ticks': hex(u32(t + 0x4c)), 'object': hex(obj),
            'event_bits': u32(obj + 8),
            'wait_args': [hex(u32(args+i)) for i in range(0, 12, 4)],
            'saved_sp': hex(sp),
            'stack_words': {hex(a): hex(u32(a)) for a in range(sp, end + 1, 4)},
        }
    result = {
        'scope': 'Original firmware and saved RAM; no injected key, event, code patch or fabricated I2C success',
        'program_sha256': sha, 'snapshot': str(folder / 'ram.bin'),
        'timer_progress': {hex(a): u32(a) for a in [0x08aaa5f4, 0x08aaa7b4, 0x08ab6394, 0x08ab18f8]},
        'main_task': task(0x08dfe890), 'i2c_worker': task(0x08e01110),
        'caller': {
            'original_string': 'audio_hpamp_on()', 'function': '0x064a070e',
            'gpio_reset': '0x7f081c14 writes0x10000000 then0x10000001; not proven SPI5 CS',
            'register_write': '0x064a0c30', 'bus': 0, 'address_argument': '0x30',
            'tx_buffer': '0x08a94e0d', 'tx_bytes_at_snapshot': ram[0x08a94e0d:0x08a94e0f].hex(),
            'register_loop': '0x064a0c4a writes paired register sequences until counter0x3d',
            'register_loop_stack_counter': '0x26',
        },
        'main_call_chain': ['0x063c8b7b', '0x0647e837', '0x0647c4b1', '0x064a0923', '0x064a0523', '0x064a0c85', '0x064a0c49', '0x066ce489', '0x066ce3e9', '0x065b24df', '0x065b2419'],
        'software_events': {
            'completion': {'global': '0x06f49744', 'id': u32(0x06f49744), 'mask': 3, 'mode': 'OR', 'wait': 'forever', 'call': '0x066ce3e4'},
            'hardware_worker': {'global': '0x06f49740', 'id': u32(0x06f49740), 'mask': 3, 'mode': 'OR', 'call': '0x066cdfea', 'wait': 'bounded in this snapshot,29ticks remaining'},
            'callback': {'address': '0x066ce2aa', 'behavior': 'For slot0, *status==0 sets completion bit1; nonzero status shifts to bit2. Calls original event-set0x06576118.', 'caller_return': '0x066ce3e8 returns true only when bit1 was observed'},
        },
        'retry': {'function': '0x066ce016', 'evidence': '0x066ce0d2 increments retry byte;0x84 converts to4 and finishes error callback', 'maximum_attempts': 4},
        'interpretation': 'Main forever completion wait does not imply permanent deadlock: worker has a finite timeout and error retries. A longer bounded boot or a generic absent-peer error controller can distinguish slow timeout-driven progress.',
        'physical_endpoint_model': 'No successful ACK/reply or exact audio amplifier part identity established',
        'menu_rendered': False,
    }
    assert result['main_task']['event_id'] == 19
    assert result['i2c_worker']['event_id'] == 18
    assert result['timer_progress']['0x8ab6394'] > 100
    (HERE / 'i2c-wait-evidence.json').write_text(json.dumps(result, indent=2)+'\n')

lines = [f'Original program sha256={sha}, Thumb disassembly; literal pools may decode as instructions.']
for a, n in [(0x064a070e,0x46),(0x064a0c30,0x6a),(0x066ce452,0x3a),(0x066ce348,0xcc),(0x066ce2aa,0x12),(0x066cdf62,0xb4),(0x066ce016,0x150)]:
    lines.append('\nRANGE '+hex(a))
    md=capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    for i in md.disasm(program[a-BASE:a-BASE+n],a):
        lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}')
(HERE/'i2c-wait.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Wrote i2c-wait-evidence.json and i2c-wait.disasm.txt; snapshot assertions passed.')
