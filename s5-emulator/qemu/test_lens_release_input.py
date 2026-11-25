#!/usr/bin/env python3
"""Test opt-in SPI3 input bit through original S5 read/write routines."""
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
    report = {'scope': 'Original SPI3 register read/write routines with explicit controller setup; external input hypothesis, not lens presence or ACK',
              'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'program_sha256': sha, 'original_instructions_modified': False,
              'guest_ui_or_event_writes': False,
              'setup': ['SPI channel address table restored', '8-bit channel descriptor',
                        'PL022 CR0=01c7,CPSR=2,CR1=0'], 'checks': []}
    with QemuArm(binary, args.output / 'vm', machine='s5-rtos', smp=4,
                 ram='512M', loads=[(program, 0x6280000)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        path = '/machine/rtc-peer'
        def state():
            return json.loads(q.qmp.command('qom-get', {'path': path, 'property': 'state-json'}))
        def enabled(value):
            q.qmp.command('qom-set', {'path': path, 'property': 'lens-release-input-high', 'value': value})
        def call(entry, r0=0, r1=0, thumb=True):
            q.select_cpu(0)
            for reg, value in [('cpsr', 0x1f3 if thumb else 0x1d3),
                               ('sp', 0x25000), ('lr', 0x10010),
                               ('r0', r0), ('r1', r1), ('pc', entry)]:
                q.write_reg(reg, value)
            q.resume_cpu(0, timeout=3)
            assert q.read_reg('pc') == 0x10010, hex(q.read_reg('pc'))
            return q.read_reg('r0')
        q.select_cpu(0)
        q.write_mem(0x10000, struct.pack('<2I', 0xe1c010b0, 0xe12fff1e))
        q.breakpoint_add(0x10010)
        q.write_mem(0x8ab472c, struct.pack('<7I', *[0x7f040000 + i*0x1000 for i in range(7)]))
        q.write_mem(0x9112628 + 3*20 + 12, struct.pack('<I', 8))
        for offset, value in [(0, 0x01c7), (0x10, 2), (4, 0)]:
            call(0x10000, 0x7f043000 + offset, value, thumb=False)
        assert state()['lens_release_input']['enabled'] is False

        def write(reg, value):
            assert call(0x6753036, reg, value) == 0
        def read(reg):
            q.write_mem(0x100200, bytes([0xa5])*16)
            assert call(0x6752fb0, reg, 0x100204) == 0
            data = q.read_mem(0x100200, 16)
            assert data[:4] == bytes([0xa5])*4 and data[5:] == bytes([0xa5])*11
            return data[4]

        write(0x044e, 2)
        assert read(0x044e) == 2
        enabled(True)
        assert read(0x044e) == 3
        assert state()['registers']['0x044e'] == 2
        enabled(False)
        assert read(0x044e) == 2
        report['checks'].append('original6752fb0: baseline02 -> enabled03 -> disabled02; stored latch remains02')
        enabled(True)
        for value in [0, 2, 0x80, 0xfe, 0xff]:
            write(0x044e, value)
            assert read(0x044e) == value | 1
            s = state()
            assert s['registers']['0x044e'] == value
            assert s['lens_release_input']['stored_value'] == value
            assert s['lens_release_input']['effective_value'] == value | 1
        report['checks'].append('original writes retained exactly; only read bit0 changes for00,02,80,fe,ff')
        for reg, value in [(0x0448, 0x10), (0x044a, 0x77), (0x044c, 0x4a), (0x0402, 0x35)]:
            write(reg, value)
            assert read(reg) == value
        report['checks'].append('other SPI registers retain exact byte values')
        write(0x044e, 2)
        enabled(False)
        assert read(0x044e) == 2
        before = state()
        assert before['unknown_reads'] == before['malformed_frames'] == 0
        assert before['lens_release_input']['affected_reads'] == 6
        assert read(0x0554) == 0
        after = state()
        assert after['unknown_reads'] == 1 and '0x0554' in after['unsupported_registers']
        assert after['lens_release_input']['affected_reads'] == 6
        report['checks'].append('unknown register remains reported unsupported; opt-in does not mask unknowns')
        report['final_state'] = after
    report['passed'] = True
    (args.output / 'verification.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'passed': True, 'checks': len(report['checks']),
                      'original_read_entry': '0x06752fb0',
                      'original_write_entry': '0x06753036', 'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
