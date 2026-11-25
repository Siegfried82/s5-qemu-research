#!/usr/bin/env python3
"""Observe original dispatcher IRQ IDs without modifying interrupt state."""
import importlib.util
import json
from pathlib import Path
import re
import struct
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('s5_timer_irq_probe', ROOT / 'menu-smp/boot.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)
OriginalQemuArm = boot.QemuArm
DISPATCH = 0x065dd080
boot.POINTS.pop(DISPATCH, None)


class IRQObservingQemu(OriginalQemuArm):
    def __enter__(self):
        result = super().__enter__()
        self.irq_events = []
        self.breakpoint_add(DISPATCH)
        return result

    def resume_all(self, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            reply = super().resume_all(max(0.05, deadline - time.monotonic()))
            match = re.search(r'thread:([^;]+)', reply)
            ids = self.threads()
            index = ids.index(match.group(1)) if match and match.group(1) in ids else 0
            self.select_cpu(index)
            if self.read_reg('pc') != DISPATCH:
                return reply
            regs = self.registers()
            irq = regs['r0']
            table = struct.unpack('<I', self.read_mem(0x065dd0f8, 4))[0]
            handler = struct.unpack('<I', self.read_mem(table + irq * 8 + 4, 4))[0] if irq < 448 else None
            self.irq_events.append({'cpu': index, 'irq': irq, 'handler': hex(handler or 0),
                                    'cpsr': hex(regs['cpsr']), 'lr': hex(regs['r14'])})
            (self.output_dir.parent / 'irq-dispatches.json').write_text(json.dumps(self.irq_events, indent=2) + '\n')
            self.breakpoint_remove(DISPATCH)
            self.step(timeout=1)
            if len(self.irq_events) < 32:
                self.breakpoint_add(DISPATCH)


def inspect_timers(emu):
    result = {}
    for timer in (0, 1, 2, 6):
        address = 0x7f020000 + (timer // 2) * 0x1000 + (timer % 2) * 0x20
        result[hex(address)] = emu.qmp.command('human-monitor-command',
            {'command-line': 'xp /7wx 0x%x' % address})
    return result


boot.QemuArm = IRQObservingQemu
boot.sample_timer_registers = inspect_timers
if __name__ == '__main__':
    boot.main()
