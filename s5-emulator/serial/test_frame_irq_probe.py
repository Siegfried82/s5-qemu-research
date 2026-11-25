#!/usr/bin/env python3
"""Host contract checks only. No actual firmware, GIC or frame is executed."""
import importlib.util,json,struct,time,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
module=load('frame_irq_probe',HERE.parent/'menu-smp/frame_irq_probe.py')
helpers=load('key_helpers',HERE/'test_key_probe.py')

class QMP:
    def __init__(self):
        self.calls=[];self.writes=[];self.enabled=False;self.counts={'192':0,'193':0};self.double=False
        self.gic={'distributor_control':1,'cpus':[{'control':1,'priority_mask':240}],
          'enabled_or_asserted_irqs':[{'id':i,'enabled':255,'targets':1,'priority_cpu0':0,
            'pending':0,'active':0,'level':0}for i in (192,193)]}
    def command(self,cmd,args=None,timeout=None):
        self.calls.append((cmd,args))
        if cmd=='query-status':return {'running':False}
        p=args['property']
        if cmd=='qom-set':
            v=args['value'];self.writes.append((p,v))
            if p=='frame-test-mode':self.enabled=v
            elif p=='frame-test-pulse':
                assert self.enabled and v in (192,193)
                self.counts[str(v)]+=2 if self.double else 1
            else:raise AssertionError(p)
            return {}
        if p=='frame-test-mode':return self.enabled
        if p=='gic-state-json':return json.dumps(self.gic)
        if p=='frame-test-state-json':return json.dumps({'enabled':self.enabled,'automatic_timing':False,'pulses':self.counts})
        raise AssertionError(p)

class Emu(helpers.FakeQemu):
    def __init__(self):
        super().__init__();self.selected_cpu=0;self.qmp=QMP()
        for a,v in[(0x8aa7f19,1),(0x8aa02af,1),(0x8aac2f9,1)]:self.seed(a,bytes([v]))
        self.seed(0x8e1b94c+(0x77-1)*4,struct.pack('<I',0x8e1a0b0))
        self.seed(0x20040,struct.pack('<I',0x8e03c18));self.seed(0x8e03c18,b'DRHT')
        for off,value in[(0xc,0x9acf048),(0x10,0x9ad6847),(0xc0,67),(0xd8,0x6705545),(0xec,2)]:
            self.seed(0x8e03c18+off,struct.pack('<I',value))
        self.seed(0x6600000-2,b'\x98\x47') # BLX r3; Thumb return
        self.seed(0x6600020-4,struct.pack('<I',0xe12fff33)) # ARM BLX r3
        self.regs.update(r13=0x20300,r14=0x6600001,cpsr=0x800001b2)

