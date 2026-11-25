#!/usr/bin/env python3
"""Host-only contract tests. These do not execute firmware or prove menu input."""
from pathlib import Path
import importlib.util
import struct
import time
import unittest

path = Path(__file__).resolve().parents[1] / 'menu-smp/key_probe.py'
spec = importlib.util.spec_from_file_location('key_probe', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeRSP:
    def __init__(self, emu):
        self.emu = emu
        self.fp = {i:bytes([i])*8 for i in range(26,58)}
        self.fp.update({74:b'FPSC',75:b'FPID',76:b'FPEX'})
        self.register_writes = []

    def read_feature(self, name):
        return (Path(__file__).parent / 'results-key-register-xml' / name).read_text()

    def request(self, command):
        if command == 'g':
            return (struct.pack('<17I', *[self.emu.regs[f'r{i}'] for i in range(16)],
                                self.emu.regs['cpsr'])).hex()
        if command.startswith('G'):
            raw = bytes.fromhex(command[1:]); vals = struct.unpack('<17I', raw[:68])
            self.emu.regs = {**{f'r{i}': vals[i] for i in range(16)}, 'cpsr': vals[16]}
            self.emu.full_register_restored = len(raw)==68
            return 'OK'
        if command.startswith('p'):
            return self.fp[int(command[1:],16)].hex()
        if command.startswith('P'):
            number,value=command[1:].split('=');number=int(number,16)
            self.register_writes.append(number);self.fp[number]=bytes.fromhex(value)
            return 'OK'
        raise AssertionError(command)


class FakeQemu:
    def __init__(self):
        self.selected_cpu = 2
        self.mem, self.breakpoints, self.writes = {}, set(), []
        self.regs = {**{f'r{i}': i for i in range(16)}, 'r13': 0x0a03df78,
                     'r15': module.LOOP_ENTRY, 'cpsr': 0x60000133}
        self.rsp = FakeRSP(self)
        self.full_register_restored = False
        tcb = 0x08e15000
        self.seed(0x20048, struct.pack('<I', tcb))
        self.seed(tcb, b'DRHT')
        for off, value in [(0xc,0x0a031b98),(0x10,0x0a03df97),(0xc0,285),
                           (0xd8,0x06475dd5),(0xec,2)]:
            self.seed(tcb+off, struct.pack('<I',value))

    def seed(self, a, data):
        self.mem.update({a+i:v for i,v in enumerate(data)})

    def read_mem(self, a, n):
        return bytes(self.mem.get(a+i,0) for i in range(n))

    def write_mem(self, a, data):
        self.writes.append(('mem', a, bytes(data))); self.seed(a,data)

    def write_reg(self, name, value):
        name = {'sp':'r13','lr':'r14','pc':'r15'}.get(name,name)
        self.writes.append(('reg',name,value)); self.regs[name]=value

    def registers(self):
        return dict(self.regs)

    def breakpoint_add(self,a,kind=2):
        self.breakpoints.add(a)

    def breakpoint_remove(self,a,kind=2):
        self.breakpoints.discard(a)

    def step(self,timeout):
        self.regs['r15'] += 2


class KeyProbeTests(unittest.TestCase):
    def setUp(self):
        self.emu = FakeQemu(); self.probe = module.KeyProbe(self.emu)

    def start(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1)
        self.probe.handle(module.LOOP_ENTRY,2)

    def test_construction_does_not_arm_or_write(self):
        self.assertFalse(self.emu.breakpoints); self.assertFalse(self.emu.writes)

    def test_key_and_action_allowlist(self):
        for key,action in [('POWER_OFF',1),('KEY_CENTER',3),('KEY_CENTER',True)]:
            with self.assertRaises(ValueError):self.probe.arm(key,action,swaps_observed=1)
        self.assertFalse(self.emu.writes)

    def test_requires_swap(self):
        with self.assertRaises(module.KeyProbeError):self.probe.arm('UP',1,swaps_observed=0)

    def test_context_guards_fail_without_guest_write(self):
        for name,value in [('cpsr',0x60000192),('cpsr',0x60000533),('r13',0xa03df74)]:
            emu=FakeQemu();emu.regs[name]=value;p=module.KeyProbe(emu)
            p.arm('KEY_CENTER',1,swaps_observed=1)
            with self.assertRaises(module.KeyProbeError):p.handle(module.LOOP_ENTRY,2)
            self.assertEqual(emu.writes,[])

    def test_transient_dispatch_guard_and_irq_disabled_wait_then_start(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1)
        deadline=self.probe._deadline
        # Transient global guards (including SMP 0x08ab4e68), per-CPU nesting, and IRQ-disabled bit
        transient_states=[
            (module.GUARDS[0],1,0x60000133),
            (module.GUARDS[2],1,0x60000133),
            (0x209c8,1,0x60000133),
            (None,0,0x600001b3),
        ]
        for addr,val,cpsr in transient_states:
            for a in module.GUARDS+(0x209c8,):
                self.emu.seed(a,struct.pack('<I',0))
            if addr is not None:
                self.emu.seed(addr,struct.pack('<I',val))
            self.emu.regs.update(r15=module.LOOP_ENTRY,cpsr=cpsr)
            self.assertTrue(self.probe.handle(module.LOOP_ENTRY,2))
            self.assertEqual(self.emu.writes,[])
            self.assertIsNone(self.probe._saved)
            self.assertIn(module.LOOP_ENTRY,self.emu.breakpoints)
            self.assertEqual(self.probe._deadline,deadline)
            self.assertEqual(self.probe.report['requests'][0]['status'],'waiting_for_clean_dispatch_guard')
        # Clear all guards and IRQ-disabled bit -> next LOOP_ENTRY starts cleanly.
        for a in module.GUARDS+(0x209c8,):
            self.emu.seed(a,struct.pack('<I',0))
        self.emu.regs.update(r15=module.LOOP_ENTRY,cpsr=0x60000133)
        self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)
        self.assertEqual(self.probe.report['requests'][0]['dispatch_guard_wait_steps'],4)

    def test_transient_dispatch_guard_timeout_reports_distinct_status_without_writes(self):
        self.emu.seed(module.GUARDS[2],struct.pack('<I',1))
        self.probe.arm('KEY_CENTER',1,swaps_observed=1)
        self.probe.handle(module.LOOP_ENTRY,2)
        self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.KeyProbeError):self.probe.check_timeout()
        self.probe.close()
        req=self.probe.report['requests'][0]
        self.assertEqual(req['status'],'timeout_waiting_for_clean_dispatch_guard')
        self.assertEqual(req['dispatch_guard_wait_steps'],1)
        self.assertFalse(self.emu.writes)

    def test_start_hard_guards_reject_nonzero_dispatch_guard_and_irq_disabled(self):
        for addr,val,cpsr in [
            (module.GUARDS[0],1,0x60000133),
            (module.GUARDS[2],1,0x60000133),
            (0x209c8,1,0x60000133),
            (None,0,0x600001b3),
        ]:
            emu=FakeQemu()
            if addr is not None:
                emu.seed(addr,struct.pack('<I',val))
            emu.regs['cpsr']=cpsr
            p=module.KeyProbe(emu)
            p.arm('KEY_CENTER',1,swaps_observed=1)
            with self.assertRaises(module.KeyProbeError):p._start(2,emu.registers())
            self.assertEqual(p.report['requests'][0]['status'],'guard_rejected')
            self.assertFalse(emu.writes)

    def test_bounded_original_arguments_and_full_register_restore(self):
        original=self.emu.registers();fp=dict(self.emu.rsp.fp);self.start();call_sp=original['r13']-32
        self.emu.rsp.fp[26]=b'changed!';self.emu.rsp.fp[76]=b'xxxx'
        words=struct.unpack('<8I',self.emu.read_mem(call_sp,32))
        self.assertEqual(words,(0x11f002,call_sp+16,8,0,7,1,0,0))
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)
        self.emu.regs.update(r15=self.probe.return_address,r0=0,r13=call_sp)
        self.probe.handle(self.probe.return_address,2)
        self.assertEqual(self.emu.regs,original)
        self.assertTrue(self.emu.full_register_restored)
        self.assertEqual(self.emu.rsp.fp,fp)
        self.assertNotIn(75,self.emu.rsp.register_writes)
        self.assertEqual(self.emu.read_mem(call_sp,32),bytes(32))
        self.assertTrue(self.probe.report['requests'][0]['queue_accepted'])

    def test_wrong_return_task_never_restores(self):
        self.start();self.emu.seed(0x08e150c0,struct.pack('<I',1))
        # TCBidentity itself unchanged but taskid mismatch must also reject.
        self.emu.seed(0x20048,struct.pack('<I',0x08e14000))
        self.emu.regs['r15']=self.probe.return_address
        with self.assertRaises(module.KeyProbeError):self.probe.handle(self.probe.return_address,2)
        self.assertFalse(self.emu.full_register_restored)

    def test_only_original_allocated_pointer_is_consumption_evidence(self):
        self.start();call_sp=self.probe._saved['sp'];ptr=0x0b000100
        self.emu.seed(ptr,struct.pack('<II',7,1))
        self.emu.seed(call_sp-0x60+0x20,struct.pack('<I',ptr))
        self.emu.regs.update(r15=module.POOL_COPY_DONE,r13=call_sp-0x60)
        self.probe.handle(module.POOL_COPY_DONE,2)
        self.emu.regs.update(r15=self.probe.return_address,r13=call_sp,r0=0)
        self.probe.handle(self.probe.return_address,2)
        self.emu.regs.update(r15=module.KEY_DISPATCH,r0=ptr+8)
        self.probe.handle(module.KEY_DISPATCH,2)
        self.assertFalse(self.probe._inside_dispatch)
        self.emu.regs.update(r15=module.KEY_DISPATCH,r0=ptr)
        self.probe.handle(module.KEY_DISPATCH,2)
        self.emu.regs['r15']=module.KEY_DISPATCH_RETURN
        self.probe.handle(module.KEY_DISPATCH_RETURN,2)
        self.assertTrue(self.probe.report['requests'][0]['consumed'])
        self.assertFalse(self.probe.active)
        self.assertFalse(self.probe.report['menu_opened_verified'])
        self.probe.arm('KEY_CENTER',0,swaps_observed=1)
        self.assertTrue(self.probe.active)
        self.assertEqual([r['action'] for r in self.probe.report['requests']],[1,0])
        self.assertTrue(self.probe.report['requests'][0]['consumed'])

    def test_close_preserves_rejection(self):
        self.emu.regs['cpsr']=0x60000533
        self.probe.arm('UP',1,swaps_observed=1)
        with self.assertRaises(module.KeyProbeError):self.probe.handle(module.LOOP_ENTRY,2)
        self.probe.close()
        self.assertEqual(self.probe.report['requests'][0]['status'],'guard_rejected')
        self.assertTrue(self.probe.report['requests'][0]['closed'])

    def test_ui_gate_waits_on_original_loop_without_call_or_deadline_extension(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_ui_active=True)
        deadline=self.probe._deadline
        for state,mode in ((0,6),(0,1),(1,6)):
            self.emu.seed(0x08a928b4,struct.pack('<I',state))
            self.emu.seed(0x08a928bc,struct.pack('<I',mode))
            self.emu.regs['r15']=module.LOOP_ENTRY
            self.assertTrue(self.probe.handle(module.LOOP_ENTRY,2))
            self.assertEqual(self.emu.writes,[])
            self.assertIsNone(self.probe._saved)
            self.assertIn(module.LOOP_ENTRY,self.emu.breakpoints)
            self.assertEqual(self.probe._deadline,deadline)
        self.emu.seed(0x08a928bc,struct.pack('<I',1))
        self.emu.regs['r15']=module.LOOP_ENTRY
        self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)
        gate=self.probe.report['requests'][0]['ui_active_gate']
        self.assertEqual(gate['wait_steps'],3)
        self.assertTrue(gate['passed'])

    def test_ui_gate_timeout_stays_bounded_and_close_preserves_reason(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_ui_active=True)
        self.probe.handle(module.LOOP_ENTRY,2)
        self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.KeyProbeError):self.probe.check_timeout()
        self.probe.close()
        self.assertEqual(self.probe.report['requests'][0]['status'],'timeout_waiting_for_ui_active')
        self.assertFalse(self.emu.writes)

    def test_default_does_not_wait_for_ui_state(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1)
        self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)

    def test_ui_gate_does_not_bypass_start_guards(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_ui_active=True)
        self.emu.seed(0x08a928b4,struct.pack('<I',1))
        self.emu.seed(0x08a928bc,struct.pack('<I',1))
        self.emu.regs['cpsr']=0x60000533
        with self.assertRaises(module.KeyProbeError):self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.writes,[])

    def test_mode35_gate_waits_without_guest_write_or_deadline_extension(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_mode35_ack=True)
        deadline=self.probe._deadline
        for mask in (0,0x0d,0xfd):
            self.emu.seed(0x08aa1103,bytes([mask]))
            self.emu.regs['r15']=module.LOOP_ENTRY
            self.probe.handle(module.LOOP_ENTRY,2)
            self.assertEqual(self.emu.writes,[])
            self.assertEqual(self.probe._deadline,deadline)
            self.assertEqual(self.probe.report['requests'][0]['mode35_ack_gate']['mask'],mask)
        self.emu.seed(0x08aa1103,b'\xff')
        self.emu.regs['r15']=module.LOOP_ENTRY
        self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)
        gate=self.probe.report['requests'][0]['mode35_ack_gate']
        self.assertTrue(gate['passed'])
        self.assertEqual(gate['wait_steps'],3)

    def test_both_gates_must_pass_on_same_original_loop_stop(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_ui_active=True,
                       wait_for_mode35_ack=True)
        for state,mode,mask in ((0,6,15),(1,1,13)):
            self.emu.seed(0x08a928b4,struct.pack('<I',state))
            self.emu.seed(0x08a928bc,struct.pack('<I',mode))
            self.emu.seed(0x08aa1103,bytes([mask]))
            self.emu.regs['r15']=module.LOOP_ENTRY
            self.probe.handle(module.LOOP_ENTRY,2)
            self.assertEqual(self.emu.writes,[])
        self.emu.seed(0x08aa1103,b'\x0f')
        self.emu.regs.update(r15=module.LOOP_ENTRY,cpsr=0x60000533)
        with self.assertRaises(module.KeyProbeError):self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.writes,[])

    def test_mode35_gate_timeout_retains_actual_mask(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_mode35_ack=True)
        self.emu.seed(0x08aa1103,b'\x0d')
        self.probe.handle(module.LOOP_ENTRY,2)
        self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.KeyProbeError):self.probe.check_timeout()
        self.probe.close()
        request=self.probe.report['requests'][0]
        self.assertEqual(request['status'],'timeout_waiting_for_mode35_ack')
        self.assertEqual(request['mode35_ack_gate']['mask'],13)
        self.assertFalse(self.emu.writes)

    def test_transition_gate_waits_for_original_terminal_step_and_stage(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_transition_complete=True)
        deadline=self.probe._deadline
        for step,stage,state,mode in ((0x436,2,1,1),(0x4a0,2,1,1),(0x4a0,0,0,1),(0x4a0,0,1,6)):
            for address,value in ((0x08cb0ee0,step),(0x08aa1120,stage),(0x08a928b4,state),(0x08a928bc,mode)):
                self.emu.seed(address,struct.pack('<I',value))
            self.emu.regs['r15']=module.LOOP_ENTRY
            self.probe.handle(module.LOOP_ENTRY,2)
            self.assertFalse(self.emu.writes);self.assertEqual(self.probe._deadline,deadline)
        self.emu.seed(0x08a928bc,struct.pack('<I',1))
        self.emu.regs['r15']=module.LOOP_ENTRY;self.probe.handle(module.LOOP_ENTRY,2)
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)
        gate=self.probe.report['requests'][0]['transition_complete_gate']
        self.assertEqual(gate['wait_steps'],4);self.assertTrue(gate['passed'])

    def test_transition_gate_timeout_keeps_original_pending_state(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_transition_complete=True)
        self.probe.handle(module.LOOP_ENTRY,2);self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.KeyProbeError):self.probe.check_timeout()
        self.probe.close()
        self.assertEqual(self.probe.report['requests'][0]['status'],'timeout_waiting_for_transition_complete')
        self.assertFalse(self.emu.writes)

    def test_route_observer_reads_exact_original_stack_payload_without_write(self):
        self.start();self.probe._inside_dispatch=True
        pointer=0x0a03de00;payload=struct.pack('<22I',*range(22))
        self.emu.seed(pointer,payload)
        self.emu.regs.update(r0=pointer,r15=0x0663fe34)
        writes=list(self.emu.writes);deadline=self.probe._deadline
        self.probe.handle(0x0663fe34,2)
        event=self.probe.report['requests'][0]['events'][-1]
        self.assertEqual(event['route'],'live_key_table_route_entry')
        self.assertEqual(event['local_payload_hex'],payload.hex())
        self.assertEqual(event['local_payload_words'],[hex(x) for x in range(22)])
        self.assertEqual(self.emu.writes,writes);self.assertEqual(self.probe._deadline,deadline)
        self.assertEqual(self.emu.regs['r0'],pointer)

    def test_route_return_records_actual_gate_but_never_rewrites_it(self):
        self.start();self.probe._inside_dispatch=True
        self.emu.seed(0x08aa1124,struct.pack('<I',1))
        self.emu.regs.update(r0=1,r15=0x06643224)
        writes=list(self.emu.writes);self.probe.handle(0x06643224,2)
        event=self.probe.report['requests'][0]['events'][-1]
        self.assertEqual(event['r0_r2'][0],'0x1')
        self.assertEqual(event['gate_values']['pending_mode'],'0x1')
        self.assertEqual(self.emu.regs['r0'],1);self.assertEqual(self.emu.writes,writes)

    def test_center_table_observation_requires_exact_original_table_slot(self):
        self.start();self.probe._inside_dispatch=True
        self.emu.regs.update(r4=0x06f10544,r15=0x0663fed0)
        self.probe.handle(0x0663fed0,2)
        self.assertEqual(self.probe.report['requests'][0]['events'],[])
        self.emu.regs.update(r4=0x06f10554,r15=0x0663fed0)
        self.probe.handle(0x0663fed0,2)
        self.assertEqual(self.probe.report['requests'][0]['events'][-1]['route'],
                         'center_table_trigger_matched')

    def test_route_payload_outside_original_stack_fails_without_write(self):
        self.start();self.probe._inside_dispatch=True
        self.emu.regs.update(r0=0x7f000000,r15=0x0663fe34)
        writes=list(self.emu.writes)
        with self.assertRaises(module.KeyProbeError):self.probe.handle(0x0663fe34,2)
        self.assertEqual(self.emu.writes,writes)

    def test_native_input_route_gate_waits_for_both_original_predicates(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_input_route_ready=True)
        deadline=self.probe._deadline
        for stage,pending,secondary in ((0,0xff,2),(0,0xff,0xffffffff),(2,0xff,0),(0,1,0)):
            for address,value in ((0x08aa1120,stage),(0x08aa1124,pending),(0x08aa090c,secondary)):
                self.emu.seed(address,struct.pack('<I',value))
            self.emu.regs['r15']=module.LOOP_ENTRY
            self.probe.handle(module.LOOP_ENTRY,2)
            self.assertFalse(self.emu.writes);self.assertEqual(self.probe._deadline,deadline)
            self.assertEqual(self.probe.report['requests'][0]['status'],'waiting_for_input_route_ready')
        # Both original predicates also permit stage1; do not invent stage0-only behavior.
        for address,value in ((0x08aa1120,1),(0x08aa1124,0xff),(0x08aa090c,1)):
            self.emu.seed(address,struct.pack('<I',value))
        self.emu.regs['r15']=module.LOOP_ENTRY;self.probe.handle(module.LOOP_ENTRY,2)
        gate=self.probe.report['requests'][0]['input_route_gate']
        self.assertTrue(gate['passed']);self.assertEqual(gate['wait_steps'],4)
        self.assertEqual(self.emu.regs['r15'],module.QUEUE_API)

    def test_input_route_gate_timeout_preserves_actual_secondary_lock(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_input_route_ready=True)
        self.emu.seed(0x08aa1124,struct.pack('<I',0xff))
        self.emu.seed(0x08aa090c,struct.pack('<I',2))
        self.probe.handle(module.LOOP_ENTRY,2);self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.KeyProbeError):self.probe.check_timeout()
        self.probe.close();req=self.probe.report['requests'][0]
        self.assertEqual(req['status'],'timeout_waiting_for_input_route_ready')
        self.assertEqual(req['input_route_gate']['secondary_stage'],2)
        self.assertTrue(req['input_route_gate']['primary_ready'])
        self.assertFalse(req['input_route_gate']['secondary_ready'])
        self.assertFalse(self.emu.writes)

    def test_input_route_gate_cannot_bypass_original_call_context_guards(self):
        self.probe.arm('KEY_CENTER',1,swaps_observed=1,wait_for_input_route_ready=True)
        self.emu.seed(0x08aa1124,struct.pack('<I',0xff))
        self.emu.regs['cpsr']=0x60000533
        with self.assertRaises(module.KeyProbeError):self.probe.handle(module.LOOP_ENTRY,2)
        self.assertFalse(self.emu.writes)

    def test_all_supported_keys_match_static_06f1003c_table_evidence(self):
        import json
        evidence = json.loads((Path(__file__).parent / 'key-event-evidence.json').read_text())
        by_name = {entry['name']: entry for entry in evidence['keys']}
        case_to_bank = {'0x6643776': 0, '0x664378c': 1}
        expected_new = (
            'KEY_DISPLAY', 'KEY_DELETE', 'KEY_ISO', 'KEY_WB', 'KEY_EXPOSE',
            'CONTROL_DIAL_LEFT', 'CONTROL_DIAL_RIGHT',
            'FRONT_DIAL_LEFT', 'FRONT_DIAL_RIGHT',
            'REAR_DIAL_LEFT', 'REAR_DIAL_RIGHT',
        )
        for name in expected_new:
            self.assertIn(name, module.KEYS)
        for name, (index, bank, mask) in module.KEYS.items():
            self.assertIn(name, by_name)
            row = by_name[name]
            self.assertEqual(index, row['index'], name)
            self.assertEqual(bank, case_to_bank[row['dispatcher_case']], name)
            self.assertEqual(mask, int(row['value'], 0), name)


if __name__ == '__main__':
    unittest.main()
