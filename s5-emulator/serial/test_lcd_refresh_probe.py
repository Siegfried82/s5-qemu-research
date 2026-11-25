#!/usr/bin/env python3
"""Host lifecycle checks only; no claim of actual firmware or LCD execution."""
import importlib.util
from pathlib import Path
import struct
import time
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load('lcd_refresh_probe', HERE.parent/'menu-smp/lcd_refresh_probe.py')
f = load('clock_helpers', HERE/'test_display_clock_probe.py')


class FakeQemu(f.FakeQemu):
    event, queue, task = 0x08e18b70, 0x08e1c000, 0x08e02000
    stack = 0x0a002000

    def __init__(self):
        super().__init__()
        self.seed(m.REQUEST, bytes.fromhex('45e7'))
        self.seed(m.CALLBACK, bytes.fromhex('7cb5'))
        self.word(0x06f498d0, 0x86)
        self.word(0x08e18d80+(0x86-1)*4, self.event)
        self.word(0x08e1b94c+(15-1)*4, self.queue)
        self.word(0x08a9d610, m.CALLBACK|1)
        self.word(0x08ab488c, 4)
        self.word(0x08a9c7f8, 2)
        self.word(self.event+8, 0)
        self.seed(self.task, b'DRHT')
        for offset, value in ((0xc0,18),(0xd8,0x0647e83f),(0xc,self.stack-0x1000),(0x10,self.stack)):
            self.word(self.task+offset, value)
        self.word(0x20048, self.task)
        self.seed(0x0b392e78, struct.pack('<5I',0,0x10e,0x11f438,0,0))
        self.seed(0x065dd092, bytes.fromhex('9847'))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.q = FakeQemu()
        self.p = m.LCDRefreshProbe(self.q, max_pulses=2)

    def at(self, pc, cpu=0, **regs):
        self.q.selected_cpu = cpu
        self.q.regs.update(r15=pc, **regs)
        return self.p.handle(pc, cpu)

    def request(self):
        self.q.word(self.q.event+8, 1)  # Represents original firmware's event set.
        self.at(m.REQUEST, cpu=2, r0=0, cpsr=0x133)

    def finish(self):
        self.at(m.ISR, r14=0x065dd095, r13=0x08ff0000, cpsr=0x132)
        self.at(0x065dd094)
        self.at(m.CALLBACK, cpu=2, r13=self.q.stack, cpsr=0x133)
        self.at(m.SEND_RETURN, cpu=2, r13=self.q.stack-24, r0=0)
        self.q.word(self.q.event+8, 0)  # Represents original callback clearing it.
        self.at(m.CALLBACK_RETURN, cpu=2)

    def test_default_close_has_no_io(self):
        self.p.close()
        self.assertEqual(self.q.qmp.calls, [])
        self.assertEqual(self.q.writes, [])

    def test_passive_request_wait_has_no_test_input_or_deadline(self):
        self.p.start()
        self.assertTrue(self.p.armed)
        self.assertFalse(self.p.active)
        self.assertEqual(self.p.breakpoints, {m.REQUEST})
        self.assertEqual(self.q.qmp.writes, [])
        self.p.check_timeout()
        self.p.close()
        self.assertEqual(self.q.qmp.writes, [])
        self.assertFalse(self.q.breakpoints)

    def test_two_real_request_observations_required_and_inputs_restored(self):
        self.q.qmp.mask = 2
        self.p.start()
        self.request(); self.finish()
        self.assertFalse(self.p.active)
        self.assertTrue(self.p.armed)
        self.assertEqual(self.p.report['completed_requests'], 1)
        self.assertFalse(self.q.qmp.enabled)
        self.assertEqual(self.q.qmp.mask, 2)
        self.request(); self.finish()
        self.assertTrue(self.p.completed)
        self.assertFalse(self.p.armed)
        self.assertEqual(self.p.report['completed_requests'], 2)
        self.assertEqual(self.q.qmp.counts, {'196':0,'230':0,'232':2})
        self.assertFalse(self.q.breakpoints)
        self.assertEqual(self.q.writes, [])

    def test_existing_original_request_can_start_immediately(self):
        self.q.word(self.q.event+8, 1)
        self.p.start()
        self.assertTrue(self.p.active)
        self.finish()
        self.assertEqual(self.p.report['completed_requests'], 1)

    def test_refuses_missing_event_without_manufacturing_one(self):
        self.p.start()
        with self.assertRaises(m.LCDRefreshProbeError):
            self.at(m.REQUEST, r0=0)
        self.assertEqual(self.q.qmp.writes, [])
        self.assertEqual(self.q.writes, [])

    def test_original_queue_send_failure_is_not_success(self):
        self.p.start(); self.request()
        self.at(m.CALLBACK, cpu=2, r13=self.q.stack, cpsr=0x133)
        with self.assertRaises(m.LCDRefreshProbeError):
            self.at(m.SEND_RETURN, cpu=2, r13=self.q.stack-24, r0=0xffffffff)
        self.assertFalse(self.q.qmp.enabled)
        self.assertFalse(self.q.breakpoints)
        self.assertEqual(self.p.report['completed_requests'], 0)

    def test_timeout_disables_inputs_and_removes_observers(self):
        self.p.start(); self.request()
        self.p._deadline = time.monotonic()-1
        with self.assertRaises(m.LCDRefreshProbeError):
            self.p.check_timeout()
        self.assertFalse(self.q.qmp.enabled)
        self.assertFalse(self.q.breakpoints)
        self.assertEqual(self.q.writes, [])

    def test_no_extra_pulse_when_another_diagnostic_owns_inputs(self):
        self.p.start()
        self.q.qmp.enabled = True
        with self.assertRaises(m.LCDRefreshProbeError): self.request()
        self.assertTrue(self.q.qmp.enabled)
        self.assertEqual(self.q.qmp.writes, [])

    def test_unexpected_duplicate_edge_fails_closed(self):
        self.p.start()
        self.q.qmp.double_pulse = True
        with self.assertRaises(m.LCDRefreshProbeError): self.request()
        self.assertFalse(self.q.qmp.enabled)
        self.assertFalse(self.q.breakpoints)


if __name__ == '__main__':
    unittest.main()
