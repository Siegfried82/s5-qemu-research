#!/usr/bin/env python3
"""Host contract checks, not firmware execution or evidence of a menu frame."""
import importlib.util
import struct
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

module=load('ui_service_probe',HERE.parent/'menu-smp/ui_service_probe.py')
helpers=load('key_contract_helpers',HERE/'test_key_probe.py')


class FakeQemu(helpers.FakeQemu):
    def __init__(self,alignment=4):
        super().__init__()
        self.selected_cpu=0
        self.regs.update(r13=0x09acef00+alignment,r15=module.PEER_WAIT,cpsr=0x60000133)
        self.tcb,self.queue,self.array,self.envelope=0x08e03ad4,0x08e1a08c,0x09aec448,0x099cab3c
        self.word(0x20040,self.tcb)
        self.seed(self.tcb,b'DRHT')
        for offset,value in ((0xc,0x09ac0000),(0x10,0x09acf03f),(0xc0,66),
                             (0xd8,module.TASK_ENTRY),(0xec,2)):
            self.word(self.tcb+offset,value)
        for address,value in ((0x06f49a28,0x76),(0x06f492b0,0x11d),
                              (0x06f49c64,0x115),(0x06f48f88,0x42),
                              (module.QUEUE_TABLE+0x75*4,self.queue),
                              (self.queue+4,56),(self.queue+0xc,self.array),
                              (self.queue+0x10,1),(self.queue+0x18,1),
                              (self.array,self.envelope),(self.envelope-4,0x08e1c2cc),
                              (0x08e1e1a4+5*4,0x08e1c2cc)):
            self.word(address,value)
        self.seed(self.envelope,struct.pack('<5I',*module.EXPECTED))

    def word(self,address,value):
        self.seed(address,struct.pack('<I',value))


