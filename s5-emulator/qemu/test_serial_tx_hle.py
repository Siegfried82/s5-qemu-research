#!/usr/bin/env python3
"""Validate original transmit routines and a default-off controller HLE."""
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
    assert hashlib.sha256(program.read_bytes()).hexdigest() == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
    report = {'scope': 'Unmodified original ARM transmit routines, synthetic descriptor/data, no peripheral peer',
              'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'cases': []}
    for label, enabled, entry, records in [
        ('absent', False, 0x67091f2, [(0x1234, 0x56), (0x078d, 0)]),
        ('individual', True, 0x67091f2, [(0x1234, 0x56), (0x078d, 0)]),
        ('batch32', True, 0x67093de, [(0x1200 + i, i + 1) for i in range(29)]),
    ]:
        with QemuArm(binary, args.output / label, machine='s5-rtos', smp=4, ram='512M',
                     loads=[(program, 0x6280000)],
                     address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
            def state():
                return json.loads(q.qmp.command('qom-get', {'path': '/machine', 'property': 'tx-serial-state-json'}))
            def mode(value):
                q.qmp.command('qom-set', {'path': '/machine', 'property': 'tx-serial-hle', 'value': value})
            assert state()['enabled'] is False
            if enabled:
                mode(True)
            q.select_cpu(0)
            q.write_mem(0x10000, struct.pack('<4I', 0xe5901000, 0xe12fff1e,
                                           0xe5801000, 0xe12fff1e))
            q.breakpoint_add(0x10010)
            q.write_mem(0x100100, struct.pack('<2I', len(records), 0x100200))
            q.write_mem(0x100200, b''.join(struct.pack('<HBB', a, d, 0) for a, d in records))
            for reg, value in [('cpsr', 0x1f3), ('sp', 0x24000), ('lr', 0x10010),
                               ('r0', 0x100100), ('pc', entry)]:
                q.write_reg(reg, value)
            reply = q.resume_cpu(0, timeout=10)
            assert q.read_reg('pc') == 0x10010, (label, reply, hex(q.read_reg('pc')))
            result = q.read_reg('r0')
            assert result == (0 if enabled else 1), (label, result)
            s = state()
            if enabled:
                expected = ([bytes([0x81, a >> 8, a & 255, d]).hex() for a, d in records]
                            if label == 'individual' else
                            [bytes([0x81, 0x12, 0] + [d for _, d in records]).hex()])
                assert s['tx_packets_hex'] == expected, s
                assert s['completed'] == s['acknowledgements'] == s['stops'] == len(expected), s
                assert s['stage'] == s['unsupported'] == 0
                assert not s['rx_or_peer_ack_generated'] and not s['pixel_or_dsp_execution_generated']
            else:
                assert s['completed'] == s['bytes'] == 0 and s['tx_packets_hex'] == []
            case = {'name': label, 'original_entry': hex(entry), 'original_return': result,
                    'state': s, 'passed': True}
            report['cases'].append(case)
            if label == 'individual':
                def access(offset, value=None, expect_stop=False):
                    for reg, v in [('cpsr', 0x1d3), ('r0', 0x7b193400 + offset), ('r1', value or 0),
                                   ('lr', 0x10010), ('pc', 0x10000 if value is None else 0x10008)]:
                        q.write_reg(reg, v)
                    q.resume_cpu(0, timeout=2)
                    if expect_stop:
                        assert q.read_reg('pc') != 0x10010
                    else:
                        assert q.read_reg('pc') == 0x10010
                    return q.read_reg('r1')
                assert access(0x44) == 0  # No always-ready status.
                # A count4 TX prefix80 is unsupported, not reported completed.
                access(0x10, 0x56341280)
                access(8, 1, expect_stop=True)
                failed = state()
                assert failed['unsupported'] == 1 and failed['completed'] == 2 and failed['stage'] == 0
                assert access(0x44) == 0
                # Unknown byte count/data coverage cannot create completion either.
                access(4, 0x320)
                access(0xc, 0)
                access(0x10, 0x56341281)
                access(8, 1, expect_stop=True)
                assert state()['unsupported'] == 2 and state()['completed'] == 2
                mode(False)
                assert access(0x44) == 0
                case['unsupported_requests_rejected'] = state()
    report['passed'] = True
    (args.output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': True, 'cases': len(report['cases']), 'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
