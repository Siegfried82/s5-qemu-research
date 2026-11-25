#!/usr/bin/env python3
"""Read original firmware and the ADC-environment snapshot; never inject state."""
from pathlib import Path
import hashlib
import json
import mmap
import struct
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = 0x06280000
source = ROOT / 's5-v29-offline/unpacked-v29/03_program.bin'
program = source.read_bytes()
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
folder = ROOT / 's5-emulator/menu-smp/results/adc-environment-boot'
boot = json.loads((folder / 'result.json').read_text())
with (folder / 'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda address: struct.unpack_from('<I', ram, address)[0]
    tcb = 0x08dfe890
    sp = u32(tcb + 8)
    end = u32(tcb + 16)
    counters = {
        'timer0_callback_modulo10': 0x08aaa5f4,
        'software_timer_ticks': 0x08aaa7b4,
        'os_ticks': 0x08ab6394,
        'clock64_low': 0x08ab18f8,
        'clock64_high': 0x08ab18fc,
    }
    result = {
        'scope': 'Original firmware static analysis and saved QEMU state; no event injection or instruction patch',
        'original_program': str(source), 'program_sha256': sha,
        'snapshot': str(folder / 'ram.bin'), 'snapshot_base': '0x0',
        'main_task': {
            'tcb': hex(tcb), 'name': 'uiTSK001', 'entry': hex(u32(tcb + 0xd8)),
            'run_count': u32(tcb + 4), 'state': u32(tcb + 0x30),
            'wait_type': u32(tcb + 0x100), 'timeout_ticks': u32(tcb + 0x4c),
            'saved_sp': hex(sp), 'wait_object': hex(u32(tcb + 0x108)),
            'stack_words': {hex(a): hex(u32(a)) for a in range(sp, end + 1, 4)},
        },
        'verified_call_chain': [
            {'call': '0x063c8b76', 'target': '0x0647e822', 'saved_return': '0x063c8b7b'},
            {'call': '0x0647e832', 'target': '0x0647c4a2', 'saved_return': '0x0647e837'},
            {'call': '0x0647c4ac', 'target': '0x064a070e', 'saved_return': '0x0647c4b1'},
            {'call': '0x064a0748', 'target': '0x066c23e8', 'r0': 1, 'saved_return': '0x064a074d'},
            {'call': '0x066c2466', 'target': '0x06498810', 'saved_return': '0x066c246b'},
        ],
        'delay': {
            'entry': '0x066c23e8', 'argument': 1,
            'behavior': 'Sets TCB wait_type=2 and timeout=argument+1, then reschedules',
            'caller_behavior': '0x064a070e writes GPIO 0x7f081c14 = 0x10000000 then 0x10000001, then requests one tick delay',
            'not_rtc_second_poll': True,
        },
        'tick_counters': {name: {'address': hex(a), 'value': u32(a)} for name, a in counters.items()},
        'timer0': {
            'mmio': '0x7f020000', 'gic_id': 36,
            'original_setup': '0x064851ee', 'load': 0x6978,
            'qemu_clock_hz': 27000000, 'expected_period_seconds': 0.001,
            'control_after_setup': '0xf2',
            'original_driver_control_or': '0x06554c7c ORR r2,r2,#0x12; bit4 is reserved in standard SP804',
            'original_irq_configuration': 'loader3 table sets edge trigger and CPU0 target for ID36',
            'handler_table': '0x09119760', 'handler': hex(u32(0x09119760 + 36 * 8 + 4)),
            'dispatch': '0x064969f4 sets channel0, tails to 0x06554da0',
            'callback_table': '0x091126b4', 'callback': hex(u32(0x091126b4)),
            'callback_behavior': '0x06485178 updates callback/software-clock counters and invokes 0x068e805c OS tick',
            'acknowledgement': 'IRQ trampoline 0x068f9a10 performs GIC IAR/EOIR. No timer INTCLR in the direct callback chain or dispatch. Driver writes INTCLR only in timer start/stop paths examined.',
        },
        'gic': boot['gic'],
        'inference': {
            'confidence': 'Strong evidence for missing repeated timer edges; physical bit4 semantics not independently verified',
            'basis': 'Four tick counters each equal1, timer0 ID36 level asserted but pending0 and active0, CPUs idle, task retains2 tick timeout',
            'candidate': 'SoC timer pulse/automatic-clear extension, possibly associated with Control bit4, which original driver always enables',
            'required_test': 'Explicit inferred timer profile, preserve original GIC edge configuration, verify original callback repeats and original delay returns',
        },
        'menu_rendered': False,
    }
    assert result['main_task']['wait_type'] == 2
    assert result['main_task']['timeout_ticks'] == 2
    assert all(result['tick_counters'][n]['value'] == 1 for n in counters if n != 'clock64_high')
    (HERE / 'tick-wait-evidence.json').write_text(json.dumps(result, indent=2) + '\n')

lines = [f'Original 03_program.bin sha256={sha}; literal pools can decode as instructions.']
for address, length, thumb in [
    (0x063c8b20, 0x9e, True), (0x0647e822, 0x1c, True),
    (0x0647c4a2, 0x16, True), (0x064a070e, 0x46, True),
    (0x066c23e8, 0xb0, True), (0x06485178, 0x24, True),
    (0x064851ee, 0x4c, True), (0x06554c20, 0x82, True),
    (0x06554cde, 0x94, True), (0x06554da0, 0x12, True),
    (0x064969f4, 0xc, True), (0x065dd080, 0x1c, True),
    (0x068e8060, 0xbc, False), (0x068f9a10, 0x70, False),
]:
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB if thumb else capstone.CS_MODE_ARM)
    md.detail = True
    lines.append(f'\nRANGE {address:08x} {"Thumb" if thumb else "ARM"}')
    for ins in md.disasm(program[address - BASE:address - BASE + length], address):
        suffix = ''
        if ins.mnemonic.startswith('ldr') and len(ins.operands) > 1:
            operand = ins.operands[1]
            if operand.type == capstone.arm.ARM_OP_MEM and operand.mem.base == capstone.arm.ARM_REG_PC:
                at = ((ins.address + (4 if thumb else 8)) & ~3) + operand.mem.disp
                suffix = f'; literal[{at:08x}]={struct.unpack_from("<I", program, at - BASE)[0]:08x}'
        lines.append(f'{ins.address:08x}: {ins.bytes.hex():10} {ins.mnemonic:9} {ins.op_str} {suffix}')
(HERE / 'tick-wait.disasm.txt').write_text('\n'.join(lines) + '\n')
print('Wrote tick-wait-evidence.json and tick-wait.disasm.txt; snapshot assertions passed.')