class UIServiceProbeTests(unittest.TestCase):
    def setUp(self):
        self.emu=FakeQemu()
        self.probe=module.UIServiceProbe(self.emu)

    def at(self,pc,**regs):
        self.emu.regs.update(r15=pc,**regs)
        return self.probe.handle(pc,0)

    def receive(self):
        self.probe.start(0)
        callsp=self.probe._saved['sp']
        self.at(module.POOL_RELEASE_CALL,r0=6,r1=self.emu.envelope,r13=callsp-24)
        self.at(module.POOL_RELEASE_RETURN,r0=0)
        self.emu.seed(module.MESSAGE_BUFFER,struct.pack('<5I',*self.probe.expected))
        self.emu.word(self.emu.queue+0x18,0)
        self.at(self.probe.return_address,r0=0,r13=callsp)
        return callsp

    def ack(self,retval=0):
        self.at(module.MODE_HANDLER,r0=module.MESSAGE_BUFFER)
        self.at(module.ACK_ENTRY,r0=0x36,r1=1,r2=0)
        self.emu.seed(0x08e9f518,struct.pack('<5I',*module.ACK_EXPECTED))
        self.at(module.ACK_SEND_RETURN,r0=retval,r4=0x08e9f518)

    def mode35(self):
        self.probe=module.UIServiceProbe(self.emu,command=0x35)
        self.emu.seed(self.emu.envelope,struct.pack('<5I',*self.probe.expected))
        self.emu.word(0x08aa0e10,0xff)
        self.emu.seed(0x08ab09e0,b'\x01\x01')
        self.emu.word(0x06fb4034+12,0x0679f7e1)
        self.emu.word(0x06fb4078+0x48,0x067a0fd1)
        for address,child in ((0x09003cf8,0x09047c50),(0x09003d8c,0x09047e28)):
            self.emu.word(address,0x06fb4034)
            self.emu.word(address+8,child)
            self.emu.word(child,0x06fb4078)

    def test_construction_does_not_change_guest(self):
        self.assertFalse(self.emu.writes)
        self.assertFalse(self.emu.breakpoints)
        self.assertFalse(self.probe.active)

    def test_both_stack_alignments_and_complete_restore(self):
        for alignment in (0,4):
            self.setUp();self.emu.regs['r13']=0x09acef00+alignment
            original=self.emu.registers();fp=dict(self.emu.rsp.fp)
            callsp=self.receive();self.assertEqual(callsp&7,0)
            self.ack();self.emu.rsp.fp[26]=b'changed!'
            self.at(self.probe.return_address,r0=1,r13=callsp)
            self.assertTrue(self.probe.completed)
            self.assertFalse(self.probe.active)
            self.assertEqual(self.emu.regs,original)
            self.assertEqual(self.emu.rsp.fp,fp)
            self.assertNotIn(75,self.emu.rsp.register_writes)
            self.assertFalse(any(x[0]=='mem'for x in self.emu.writes))
            self.assertEqual(self.emu.read_mem(module.MESSAGE_BUFFER,20),struct.pack('<5I',*module.EXPECTED))

    def test_wrong_or_empty_queue_head_rejected_without_writes(self):
        for change in ('empty','message','waiting_sender','pool_prefix','read_index'):
            self.setUp()
            if change=='empty':self.emu.word(self.emu.queue+0x18,0)
            if change=='message':self.emu.word(self.emu.envelope+12,0)
            if change=='waiting_sender':self.emu.word(self.emu.queue+0x20,0x08e10000)
            if change=='pool_prefix':self.emu.word(self.emu.envelope-4,0)
            if change=='read_index':self.emu.word(self.emu.queue+0x14,56)
            with self.assertRaises(module.UIServiceProbeError):self.probe.start(0)
            self.assertFalse(self.emu.writes)

    def test_dynamic_nonzero_read_index(self):
        self.emu.word(self.emu.queue+0x14,3)
        self.emu.word(self.emu.array,0)
        self.emu.word(self.emu.array+12,self.emu.envelope)
        self.probe.start(0)
        self.assertEqual(self.probe.report['queue_before']['slot'],hex(self.emu.array+12))

    def test_context_and_route_guards(self):
        for address,value in [(module.GUARDS[0],1),(0x209c0,1),(0x06f49c64,0x114),
                              (self.emu.tcb+0xc0,65),(self.emu.tcb+0xec,3)]:
            self.setUp();self.emu.word(address,value)
            with self.assertRaises(module.UIServiceProbeError):self.probe.start(0)
            self.assertFalse(self.emu.writes)
        for cpsr in (0x600001b3,0x60000533,0x60000132):
            self.setUp();self.emu.regs['cpsr']=cpsr
            with self.assertRaises(module.UIServiceProbeError):self.probe.start(0)
            self.assertFalse(self.emu.writes)

    def test_receive_error_restores_only_known_return(self):
        original=self.emu.registers();self.probe.start(0)
        with self.assertRaises(module.UIServiceProbeError):
            self.at(self.probe.return_address,r0=0xfffffffe,r13=self.probe._saved['sp'])
        self.assertEqual(self.emu.regs,original)
        self.assertFalse(self.probe.completed)
        self.assertEqual(self.probe.report['status'],'original_receive_rejected')

    def test_wrong_return_task_never_restores(self):
        self.probe.start(0);self.emu.word(0x20040,0x08e14000)
        with self.assertRaises(module.UIServiceProbeError):self.at(self.probe.return_address,r0=0)
        self.assertFalse(self.emu.full_register_restored)

    def test_failed_original_pool_release_is_not_ignored(self):
        self.probe.start(0)
        self.at(module.POOL_RELEASE_CALL,r0=6,r1=self.emu.envelope)
        with self.assertRaises(module.UIServiceProbeError):
            self.at(module.POOL_RELEASE_RETURN,r0=0xffffffff)
        self.assertFalse(self.emu.full_register_restored)
        self.assertFalse(self.probe.completed)

    def test_dispatch_handled_without_ack_is_not_success(self):
        callsp=self.receive()
        with self.assertRaises(module.UIServiceProbeError):self.at(self.probe.return_address,r0=1,r13=callsp)
        self.assertTrue(self.emu.full_register_restored)
        self.assertFalse(self.probe.completed)

    def test_ack_failure_stops_nested_call_without_forced_restore(self):
        self.receive()
        with self.assertRaises(module.UIServiceProbeError):self.ack(0xffffffff)
        self.assertFalse(self.emu.full_register_restored)
        self.assertTrue(self.probe.active)
        self.assertFalse(self.probe.completed)

    def test_modified_live_frame_is_not_repaired(self):
        self.probe.start(0);callsp=self.probe._saved['sp']
        self.emu.word(callsp,0x123)
        with self.assertRaises(module.UIServiceProbeError):self.at(self.probe.return_address,r0=0,r13=callsp)
        self.assertFalse(self.emu.full_register_restored)
        self.assertFalse(any(x[0]=='mem'for x in self.emu.writes))

    def test_deadline_close_preserves_failure(self):
        self.probe.start(0);self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.UIServiceProbeError):self.probe.check_timeout()
        self.probe.close()
        self.assertEqual(self.probe.report['status'],'timeout_call_incomplete')
        self.assertTrue(self.probe.report['unsafe_to_force_context_restore'])
        self.assertFalse(self.emu.breakpoints)

    def test_explicit35_returns_with_ack_pending_without_claiming_mode_done(self):
        self.mode35();original=self.emu.registers();callsp=self.receive()
        self.at(module.MODE35_HANDLER,r0=module.MESSAGE_BUFFER)
        self.at(module.MODE35_START,r0=0xff)
        self.at(module.MODE35_NEXT,r0=0)
        self.at(self.probe.return_address,r0=1,r13=callsp)
        self.assertEqual(self.emu.regs,original)
        self.assertTrue(self.probe.completed)
        self.assertEqual(self.probe.report['status'],'handler_returned_ack_pending')
        self.assertFalse(self.probe.report['original_ack_enqueued'])
        self.assertFalse(self.probe.report['asynchronous_mode_completion_verified'])
        self.assertFalse(any(x[0]=='mem'for x in self.emu.writes))

    def test35_requires_actual_previous_state_and_ready_objects(self):
        for address,value in ((0x08aa0e10,1),(0x08ab09e0,0),(0x08a9da70,1),
                              (0x09003cf8,0),(0x09003d8c+8,0),
                              (0x06fb4078+0x48,0)):
            self.setUp();self.mode35();self.emu.word(address,value)
            with self.assertRaises(module.UIServiceProbeError):self.probe.start(0)
            self.assertFalse(self.emu.writes)

    def test35_missing_control_observations_is_not_completion(self):
        self.mode35();callsp=self.receive()
        with self.assertRaises(module.UIServiceProbeError):
            self.at(self.probe.return_address,r0=1,r13=callsp)
        self.assertFalse(self.probe.completed)
        self.assertTrue(self.emu.full_register_restored)

    def test_no_other_command_allowlisted(self):
        for command in (0x2b,0x37,True):
            with self.assertRaises(ValueError):module.UIServiceProbe(self.emu,command=command)


if __name__=='__main__':
    unittest.main()
