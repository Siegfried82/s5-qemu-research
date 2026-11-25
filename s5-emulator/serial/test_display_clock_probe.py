#!/usr/bin/env python3
"""Host orchestration guards only; no QEMU, IRQ execution, or physical timing."""
import importlib.util
import json
import struct
import time
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parent

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

module=load('display_clock_probe',HERE.parent/'menu-smp/display_clock_probe.py')
helpers=load('key_contract_helpers',HERE/'test_key_probe.py')


class FakeQMP:
    def __init__(self):
        self.calls,self.writes=[],[]
        self.running,self.enabled,self.mask=False,False,0
        self.counts={'196':0,'230':0,'232':0}
        self.double_pulse=False
        self.gic={'distributor_control':1,'cpus':[{'control':1,'priority_mask':240}],
                  'enabled_or_asserted_irqs':[{'id':i,'enabled':255,'targets':1,
                      'priority_cpu0':0,'pending':0,'active':0,'level':0}for i in (196,232)]}

    def command(self,name,arguments=None,timeout=None):
        self.calls.append((name,arguments))
        if name=='query-status':return {'running':self.running,'status':'debug'}
        assert arguments['path']=='/machine'
        prop=arguments['property']
        if name=='qom-set':
            value=arguments['value'];self.writes.append((prop,value))
            if prop=='display-test-mode':self.enabled=value
            elif prop=='display-test-status-mask':self.mask=value
            elif prop=='display-test-pulse':
                assert self.enabled and value in (196,230,232)
                self.counts[str(value)]+=2 if self.double_pulse else 1
            else:raise AssertionError(prop)
            return {}
        assert name=='qom-get'
        if prop=='display-test-mode':return self.enabled
        if prop=='display-test-status-mask':return self.mask
        if prop=='gic-state-json':return json.dumps(self.gic)
        if prop=='display-test-state-json':
            return json.dumps({'scope':'test-only explicit host status and IRQ inputs',
                'enabled':self.enabled,'status_mask':self.mask,'automatic_timing':False,
                'hardware_clock_verified':False,'pulses':self.counts})
        raise AssertionError(prop)


class FakeQemu(helpers.FakeQemu):
    def __init__(self):
        super().__init__()
        self.selected_cpu=0
        self.qmp=FakeQMP()
        self.regs.update(r15=0x1234,cpsr=0x60000192)
        self.state(1,0,module.CALLBACK,dirty=1)
        self.word(0x090db524,0)

    def word(self,address,value):
        self.seed(address,struct.pack('<I',value))

    def state(self,lcd,avio,callback,dirty=0):
        for address,value in ((0x08ab488c,lcd),(0x08a9c7f8,avio),
                              (0x08ab4890,callback),(0x090db508,dirty)):
            self.word(address,value)


class DisplayClockProbeTests(unittest.TestCase):
    def setUp(self):
        self.emu=FakeQemu();self.probe=module.DisplayClockProbe(self.emu)

    def at(self,pc,cpu=0):
        self.emu.regs['r15']=pc
        return self.probe.handle(pc,cpu)

    def primary(self):
        self.probe.start()
        self.at(module.PRIMARY_ISR)
        self.emu.state(2,0,module.CALLBACK)
        self.at(module.PRIMARY_CHECK)

    def test_default_has_no_io_even_on_close(self):
        self.assertEqual(self.emu.qmp.calls,[])
        self.assertFalse(self.emu.writes)
        self.probe.close()
        self.assertEqual(self.emu.qmp.calls,[])

    def test_exact_three_pulses_original_observations_and_input_restore(self):
        self.emu.qmp.mask=2
        self.primary()
        self.at(module.SECONDARY_ISR)
        self.emu.state(3,1,module.CALLBACK)
        self.at(module.SECONDARY_CHECK)
        self.at(module.SECONDARY_ISR)
        self.emu.state(4,2,0)
        self.at(module.SECONDARY_CHECK)
        self.assertTrue(self.probe.completed)
        self.assertFalse(self.probe.active)
        self.assertFalse(self.emu.qmp.enabled)
        self.assertEqual(self.emu.qmp.mask,2)
        self.assertEqual(self.emu.qmp.counts,{'196':1,'230':0,'232':2})
        self.assertEqual(len(self.probe.report['events']),6)
        self.assertEqual(self.emu.writes,[])
        self.assertFalse(self.emu.breakpoints)
        self.assertFalse(self.probe.report['hardware_clock_verified'])

    def test_unknown_initial_guest_state_rejected_without_inputs(self):
        for address,value in ((0x08ab488c,2),(0x08ab4890,0),(0x08a9c7f8,1),
                              (0x08ab1bba,1),(0x090db508,0),(0x090db524,4)):
            self.setUp();self.emu.word(address,value)
            with self.assertRaises(module.DisplayClockProbeError):self.probe.start()
            self.assertEqual(self.emu.qmp.writes,[])

    def test_running_guest_rejected(self):
        self.emu.qmp.running=True
        with self.assertRaises(module.DisplayClockProbeError):self.probe.start()
        self.assertEqual(self.emu.qmp.writes,[])

    def test_existing_owner_not_disabled(self):
        self.emu.qmp.enabled=True
        with self.assertRaises(module.DisplayClockProbeError):self.probe.start()
        self.assertTrue(self.emu.qmp.enabled)
        self.assertEqual(self.emu.qmp.writes,[])

    def test_pending_or_wrong_route_rejected_without_pulse(self):
        for key,value in (('pending',1),('active',1),('targets',2),('enabled',0)):
            self.setUp();self.emu.qmp.gic['enabled_or_asserted_irqs'][0][key]=value
            with self.assertRaises(module.DisplayClockProbeError):self.probe.start()
            self.assertEqual(self.emu.qmp.writes,[])

    def test_state_progress_without_original_isr_not_accepted(self):
        self.probe.start();self.emu.state(2,0,module.CALLBACK)
        with self.assertRaises(module.DisplayClockProbeError):self.at(module.PRIMARY_CHECK)
        self.assertFalse(self.emu.qmp.enabled)
        self.assertFalse(self.probe.completed)
        self.assertEqual(self.emu.qmp.counts['196'],1)

    def test_wrong_original_result_stops_without_more_pulses(self):
        self.probe.start();self.at(module.PRIMARY_ISR)
        with self.assertRaises(module.DisplayClockProbeError):self.at(module.PRIMARY_CHECK)
        self.assertEqual(self.emu.qmp.counts,{'196':1,'230':0,'232':0})
        self.assertFalse(self.emu.qmp.enabled)

    def test_model_counter_mismatch_fails_closed(self):
        self.emu.qmp.double_pulse=True
        with self.assertRaises(module.DisplayClockProbeError):self.probe.start()
        self.assertFalse(self.emu.qmp.enabled)
        self.assertFalse(self.emu.breakpoints)

    def test_timeout_and_close_preserve_primary_reason(self):
        self.probe.start();self.probe._deadline=time.monotonic()-1
        with self.assertRaises(module.DisplayClockProbeError):self.probe.check_timeout()
        self.probe.close()
        self.assertEqual(self.probe.report['status'],'timeout_original_handler')
        self.assertFalse(self.emu.qmp.enabled)
        self.assertFalse(self.probe.completed)

    def test_wrong_cpu_observation_stops(self):
        self.probe.start();self.emu.selected_cpu=1
        with self.assertRaises(module.DisplayClockProbeError):self.at(module.PRIMARY_ISR,1)
        self.assertFalse(self.emu.qmp.enabled)


if __name__=='__main__':
    unittest.main()
