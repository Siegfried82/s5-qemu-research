#!/usr/bin/env python3
"""Synthetic-GIC checks for explicit frame TEST edges, without firmware."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from probe.qemu_arm import QemuArm


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    binary = HERE.parent / 'toolchain/build/qemu-system-arm'
    report = {'scope': 'Synthetic GIC/MMIO only; no firmware, frame data or guest flag writes',
              'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'checks': []}
    def check(name, condition):
        report['checks'].append({'name': name, 'passed': bool(condition)})
        assert condition, name
    with QemuArm(binary, args.output / 'qemu', machine='s5-rtos', smp=4, ram='512M',
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        q.select_cpu(0)
        q.write_mem(0x10000, struct.pack('<4I', 0xe5901000, 0xe12fff1e,
                                       0xe5801000, 0xe12fff1e))
        q.breakpoint_add(0x10010)
        q.write_reg('cpsr', 0x1d3)
        def access(addr, value=None):
            for reg, v in [('r0', addr), ('r1', value or 0), ('lr', 0x10010),
                           ('pc', 0x10000 if value is None else 0x10008)]:
                q.write_reg(reg, v)
            q.resume_cpu(0, timeout=2)
            assert q.read_reg('pc') == 0x10010
            return q.read_reg('r1')
        def get(name):
            return q.qmp.command('qom-get', {'path': '/machine', 'property': name})
        def put(name, value):
            return q.qmp.command('qom-set', {'path': '/machine', 'property': name, 'value': value})
        def rejected(name, value):
            try:
                put(name, value)
            except RuntimeError as e:
                report.setdefault('expected_rejections', []).append(str(e))
                return True
            return False
        check('default disabled', get('frame-test-mode') is False)
        check('default counters zero', json.loads(get('frame-test-state-json'))['pulses'] == {'192': 0, '193': 0})
        check('disabled pulse rejected', rejected('frame-test-pulse', 192))
        put('frame-test-mode', True)
        for irq in (0, 191, 194, 196, 232, 448):
            check('reject outside allowlist ' + str(irq), rejected('frame-test-pulse', irq))
        check('mode changes do not modify display MMIO',
              access(0x7c00011c) == 0 and access(0x7c050ef0) == 0)
        for irq in (192, 193):
            cfg = 0x70001c00 + (irq // 16) * 4
            shift = (irq % 16) * 2
            access(cfg, (access(cfg) & ~(3 << shift)) | (2 << shift))
            target = 0x70001800 + (irq & ~3)
            shift = (irq % 4) * 8
            access(target, (access(target) & ~(255 << shift)) | (1 << shift))
            word, bit = irq // 32, 1 << (irq % 32)
            access(0x70001100 + word * 4, bit)
            access(0x70001280 + word * 4, bit)
            check('pending starts clear ' + str(irq), not access(0x70001200 + word * 4) & bit)
            put('frame-test-pulse', irq)
            check('edge reaches actual GIC ' + str(irq), bool(access(0x70001200 + word * 4) & bit))
            access(0x70001280 + word * 4, bit)
            check('ordinary GIC clear ' + str(irq), not access(0x70001200 + word * 4) & bit)
        put('frame-test-mode', False)
        check('disable rejects further pulse', rejected('frame-test-pulse', 193))
        check('state is read-only', rejected('frame-test-state-json', '{}'))
        state = json.loads(get('frame-test-state-json'))
        report['state'] = state
        check('request accounting', state['pulse_requests'] == 10 and state['pulse_rejected'] == 8
              and state['pulses'] == {'192': 1, '193': 1})
        check('test-only declared scope', not state['enabled'] and not state['automatic_timing']
              and not state['hardware_clock_verified'] and not state['guest_callback_or_flag_writes']
              and not state['mmio_status_modified'])
        check('independent display test remains disabled', get('display-test-mode') is False)
    report['passed'] = True
    (args.output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': True, 'checks': len(report['checks']), 'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
