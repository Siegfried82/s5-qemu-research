#!/usr/bin/env python3
"""Run the existing original-firmware runner with non-executing IRQ inspection.

No wakeup, register mutation, or acknowledgement-register read is performed
by the final inspection. QEMU interrupt logging records actual exceptions.
"""
import importlib.util
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('s5_menu_irq_probe', ROOT / 'menu-smp/boot.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)
OriginalQemuArm = boot.QemuArm


class InterruptLoggingQemu(OriginalQemuArm):
    def __init__(self, *args, **kwargs):
        kwargs['log_mask'] = kwargs.get('log_mask', 'unimp,guest_errors') + ',int'
        super().__init__(*args, **kwargs)

    def __enter__(self):
        emu = super().__enter__()
        # Preserve the previous all-zero-input baseline for this diagnosis.
        emu.qmp.command('qom-set', {'path': '/machine',
                                   'property': 'inactive-inputs', 'value': False})
        return emu


def inspect(emu):
    data = {'cpu_registers': [], 'system_registers': [], 'physical_words': {},
            'inspection_modifies_guest_registers': False,
            'interrupt_acknowledgement_read': False}
    feature = ET.fromstring(emu.rsp.read_feature('system-registers.xml'))
    registers = [r for r in feature.iter('reg') if r.get('name') in
                 ('VBAR', 'VBAR_S', 'VBAR_NS', 'SCTLR', 'SCTLR_S', 'SCTLR_NS',
                  'CPSR', 'TTBR0', 'TTBR0_S', 'TTBR0_NS')]
    for index in range(4):
        emu.select_cpu(index)
        data['cpu_registers'].append(emu.registers())
        current = {}
        for reg in registers:
            raw = emu.rsp.request('p%x' % int(reg.get('regnum'), 0))
            current[reg.get('name')] = hex(int.from_bytes(bytes.fromhex(raw), 'little'))
        data['system_registers'].append(current)
    emu.select_cpu(0)
    # Never read banked GIC via HMP xp: stock QEMU dereferences current_cpu,
    # which is null in the monitor. The board's QOM getter reads state safely.
    data['gic'] = json.loads(emu.qmp.command('qom-get', {
        'path': '/machine', 'property': 'gic-state-json'}))
    ranges = [(0x7f020000, 7), (0x7f020020, 7)]
    for address, count in ranges:
        data['physical_words'][hex(address)] = emu.qmp.command(
            'human-monitor-command', {'command-line': 'xp /%dwx 0x%x' % (count, address)})
    (emu.output_dir.parent / 'irq-inspection.json').write_text(json.dumps(data, indent=2) + '\n')
    return data


boot.QemuArm = InterruptLoggingQemu
boot.sample_timer_registers = inspect
if __name__ == '__main__':
    boot.main()
