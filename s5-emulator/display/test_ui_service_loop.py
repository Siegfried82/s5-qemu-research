#!/usr/bin/env python3
"""Host contract checks only; these are not firmware/menu execution evidence."""
import importlib.util
from pathlib import Path
import struct
import sys
import unittest
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load('service_loop', ROOT/'menu-smp/ui_service_loop.py')
fixtures = load('service_fixture', ROOT/'serial/test_ui_service_probe.py')


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.q = fixtures.FakeQemu()
        self.q.seed(m.LOOP_ENTRY, m.LOOP_BYTES)
        self.q.seed(m.POSTINIT_ENTRY, m.POSTINIT_BYTES)
        self.p = m.UIServiceLoopProbe(self.q)

    def at(self, pc, **regs):
        self.q.regs.update(r15=pc, **regs)
        return self.p.handle(pc, self.q.selected_cpu)

    def enter(self):
        self.p.start(0)
        self.q.regs['r5'] = 0x06f49a28  # Represents original LDR, not helper behavior.
        self.at(m.ITERATION)

    def receive_and_dispatch(self, result=1, words=(0x115,0x11d,0x35,1,0)):
        self.q.seed(m.MESSAGE_BUFFER, struct.pack('<5I', *words))
        self.at(m.RECEIVE_RETURN, r0=0)
        self.at(m.DISPATCH_CALL, r0=m.MESSAGE_BUFFER, r4=m.MESSAGE_BUFFER)
        self.at(m.DISPATCH_RETURN, r0=result)

    def test_start_has_only_three_register_writes_and_no_memory_writes(self):
        old = self.q.registers()
        self.assertFalse(self.q.writes)
        self.enter()
        self.assertEqual(self.q.regs['r13'], (old['r13'] & ~7)-16)
        self.assertEqual(self.q.writes, [('reg','r13',(old['r13'] & ~7)-16),
                                        ('reg','r6',0xffffffff),('reg','r15',m.LOOP_ENTRY)])
        self.p.close()
        self.assertEqual(len(self.q.writes), 3)
        self.assertFalse(self.q.breakpoints)
        self.assertFalse(self.p.active)

    def test_empty_queue_and_static_envelope_are_allowed(self):
        for count in (0,1):
            self.setUp()
            self.q.word(self.q.queue+0x18, count)
            self.q.word(self.q.envelope-4, 0)
            self.enter()
            self.assertEqual(self.p.report['status'], 'original_receive_pending')
            self.assertEqual(self.p.report['messages_received'], 0)

    def test_limit_stops_after_fallback_without_receiving_next(self):
        self.p = m.UIServiceLoopProbe(self.q, max_messages=1)
        self.enter(); self.receive_and_dispatch(result=0)
        self.at(m.FALLBACK_CALL)
        self.at(m.FALLBACK_RETURN, r0=0)
        with self.assertRaises(m.UIServiceLoopProbeError):
            self.at(m.ITERATION)
        self.assertEqual(self.p.report['status'], 'message_limit_reached')
        self.assertEqual(self.p.report['messages_completed'], 1)
        self.assertEqual(self.p.report['messages_received'], 1)
        self.assertEqual(self.q.regs['r15'], m.ITERATION)
        self.assertEqual(self.p.report['messages'][0]['fallback_send_return'], '0x0')

    def test_limit_all_64_original_returns(self):
        self.enter()
        for number in range(64):
            self.receive_and_dispatch(words=(0x7f,0x4a,0x40001,0x08aab920,1))
            if number == 63:
                with self.assertRaises(m.UIServiceLoopProbeError): self.at(m.ITERATION)
            else:
                self.at(m.ITERATION)
        self.assertEqual(self.p.report['messages_completed'], 64)
        self.assertEqual(self.p.report['messages_received'], 64)
        self.assertFalse(any(w[0]=='mem' for w in self.q.writes))

    def test_original_task_may_migrate(self):
        self.enter()
        self.q.selected_cpu = 2
        self.q.word(0x20048, self.q.tcb)
        self.receive_and_dispatch()
        self.at(m.ITERATION)
        self.assertEqual(self.p.report['messages_completed'], 1)

    def test_postinit_requires_actual_observed_returns(self):
        self.p = m.UIServiceLoopProbe(self.q, run_postinit=True)
        self.p.start(0)
        self.assertEqual(self.q.regs['r15'], m.POSTINIT_ENTRY)
        self.assertFalse(self.p.report['postinit_672060c_executed_by_helper'])
        self.at(m.POSTINIT_ENTRY)
        self.at(m.POSTINIT_SECOND, r0=0)
        self.assertTrue(self.p.report['postinit_672060c_executed_by_helper'])
        self.assertFalse(self.p.report['postinit_65b9fcc_executed_by_helper'])
        self.at(m.LOOP_ENTRY)
        self.assertTrue(self.p.report['postinit_65b9fcc_executed_by_helper'])
        self.q.regs['r5'] = 0x06f49a28
        self.at(m.ITERATION)
        self.assertFalse(any(w[0]=='mem' for w in self.q.writes))

    def test_guards_and_liveframe_never_repair_memory(self):
        self.q.seed(m.LOOP_ENTRY, b'XX')
        with self.assertRaises(m.UIServiceLoopProbeError): self.p.start(0)
        self.assertEqual(self.q.writes, [])
        self.setUp(); self.enter()
        self.q.word(self.p._context['live_start'], 0x12345678)
        with self.assertRaises(m.UIServiceLoopProbeError): self.at(m.RECEIVE_RETURN, r0=0)
        self.p.close()
        self.assertEqual(len(self.q.writes), 3)


if __name__ == '__main__':
    unittest.main()