class Tests(unittest.TestCase):
    def setUp(self):self.emu=Emu();self.p=module.FrameIRQProbe(self.emu)
    def at(self,pc,cpu=0):
        self.emu.selected_cpu=cpu;self.emu.regs['r15']=pc
        return self.p.handle(pc,cpu)
    def entry(self,irq):
        self.emu.regs.update(r13=0x20300,cpsr=0x800001b2)
        return self.at(module.ISR[irq])
    def ret(self):return self.at(self.p._entry['return_pc'])
    def test_default_off_no_io_even_close(self):
        self.p.close();self.assertFalse(self.emu.qmp.calls);self.assertFalse(self.emu.writes)
    def test_bounded_pairs(self):
        for value in (0,129,True,1.5):
            with self.assertRaises(ValueError):module.FrameIRQProbe(self.emu,pairs=value)
    def test_true_readiness_required_before_test_enable(self):
        self.emu.seed(0x8aa7f19,b'\0')
        with self.assertRaises(module.FrameIRQProbeError):self.p.start()
        self.assertFalse(self.emu.qmp.writes);self.assertFalse(self.emu.breakpoints)
    def test_prior_owner_never_disabled(self):
        self.emu.qmp.enabled=True
        with self.assertRaises(module.FrameIRQProbeError):self.p.start()
        self.assertTrue(self.emu.qmp.enabled);self.assertFalse(self.emu.qmp.writes)
    def test_two_pairs_real_entry_return_no_guest_writes(self):
        self.p=module.FrameIRQProbe(self.emu,pairs=2);self.p.start()
        for irq in (192,193,192,193):
            self.entry(irq);self.ret()
        self.assertFalse(self.p.completed)
        self.consumer_cycle()
        self.assertTrue(self.p.completed);self.assertFalse(self.p.active)
        self.assertEqual(self.emu.qmp.counts,{'192':2,'193':2})
        self.assertFalse(self.emu.qmp.enabled);self.assertFalse(self.emu.breakpoints)
        self.assertFalse(self.emu.writes);self.assertEqual(len(self.p.report['events']),8)
    def consumer_cycle(self,queue_count=0):
        self.emu.regs.update(r13=0x9ad6810,cpsr=0x60000133,r0=0)
        self.emu.seed(0x9ad6810,struct.pack('<5I',0,0x2001,0,0x8c51d9c,0))
        self.at(module.RECEIVE_RETURN)
        self.emu.seed(0x8aac26c,struct.pack('<H',self.p._pair_poll+1))
        self.emu.seed(0x8e1a0b0+0x18,struct.pack('<I',queue_count))
        self.at(module.CONSUMER_DONE);self.at(module.CONSUMER_LOOP)
    def test_next_pair_waits_for_complete_consumer_and_queue_drain(self):
        self.p=module.FrameIRQProbe(self.emu,pairs=3);self.p.start()
        for irq in (192,193,192,193):self.entry(irq);self.ret()
        self.assertEqual(len(self.p.report['pulses']),4)
        self.consumer_cycle(queue_count=1)
        self.assertEqual(len(self.p.report['pulses']),4)
        self.consumer_cycle(queue_count=0)
        self.assertEqual(len(self.p.report['pulses']),5)
        self.assertEqual(self.p.report['pair_consumption'][0]['completed_messages'],2)
    def test_missing_frame_consumption_times_out_without_next_pair(self):
        self.p=module.FrameIRQProbe(self.emu,pairs=128);self.p.start()
        for irq in (192,193,192,193):self.entry(irq);self.ret()
        self.p._deadline=time.monotonic()-1
        with self.assertRaises(module.FrameIRQProbeError):self.p.check_timeout()
        self.assertEqual(len(self.p.report['pulses']),4)
        self.assertFalse(self.emu.qmp.enabled)
    def test_ack_requires_actual_helper_and_successful_matching_packet(self):
        self.p=module.FrameIRQProbe(self.emu,pairs=128);self.p.start()
        for irq in (192,193,192,193):self.entry(irq);self.ret()
        self.emu.regs.update(r13=0x9ad6610,cpsr=0x60000133,r0=0x35,r1=1,r2=0)
        self.at(module.ACK_ENTRY)
        ptr=0x8e9f518;self.emu.seed(ptr,struct.pack('<5I',0x76,0x42,0x35,0,1))
        self.emu.regs.update(r0=0,r4=ptr)
        self.at(module.ACK_SENT)
        self.assertTrue(self.p.report['original_mode35_ack_enqueued'])
        self.consumer_cycle()
        self.assertTrue(self.p.completed)
        self.assertEqual(self.p.report['status'],'original_mode35_ack_enqueued_ui_pending')
        self.assertEqual(len(self.p.report['pulses']),4)
    def test_failed_ack_never_claims_enqueued(self):
        self.p.start();self.emu.regs.update(r13=0x9ad6610,cpsr=0x60000133,r0=0x35,r1=1,r2=0)
        self.at(module.ACK_ENTRY)
        ptr=0x8e9f518;self.emu.seed(ptr,struct.pack('<5I',0x76,0x42,0x35,0,1))
        self.emu.regs.update(r0=1,r4=ptr)
        with self.assertRaises(module.FrameIRQProbeError):self.at(module.ACK_SENT)
        self.assertFalse(self.p.report['original_mode35_ack_enqueued'])
        self.assertFalse(self.emu.qmp.enabled)
    def test_actual_ui_mask_can_end_inputs_without_claiming_observed_ack_call(self):
        self.p=module.FrameIRQProbe(self.emu,pairs=128);self.p.start()
        for irq in (192,193,192,193):self.entry(irq);self.ret()
        self.emu.seed(0x8aa1103,b'\x0f');self.consumer_cycle()
        self.assertEqual(self.p.report['status'],'original_mode35_ack_consumed_stop_test_inputs')
        self.assertFalse(self.p.report['original_mode35_ack_enqueued'])
    def test_arm_return_site_supported(self):
        self.p.start();self.emu.regs['r14']=0x6600020;self.entry(192)
        self.assertEqual(self.p._entry['return_kind'],4)
        self.emu.regs['cpsr']=0x80000192;self.ret()
        self.assertEqual(self.emu.qmp.counts,{'192':1,'193':1})
    def test_bad_return_instruction_stops_no_context_write(self):
        self.p.start();self.emu.seed(0x6600000-2,b'\x00\xbf')
        with self.assertRaises(module.FrameIRQProbeError):self.entry(192)
        self.assertFalse(self.emu.qmp.enabled);self.assertFalse(self.emu.writes)
    def test_foreign_cpu_or_stack_step_original_and_keep_deadline(self):
        self.p.start();self.entry(192);deadline=self.p._deadline;ret=self.p._entry['return_pc']
        self.at(ret,cpu=1);self.assertEqual(self.p._deadline,deadline)
        self.assertIn(ret,self.emu.breakpoints);self.assertFalse(self.emu.writes)
        self.emu.regs['r13']=0x20400;self.at(ret,cpu=0)
        self.assertEqual(self.p.report['foreign_return_stops'],2)
        self.emu.regs['r13']=0x20300;self.ret()
        self.assertEqual(self.emu.qmp.counts,{'192':1,'193':1})
    def test_double_pulse_detected_and_disabled(self):
        self.emu.qmp.double=True
        with self.assertRaises(module.FrameIRQProbeError):self.p.start()
        self.assertFalse(self.emu.qmp.enabled);self.assertFalse(self.emu.breakpoints)
    def test_timeout_cleans_without_fabricating_return(self):
        self.p.start();self.entry(192);self.p._deadline=time.monotonic()-1
        with self.assertRaises(module.FrameIRQProbeError):self.p.check_timeout()
        self.p.close();self.assertEqual(self.p.report['status'],'timeout_original_isr')
        self.assertFalse(self.p.completed);self.assertFalse(self.emu.qmp.enabled)
        self.assertFalse(self.emu.writes)
    def test_start_allows_current_irq_context_and_latch_zero(self):
        self.emu.regs.update(cpsr=0x800001b2,r15=0x648161a)
        self.p.start();self.assertTrue(self.p.active)
        self.assertEqual(self.p.report['initial_state']['source_latch'],0)

if __name__=='__main__':unittest.main()
