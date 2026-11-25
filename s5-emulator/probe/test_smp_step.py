#!/usr/bin/env python3
"""Actual four-core stepping regression; no original firmware is executed."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from probe.qemu_arm import QemuArm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    binary = ROOT/'toolchain/build/qemu-system-arm'
    report = {'scope': 'Synthetic ARM instructions; thread-qualified steps must leave three other CPU contexts unchanged',
              'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'steps': []}
    with QemuArm(binary, out/'qemu', machine='s5-rtos', ram='512M', smp=4,
                 address_ranges=[(0, 0x20000000)]) as emu:
        ids = emu.threads()
        bases = [0x10000+i*0x100 for i in range(4)]
        for i, base in enumerate(bases):
            emu.write_mem(base, struct.pack('<2I', 0xe2800001, 0xeafffffd))
            emu.select_cpu(i)
            for n in range(16): emu.write_reg(n, 0)
            emu.write_reg('cpsr', 0x1d3)
            emu.write_reg('r0', i*100)
            emu.write_reg('pc', base)

        # Match the debugger's real use after boot has started all vCPUs.
        try:
            emu.resume_all(timeout=.01)
        except TimeoutError:
            pass
        for i, base in enumerate(bases):
            emu.select_cpu(i)
            emu.write_reg('r0', i*100)
            emu.write_reg('pc', base)

        def snapshot():
            values = []
            for i in range(4):
                emu.select_cpu(i)
                values.append(emu.registers())
            return values

        for target in (2, 0, 3, 1):
            before = snapshot()
            for other in range(4):
                if other != target:
                    emu.breakpoint_add(before[other]['r15'], kind=4)
            emu.select_cpu(target)
            try:
                reply = emu.step(timeout=2)
            except TimeoutError:
                report['timeout'] = {'target': target, 'before': before, 'after': snapshot(),
                    'gic': emu.qmp.command('qom-get', {'path':'/machine','property':'gic-state-json'})}
                (out/'verification.json').write_text(json.dumps(report, indent=2)+'\n')
                raise
            after = snapshot()
            assert re.search(r'thread:'+re.escape(ids[target])+r';', reply), reply
            assert after[target]['r0'] == before[target]['r0']+1
            assert after[target]['r15'] == before[target]['r15']+4
            for other in range(4):
                if other != target:
                    assert after[other] == before[other], ('Other CPU ran', target, other)
                    emu.breakpoint_remove(before[other]['r15'], kind=4)
            report['steps'].append({'cpu': target, 'reply': reply, 'other_cpus_unchanged': True,
                                    'r0': after[target]['r0'], 'pc': hex(after[target]['r15'])})
            # A branch step must also be confined, not just a straight-line add.
            emu.select_cpu(target)
            reply = emu.step(timeout=2)
            branched = snapshot()
            assert branched[target]['r15'] == bases[target]
            assert branched[target]['r0'] == after[target]['r0']
            assert all(branched[i] == after[i] for i in range(4) if i != target)
            report['steps'].append({'cpu': target, 'reply': reply, 'branch_step': True,
                                    'other_cpus_unchanged': True, 'pc': hex(branched[target]['r15'])})
    report['passed'] = True
    (out/'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'passed': True, 'actual_steps': len(report['steps']), 'output': str(out)}))


if __name__ == '__main__': main()
