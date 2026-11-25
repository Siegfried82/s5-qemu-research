#!/usr/bin/env python3
"""Host-only lifecycle checks for passive observation of original queue work."""
import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[1]/'serial/test_lcd_refresh_probe.py'
spec=importlib.util.spec_from_file_location('lcd_base_tests',path)
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
m=base.m


class DeferTests(unittest.TestCase):
    def setUp(self):
        self.q=base.FakeQemu();self.p=m.LCDRefreshProbe(self.q,max_pulses=2)
        self.q.seed(m.DRAIN,bytes.fromhex('6946'))
        self.q.word(self.q.event+8,1);self.q.word(self.q.queue+0x18,6)
        self.q.seed(0x08a9d606,(2).to_bytes(2,'little'))
    def at_drain(self):
        self.q.selected_cpu=2
        self.q.regs.update(r15=m.DRAIN,r13=self.q.stack,cpsr=0x133)
        return self.p.handle(m.DRAIN,2)
    def empty_queue(self):
        self.q.word(self.q.queue+0x18,0);self.q.seed(0x08a9d606,bytes(2))
    def test_pending_work_is_passive_and_never_cleared_by_host(self):
        self.p.start()
        self.assertFalse(self.p.active);self.assertTrue(self.p.armed)
        self.assertEqual(self.p.breakpoints,{m.DRAIN})
        self.assertEqual(self.q.qmp.writes,[]);self.assertEqual(self.q.writes,[])
        self.p.close();self.assertEqual(self.q.qmp.writes,[])
    def test_natural_event_clear_needs_no_test_pulse(self):
        self.p.start();self.q.word(self.q.event+8,0)
        self.at_drain()
        self.assertFalse(self.p.active);self.assertEqual(self.p.breakpoints,{m.REQUEST})
        self.assertEqual(self.q.qmp.writes,[]);self.assertEqual(self.q.writes,[])
        self.assertEqual(self.p.report['completed_requests'],0)
    def test_only_empty_queue_with_still_pending_event_can_pulse(self):
        self.p.start();self.empty_queue();self.at_drain()
        self.assertTrue(self.p.active);self.assertNotIn(m.DRAIN,self.p.breakpoints)
        self.assertEqual(self.q.qmp.counts['232'],1);self.assertEqual(self.q.writes,[])
        self.p.close()
    def test_request_boundary_also_defers_pending_work(self):
        self.q.word(self.q.event+8,0);self.p.start()
        self.q.word(self.q.event+8,1)
        self.q.selected_cpu=2;self.q.regs.update(r15=m.REQUEST,r0=0,cpsr=0x133)
        self.p.handle(m.REQUEST,2)
        self.assertFalse(self.p.active);self.assertIn(m.DRAIN,self.p.breakpoints)
        self.assertNotIn(m.REQUEST,self.p.breakpoints)
        self.assertEqual(self.q.qmp.writes,[])

    def test_repeated_receive_boundary_executes_original_mov_without_test_input(self):
        self.p.start();self.at_drain()
        self.assertEqual(self.q.regs['r15'],m.DRAIN+2)
        self.assertEqual(self.p.breakpoints,{m.DRAIN})
        self.assertFalse(self.p.active);self.assertEqual(self.q.qmp.writes,[])
        self.assertEqual(self.q.writes,[])

if __name__=='__main__':unittest.main()
