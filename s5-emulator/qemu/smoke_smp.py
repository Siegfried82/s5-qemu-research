#!/usr/bin/env python3
"""Check actual CPU identities and coherent RAM on s5-rtos (synthetic code)."""
import json
from pathlib import Path
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'probe'))
from qemu_arm import QemuArm
from generate_board_setup import code_from_object


def main():
    out = HERE / 'results-smp-smoke'
    out.mkdir(exist_ok=True)
    source = out / 'probe.S'
    source.write_text('''\
.syntax unified
.arm
.text
.global _start
_start:
    mrc p15, 0, r0, c0, c0, 5
    and r3, r0, #3
    str r0, [r1, r3, lsl #2]
    ldr r2, [r4]
    add r2, r2, #1
    str r2, [r5]
    bx lr
''')
    obj = out / 'probe.o'
    subprocess.run(['clang', '--target=arm-none-eabi', '-march=armv7-a',
                    '-marm', '-c', str(source), '-o', str(obj)], check=True)
    code = code_from_object(obj.read_bytes())
    result = {'kind': 'synthetic-machine-smoke', 'firmware_executed': False}
    with QemuArm(HERE.parent / 'toolchain/build/qemu-system-arm', out,
                 ram='512M', machine='s5-rtos', smp=4,
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000),
                                 (0x80000000, 0x20000000)]) as emu:
        result['threads'] = emu.threads()
        assert len(result['threads']) == 4
        result['qmp_cpus'] = emu.qmp.command('query-cpus-fast')
        emu.write_mem(0x10000, code)
        assert emu.read_mem(0x80010000, len(code)) == code
        emu.write_mem(0x80021000, struct.pack('<I', 0x11223344))
        assert emu.read_mem(0x21000, 4) == struct.pack('<I', 0x11223344)
        emu.write_mem(0x21000, struct.pack('<I', 0))
        emu.breakpoint_add(0x10100, 4)
        result['executions'] = []
        for cpu in range(4):
            emu.select_cpu(cpu)
            for register, value in [('cpsr', 0x1d3), ('pc', 0x10000),
                                    ('lr', 0x10100), ('r1', 0x20000),
                                    ('r4', 0x21000), ('r5', 0x80021000)]:
                emu.write_reg(register, value)
            stop = emu.resume_cpu(cpu, timeout=2)
            emu.select_cpu(cpu)
            mpidr, counter = emu.read_reg('r0'), emu.read_reg('r2')
            assert mpidr & 3 == cpu, (cpu, mpidr)
            assert counter == cpu + 1, (cpu, counter)
            assert emu.read_reg('pc') == 0x10100
            result['executions'].append({'cpu': cpu, 'mpidr': hex(mpidr),
                                         'shared_counter': counter, 'stop': stop})
        assert struct.unpack('<I', emu.read_mem(0x21000, 4))[0] == 4
        result['coherent_alias_both_directions'] = True
        result['guest_cross_cpu_counter'] = 4
    result['passed'] = True
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
