#!/usr/bin/env python3
"""Exercise original status helpers and scene gate against four shared words."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from probe.qemu_arm import QemuArm


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    binary = ROOT / 'toolchain/build/qemu-system-arm'
    program = ROOT.parent / 's5-v29-offline/unpacked-v29/03_program.bin'
    sha = hashlib.sha256(program.read_bytes()).hexdigest()
    assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
    report = {'scope': 'Original status write/read/gate code, synthetic source words; no fake event, component completion or scene',
              'source_sha256': sha,
              'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'checks': []}
    with QemuArm(binary, args.output / 'vm', machine='s5-rtos', smp=4, ram='512M',
                 loads=[(program, 0x6280000)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        def state():
            return json.loads(q.qmp.command('qom-get', {'path': '/machine', 'property': 'software-status-state-json'}))
        def call(cpu, entry, r0, r1=0, thumb=True):
            q.select_cpu(cpu)
            for reg, value in [('cpsr', 0x1f3 if thumb else 0x1d3),
                               ('sp', 0x25000 + cpu * 0x1000), ('lr', 0x10010),
                               ('r0', r0), ('r1', r1), ('pc', entry)]:
                q.write_reg(reg, value)
            q.resume_cpu(cpu, timeout=3)
            assert q.read_reg('pc') == 0x10010, (cpu, hex(q.read_reg('pc')))
            return q.read_reg('r0')
        q.select_cpu(0)
        # ARM raw STR/LDR helpers solely for the adjacent unmodeled word check.
        q.write_mem(0x10000, struct.pack('<4I', 0xe5801000, 0xe12fff1e,
                                       0xe5900000, 0xe12fff1e))
        q.breakpoint_add(0x10010)
        assert [r['value'] for r in state()['registers']] == [0]*4
        assert call(0, 0x6469ebe, 2) == 0x8000000e
        report['checks'].append('initial zero does not satisfy original scene-mask2 gate')
        patterns = [
            (2, [0x12345678, 0x9abcdef0, 2, 0x87654321]),
            (1, [0xffffffff, 0, 8, 0x13579bdf]),
            (0, [0, 0, 0, 0]),
        ]
        for cpu, words in patterns:
            q.write_mem(0x100200, struct.pack('<4I', *words))
            assert call(cpu, 0x6680708, 0x100200) == 0
            assert call((cpu + 1) % 3, 0x6680720, 0x100240) == 0
            assert struct.unpack('<4I', q.read_mem(0x100240, 16)) == tuple(words)
            assert [r['value'] for r in state()['registers']] == words
            for mask in [2, 4, 8, 0xa]:
                expected = 0 if words[2] & mask == mask else 0x8000000e
                assert call(0, 0x6469ebe, mask) == expected
            report['checks'].append({'writer_cpu': cpu, 'reader_cpu': (cpu + 1) % 3,
                                     'exact_words': words, 'original_gates_checked': [2,4,8,10]})
        before = state()
        call(0, 0x10000, 0x7c0151f0, 0xffffffff, thumb=False)
        assert call(0, 0x10008, 0x7c0151f0, thumb=False) == 0
        assert state() == before
        report['checks'].append('adjacent51f0 remains unmodeled and does not alter retained words')
        assert not before['automatic_completion'] and not before['irq_or_event_generated']
        assert not before['adjacent_reset_command_modeled']
        assert all(row['writes'] == 3 for row in before['registers'])
        report['final_state'] = before
    report['passed'] = True
    (args.output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': True, 'checks': len(report['checks']), 'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
