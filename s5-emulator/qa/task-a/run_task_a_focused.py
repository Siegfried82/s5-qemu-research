#!/usr/bin/env python3
"""Task A focused verification for SPI3 register 0x044e bit0 external input model.

1. Reproduces and records the exact UND exception in unmodified qemu/test_lens_release_input.py
   (0x06753036 -> 0x064e6a78 -> 0x066c261c executes vpush {d0-d3} at 0x066c2620 with cold-reset
   CPACR/FPEXC disabled).
2. Runs the exact original firmware routines 0x06752fb0 (read) and 0x06753036 (write) in a clean
   s5-rtos VM with architectural CPACR/FPEXC VFP enable (matching boot-state.S lines 8-13),
   verifying guest breakpoint hits at 0x06752fb0, 0x06753036, and 0x0642c43c and all register/bit
   invariants without modifying original firmware instructions or guest UI/event flags.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from probe.qemu_arm import QemuArm

EXPECTED_PROGRAM_SHA256 = 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'


def diagnose_unmodified_defect(binary: Path, program: Path, out_dir: Path) -> dict:
    """Confirm exact root cause of unmodified test_lens_release_input.py timeout."""
    with QemuArm(binary, out_dir, machine='s5-rtos', smp=4, ram='512M',
                 loads=[(program, 0x6280000)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        def call(entry, r0=0, r1=0, thumb=True):
            q.select_cpu(0)
            for reg, value in [('cpsr', 0x1f3 if thumb else 0x1d3),
                               ('sp', 0x25000), ('lr', 0x10010),
                               ('r0', r0), ('r1', r1), ('pc', entry)]:
                q.write_reg(reg, value)
            return q.resume_cpu(0, timeout=3)

        q.select_cpu(0)
        q.write_mem(0x10000, struct.pack('<2I', 0xe1c010b0, 0xe12fff1e))
        q.breakpoint_add(0x10010)
        q.breakpoint_add(0x00000004, kind=4)  # ARM Undefined Instruction vector
        q.write_mem(0x8ab472c, struct.pack('<7I', *[0x7f040000 + i * 0x1000 for i in range(7)]))
        q.write_mem(0x9112628 + 3 * 20 + 12, struct.pack('<I', 8))
        for offset, value in [(0, 0x01c7), (0x10, 2), (4, 0)]:
            call(0x10000, 0x7f043000 + offset, value, thumb=False)
            assert q.read_reg('pc') == 0x10010

        reply = call(0x6753036, 0x044e, 2, thumb=True)
        pc = q.read_reg('pc')
        cpsr = q.read_reg('cpsr')
        lr_und = q.read_reg('lr')
        faulting_pc = lr_und - 4  # ARM state UND sets lr_und = faulting_instr + 4
        faulting_word = struct.unpack('<I', q.read_mem(faulting_pc, 4))[0]
        assert pc == 0x4, f'Expected UND vector 0x4, got {hex(pc)}'
        assert (cpsr & 0x1f) == 0x1b, f'Expected UND mode 0x1b, got {hex(cpsr)}'
        assert faulting_pc == 0x66c2620, f'Expected vpush at 0x66c2620, got {hex(faulting_pc)}'
        assert faulting_word == 0xed2d0b08, f'Expected vpush {{d0-d3}} (0xed2d0b08), got {hex(faulting_word)}'
        return {
            'reproduced': True,
            'stop_reply': reply,
            'vector_pc': hex(pc),
            'cpsr': hex(cpsr),
            'mode': 'UND (0x1b)',
            'lr_und': hex(lr_und),
            'faulting_pc': hex(faulting_pc),
            'faulting_instruction_word': hex(faulting_word),
            'faulting_instruction_mnemonic': 'vpush {d0, d1, d2, d3}',
            'call_chain': '0x06753036 -> 0x064e6a78 -> 0x066c261c (zeroes 0x18-byte SPI stack struct via NEON/VFP)',
            'cause': 'qemu/test_lens_release_input.py runs from cold CPU reset without enabling CPACR.cp10/11 and FPEXC.EN',
            'suggested_patch': str(HERE / 'task-a-test_lens_release_input.patch'),
        }


def run_focused_verification(binary: Path, program: Path, out_dir: Path, prog_sha: str) -> dict:
    """Execute all focused SPI3 0x044e checks through original 0x06752fb0 and 0x06753036."""
    report = {
        'scope': (
            'Original SPI3 register read/write routines (0x06752fb0 / 0x06753036) with explicit '
            'controller + VFP setup; external input hypothesis on register 0x044e bit0 only, '
            'not lens presence or peer ACK'
        ),
        'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'program_sha256': prog_sha,
        'original_instructions_modified': False,
        'guest_ui_or_event_writes': False,
        'setup': [
            'CPACR cp10/cp11=0x00f00000 and FPEXC.EN=0x40000000 (matches menu-smp/boot-state.S)',
            'SPI channel address table restored at 0x08ab472c',
            '8-bit channel descriptor at 0x09112628 + 3*20 + 12',
            'PL022 CR0=01c7, CPSR=2, CR1=0',
        ],
        'original_entry_execution_evidence': {
            '0x06753036_hits': 0,
            '0x06752fb0_hits': 0,
            '0x0642c43c_hits': 0,
            'verified_instruction_words': {},
        },
        'checks': [],
    }
    with QemuArm(binary, out_dir, machine='s5-rtos', smp=4, ram='512M',
                 loads=[(program, 0x6280000)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        path = '/machine/rtc-peer'

        def state():
            return json.loads(q.qmp.command('qom-get', {'path': path, 'property': 'state-json'}))

        def set_enabled(value: bool):
            q.qmp.command('qom-set', {'path': path, 'property': 'lens-release-input-high', 'value': value})

        # Verify unmodified firmware bytes in guest RAM before running.
        report['original_entry_execution_evidence']['verified_instruction_words'] = {
            '0x06752fb0_first8_hex': q.read_mem(0x06752fb0, 8).hex(),
            '0x06753036_first8_hex': q.read_mem(0x06753036, 8).hex(),
            '0x0642c43c_first8_hex': q.read_mem(0x0642c43c, 8).hex(),
        }
        assert report['original_entry_execution_evidence']['verified_instruction_words']['0x06752fb0_first8_hex'] == '7fb5fa4c0d46f849'
        assert report['original_entry_execution_evidence']['verified_instruction_words']['0x06753036_first8_hex'] == '7cb5d84d0024d64a'
        assert report['original_entry_execution_evidence']['verified_instruction_words']['0x0642c43c_first8_hex'] == 'f0b5384f00252c46'

        q.select_cpu(0)
        q.write_mem(0x10000, struct.pack('<2I', 0xe1c010b0, 0xe12fff1e))
        q.write_mem(0x10020, struct.pack(
            '<7I',
            0xee110f50,  # mrc p15, 0, r0, c1, c0, 2
            0xe380060f,  # orr r0, r0, #0x00f00000
            0xee010f50,  # mcr p15, 0, r0, c1, c0, 2
            0xf57ff06f,  # isb
            0xe3a00101,  # mov r0, #0x40000000
            0xeee80a10,  # vmsr fpexc, r0
            0xe12fff1e,  # bx lr
        ))
        q.breakpoint_add(0x10010)
        # Also observe first entry into original 0x06753036, 0x06752fb0, and 0x0642c43c
        # via breakpoints to prove guest execution through the real firmware call chain.
        observed_entries = {0x06753036: '0x06753036_hits',
                            0x06752fb0: '0x06752fb0_hits',
                            0x0642c43c: '0x0642c43c_hits'}
        for bp_addr in observed_entries:
            q.breakpoint_add(bp_addr, kind=2)

        def call(entry, r0=0, r1=0, thumb=True):
            q.select_cpu(0)
            for reg, value in [('cpsr', 0x1f3 if thumb else 0x1d3),
                               ('sp', 0x25000), ('lr', 0x10010),
                               ('r0', r0), ('r1', r1), ('pc', entry)]:
                q.write_reg(reg, value)
            while True:
                q.resume_cpu(0, timeout=3)
                pc = q.read_reg('pc')
                if pc == 0x10010:
                    return q.read_reg('r0')
                if pc in observed_entries:
                    key = observed_entries[pc]
                    report['original_entry_execution_evidence'][key] += 1
                    q.breakpoint_remove(pc, kind=2)
                    q.step_cpu(0, timeout=2)
                    q.breakpoint_add(pc, kind=2)
                    continue
                raise AssertionError(f'Unexpected stop PC: {hex(pc)}')

        call(0x10020, thumb=False)
        q.write_mem(0x8ab472c, struct.pack('<7I', *[0x7f040000 + i * 0x1000 for i in range(7)]))
        q.write_mem(0x9112628 + 3 * 20 + 12, struct.pack('<I', 8))
        for offset, value in [(0, 0x01c7), (0x10, 2), (4, 0)]:
            call(0x10000, 0x7f043000 + offset, value, thumb=False)

        s0 = state()
        assert s0['lens_release_input']['enabled'] is False

        def write(reg, value):
            assert call(0x6753036, reg, value) == 0

        def read(reg):
            q.write_mem(0x100200, bytes([0xa5]) * 16)
            assert call(0x6752fb0, reg, 0x100204) == 0
            data = q.read_mem(0x100200, 16)
            assert data[:4] == bytes([0xa5]) * 4 and data[5:] == bytes([0xa5]) * 11
            return data[4]

        # Step 2 core checks: default disabled, stored 02, enabled effective read 03, disabled restores 02.
        write(0x044e, 2)
        r_default = read(0x044e)
        s_default = state()
        assert r_default == 2
        assert s_default['lens_release_input']['enabled'] is False
        assert s_default['lens_release_input']['stored_value'] == 2
        assert s_default['lens_release_input']['effective_value'] == 2
        assert s_default['registers']['0x044e'] == 2

        set_enabled(True)
        r_enabled = read(0x044e)
        s_enabled = state()
        assert r_enabled == 3
        assert s_enabled['lens_release_input']['enabled'] is True
        assert s_enabled['lens_release_input']['stored_value'] == 2
        assert s_enabled['lens_release_input']['effective_value'] == 3
        assert s_enabled['registers']['0x044e'] == 2
        assert s_enabled['lens_release_input']['stored_value_modified'] is False

        set_enabled(False)
        r_disabled = read(0x044e)
        s_disabled = state()
        assert r_disabled == 2
        assert s_disabled['lens_release_input']['enabled'] is False
        assert s_disabled['lens_release_input']['stored_value'] == 2
        assert s_disabled['lens_release_input']['effective_value'] == 2
        assert s_disabled['registers']['0x044e'] == 2
        report['checks'].append({
            'name': 'default_off_stored02_enabled03_disabled02',
            'passed': True,
            'default_read': r_default,
            'enabled_read': r_enabled,
            'disabled_read': r_disabled,
            'stored_value_throughout': 2,
        })

        # Other bits on 0x044e preserved.
        set_enabled(True)
        bit_cases = []
        for value in [0, 2, 0x80, 0xfe, 0xff]:
            write(0x044e, value)
            got = read(0x044e)
            s = state()
            assert got == (value | 1)
            assert s['registers']['0x044e'] == value
            assert s['lens_release_input']['stored_value'] == value
            assert s['lens_release_input']['effective_value'] == (value | 1)
            bit_cases.append({'written': value, 'stored': s['registers']['0x044e'], 'read_when_enabled': got})
        report['checks'].append({
            'name': 'register_044e_other_bits_and_latch_preserved',
            'passed': True,
            'cases': bit_cases,
        })

        # Other SPI3 registers unaffected even while lens_release_input is enabled.
        other_regs = []
        for reg, value in [(0x0448, 0x10), (0x044a, 0x77), (0x044c, 0x4a), (0x0402, 0x35)]:
            write(reg, value)
            got = read(reg)
            assert got == value
            other_regs.append({'register': f'0x{reg:04x}', 'written': value, 'read': got})
        report['checks'].append({
            'name': 'other_spi3_registers_unaffected',
            'passed': True,
            'cases': other_regs,
        })

        # Restore 0x044e=2, disable, verify unknown read tracking remains intact.
        write(0x044e, 2)
        set_enabled(False)
        assert read(0x044e) == 2
        before = state()
        assert before['unknown_reads'] == before['malformed_frames'] == 0
        assert before['lens_release_input']['affected_reads'] == 6
        assert read(0x0554) == 0
        after = state()
        assert after['unknown_reads'] == 1 and '0x0554' in after['unsupported_registers']
        assert after['lens_release_input']['affected_reads'] == 6
        report['checks'].append({
            'name': 'unknown_register_still_reported_unsupported',
            'passed': True,
            'unknown_reads': after['unknown_reads'],
            'unsupported_registers': after['unsupported_registers'],
            'affected_reads': after['lens_release_input']['affected_reads'],
        })

        # Confirm exact counts of original routine breakpoint hits:
        # writes: 1 (initial 2) + 5 (bit_cases) + 4 (other_regs) + 1 (restore 2) = 11 calls to 0x06753036
        # reads:  3 (default/en/dis) + 5 (bit_cases) + 4 (other_regs) + 1 (restore) + 1 (0x0554) = 14 calls to 0x06752fb0
        # total SPI3 transfers via 0x0642c43c = 11 + 14 = 25
        ev = report['original_entry_execution_evidence']
        assert ev['0x06753036_hits'] == 11, ev
        assert ev['0x06752fb0_hits'] == 14, ev
        assert ev['0x0642c43c_hits'] == 25, ev
        report['final_state'] = after

    report['execution_succeeded'] = True
    report['assertions_passed'] = True
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)

    binary = ROOT / 'toolchain/build/qemu-system-arm'
    program = ROOT.parent / 's5-v29-offline/unpacked-v29/03_program.bin'
    prog_sha = hashlib.sha256(program.read_bytes()).hexdigest()
    assert prog_sha == EXPECTED_PROGRAM_SHA256

    defect = diagnose_unmodified_defect(binary, program, out / 'unmodified-defect-diagnosis')
    focused = run_focused_verification(binary, program, out / 'vm', prog_sha)

    combined = {
        'task': 'Task A Step 1-2 Focused SPI3 0x044e Input Verification',
        'unmodified_test_script': str(ROOT / 'qemu/test_lens_release_input.py'),
        'unmodified_test_execution_succeeded': False,
        'unmodified_test_defect_diagnosis': defect,
        'focused_with_vfp_enable_execution_succeeded': focused['execution_succeeded'],
        'focused_with_vfp_enable_assertions_passed': focused['assertions_passed'],
        'lens_attachment_claimed': False,
        'focused_report': focused,
    }
    (out / 'verification.json').write_text(json.dumps(combined, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({
        'unmodified_defect_reproduced': defect['reproduced'],
        'faulting_pc': defect['faulting_pc'],
        'focused_execution_succeeded': focused['execution_succeeded'],
        'focused_assertions_passed': focused['assertions_passed'],
        '0x06753036_hits': focused['original_entry_execution_evidence']['0x06753036_hits'],
        '0x06752fb0_hits': focused['original_entry_execution_evidence']['0x06752fb0_hits'],
        '0x0642c43c_hits': focused['original_entry_execution_evidence']['0x0642c43c_hits'],
        'output': str(out),
    }, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
