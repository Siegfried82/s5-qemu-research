#!/usr/bin/env python3
"""Exercise LOAD-only rearming after a real one-shot expiry; no firmware loaded."""
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
    ap.add_argument('--expect-legacy', action='store_true')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    binary = HERE.parent / 'toolchain/build/qemu-system-arm'
    report = {'scope': 'Synthetic timer MMIO only, no camera firmware or ready writes',
              'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'cases': []}
    for machine, timer, ram, smp in [('s5-rtos', 0x7f023000, '512M', 4),
                                    ('s5-linux', 0x7f02a000, '64M', 1)]:
        case = {'machine': machine, 'expiries': []}
        report['cases'].append(case)
        with QemuArm(binary, args.output / machine, machine=machine, smp=smp, ram=ram,
                     address_ranges=[(0x70000000, 0x10000000), (0x80000000, 0x4000000)]) as q:
            q.select_cpu(0)
            q.write_mem(0x80010000, struct.pack('<9I', 0xe5901000, 0xe12fff1e,
                                             0xe5801000, 0xe12fff1e,
                                             0xe1a00000, 0xe1a00000,
                                             0xe1a00000, 0xe1a00000, 0xeafffffe))
            q.breakpoint_add(0x80010010)
            q.write_reg('cpsr', 0x1d3)
            def access(off, value=None):
                for reg, v in [('r0', timer + off), ('r1', value or 0),
                               ('lr', 0x80010010),
                               ('pc', 0x80010000 if value is None else 0x80010008)]:
                    q.write_reg(reg, v)
                q.resume_cpu(0, timeout=2)
                assert q.read_reg('pc') == 0x80010010
                return q.read_reg('r1')
            def wr(off, val):
                access(off, val)
            def rd(off):
                return access(off)
            def run():
                q.write_reg('pc', 0x80010020)
                try:
                    q.resume_cpu(0, timeout=0.04)
                except TimeoutError:
                    pass  # Expected bounded ARM branch loop, IRQ masked.
                assert q.read_reg('pc') == 0x80010020
            # Original 6759544 config: one-shot/32-bit/IRQ/bit4, then enable.
            wr(8, 0x33)
            wr(0, 27000)
            wr(8, 0xb3)
            for index in range(3):
                run()
                values = {'index': index, 'load': rd(0), 'value': rd(4),
                          'control': rd(8), 'ris': rd(0x10), 'mis': rd(0x14)}
                case['expiries'].append(values)
                expected = index == 0 or (machine == 's5-rtos' and not args.expect_legacy)
                assert (values['ris'] == 1 and values['value'] == 0) == expected, values
                # Clear status to observe the next real expiry independently.
                wr(0xc, 0)
                # Original 6554ca2 writes only LOAD on each later sequence step.
                wr(0, 54000 + index * 27000)
            wr(8, 0x33)
            wr(0xc, 0)
            wr(0, 27000)
            run()
            assert rd(0x10) == 0 and rd(4) == 27000
            case['disabled_load_does_not_start'] = True
            wr(8, 0xf2)  # Existing periodic timer behavior remains functional.
            for _ in range(3):
                run()
                assert rd(0x10) == 1
                wr(0xc, 0)
            case['periodic_three_expiries'] = True
        case['passed'] = True
    report['passed'] = True
    (args.output / 'verification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
