#!/usr/bin/env python3
"""Explicit TEST status/IRQ sequence, not a display clock hardware model.

Construction has no I/O. Call start() only while QEMU is stopped and after
explicitly choosing this diagnostic. Forward owned stops to handle(pc,cpu),
resume all CPUs externally, and obey timeout_hint()/check_timeout() plus an
outer process deadline. Every pulse goes through QOM to a real GIC SPI input.
Only the original ISR and original callbacks may alter guest state.

The deliberately artificial sequence is mask3, IRQ196, IRQ232, IRQ232, gated
by observed original handler progress. No period, source clock, physical phase,
enable register or device ACK semantics are inferred. The supplied mask is
constant throughout the sequence; it is not a reconstructed field waveform.
Completion only verifies the original LCD callback chain under TEST inputs.
It does not verify display hardware, a menu frame, DSP readiness or camera boot.
"""
import json
import struct
import time

PRIMARY_ISR = 0x0649728e
SECONDARY_ISR = 0x06497338
PRIMARY_CHECK = 0x064815c2
SECONDARY_CHECK = 0x0648161a
CALLBACK = 0x0647dcf7


class DisplayClockProbeError(RuntimeError):
    """Stop this diagnostic; do not fabricate a successful original callback."""


class DisplayClockProbe:
    def __init__(self, emu, *, stage_timeout=2.0):
        if not 0 < stage_timeout <= 10:
            raise ValueError('Use bounded per-stage timeout <=10s')
        self.emu, self.stage_timeout = emu, stage_timeout
        self._breakpoints = set()
        self._started = self._owns_inputs = self._completed = False
        self._stage = self._deadline = self._old_mask = None
        self._seen_isr = False
        self._last_counts = None
        self.report = {
            'scope':'TEST-only explicit host status and GIC inputs; not a hardware display/clock model',
            'status':'not_started','automatic_timing':False,'hardware_clock_verified':False,
            'phase_or_period_verified':False,'guest_callback_invoked_by_host':False,
            'guest_registers_or_ram_written_by_host':False,'ui_dsp_tcb_flags_written_by_host':False,
            'menu_rendered_verified':False,'pulses':[],'events':[],
            'pending_irq_cleanup':'Disabling TEST inputs does not retract already pending GIC edges; no GIC pending bits are manually cleared',
        }

    @property
    def active(self):
        return self._stage is not None

    @property
    def completed(self):
        return self._completed

    @property
    def breakpoints(self):
        return frozenset(self._breakpoints)

    def _get(self, name):
        return self.emu.qmp.command('qom-get',{'path':'/machine','property':name},timeout=2)

    def _set(self, name, value):
        return self.emu.qmp.command('qom-set',{'path':'/machine','property':name,'value':value},timeout=2)

    def _json(self, name):
        value=self._get(name)
        return json.loads(value)if isinstance(value,str)else value

    def _u32(self, address):
        return struct.unpack('<I',self.emu.read_mem(address,4))[0]

    def _state(self):
        return {'lcd_state':self._u32(0x08ab488c),'avio_completion':self._u32(0x08a9c7f8),
                'callback':hex(self._u32(0x08ab4890)),
                'selector':self.emu.read_mem(0x08ab1bba,1)[0],
                'bank0_dirty':self._u32(0x090db508),'bank0_state':self._u32(0x090db524)}

    def _add(self, address):
        if address not in self._breakpoints:
            self.emu.breakpoint_add(address)
            self._breakpoints.add(address)

    def _remove(self, address):
        if address in self._breakpoints:
            self.emu.breakpoint_remove(address)
            self._breakpoints.remove(address)

    def _cleanup(self):
        errors=[]
        if self._owns_inputs:
            try:
                self._set('display-test-mode',False)
                self._set('display-test-status-mask',self._old_mask)
                self.report['inputs_restored_off']=(self._get('display-test-mode')is False
                    and self._get('display-test-status-mask')==self._old_mask)
                if not self.report['inputs_restored_off']:
                    errors.append('TEST input restoration readback differs')
            except (RuntimeError,TimeoutError,OSError)as exc:
                errors.append(str(exc))
            if not errors:
                self._owns_inputs=False
        for address in tuple(self._breakpoints):
            try:
                self._remove(address)
            except (RuntimeError,TimeoutError,OSError)as exc:
                errors.append(str(exc))
        if errors:
            self.report.setdefault('cleanup_errors',[]).extend(errors)
        self._stage=self._deadline=None
        return not errors

    def _fail(self, message, status=None):
        self.report.update(status=status or ('test_sequence_stopped'if self.active else 'guard_rejected'),error=message)
        self._cleanup()
        raise DisplayClockProbeError(message)

    def timeout_hint(self):
        return 30.0 if self._deadline is None else max(.001,self._deadline-time.monotonic())

    def check_timeout(self):
        if self._deadline is not None and time.monotonic()>=self._deadline:
            self._fail('Original handler stage did not complete before its deadline','timeout_original_handler')

    def start(self):
        try:
            return self._start()
        except (RuntimeError,TimeoutError,OSError,KeyError,ValueError)as exc:
            if isinstance(exc,DisplayClockProbeError):
                raise
            self._fail('Cannot start TEST diagnostic: '+str(exc))

    def _start(self):
        if self._started:
            raise DisplayClockProbeError('Use a fresh probe for a separate TEST experiment')
        self._started=True
        status=self.emu.qmp.command('query-status',timeout=2)
        if status.get('running')is not False:
            self._fail('Stop all guest CPUs before enabling TEST inputs')
        if self._get('display-test-mode')is not False:
            self._fail('TEST input mode is already owned/enabled elsewhere')
        self._old_mask=self._get('display-test-status-mask')
        if type(self._old_mask)is not int or self._old_mask not in range(4):
            self._fail('Unsupported TEST status-mask property')
        initial=self._state()
        self.report['initial_state']=initial
        if initial != {'lcd_state':1,'avio_completion':0,'callback':hex(CALLBACK),
                       'selector':0,'bank0_dirty':1,'bank0_state':0}:
            self._fail('Require the observed LCDstate1/AVIO0 callback and dirty bank0 baseline')
        gic=self._json('gic-state-json')
        irq={item['id']:item for item in gic['enabled_or_asserted_irqs']}
        cpu0=gic['cpus'][0]
        self.report['initial_gic']={'distributor_control':gic['distributor_control'],
                                   'cpu0':cpu0,'irqs':{str(i):irq.get(i)for i in (196,232)}}
        if not(gic['distributor_control']&1 and cpu0['control']&1):
            self._fail('Original GIC distributor/CPU0 interface not enabled')
        for identifier in (196,232):
            item=irq.get(identifier)
            if (not item or not item['enabled']&1 or item['targets']!=1
                    or item['priority_cpu0']>=cpu0['priority_mask']
                    or any(item[key]for key in ('pending','active','level'))):
                self._fail('Original GIC IRQ route/mask/state differs from the isolated TEST baseline')
        model=self._json('display-test-state-json')
        if model['enabled'] or model['automatic_timing']or model['hardware_clock_verified']:
            self._fail('Expected default-off TEST-only model contract')
        self._last_counts=dict(model['pulses'])
        self.report['model_before']=model
        # Ownership is set before setters, so any partial setup gets disabled.
        self._owns_inputs=True
        try:
            self._set('display-test-status-mask',3)
            self._set('display-test-mode',True)
            if self._get('display-test-mode')is not True or self._get('display-test-status-mask')!=3:
                self._fail('TEST enable/status readback differs')
            self._add(PRIMARY_CHECK)
            self._pulse(196,'wait_original_state2')
        except (RuntimeError,TimeoutError,OSError)as exc:
            if isinstance(exc,DisplayClockProbeError):
                raise
            self._fail('Cannot start TEST sequence: '+str(exc))

    def _pulse(self, irq, stage):
        if len(self.report['pulses'])>=3 or irq not in (196,232):
            self._fail('TEST sequence pulse limit or IRQ allowlist violated')
        self._stage=stage
        self._seen_isr=False
        self._add(PRIMARY_ISR if irq==196 else SECONDARY_ISR)
        before=dict(self._last_counts)
        self._set('display-test-pulse',irq)
        model=self._json('display-test-state-json')
        after=model['pulses']
        expected={key:value+(key==str(irq))for key,value in before.items()}
        if after!=expected or not model['enabled']or model['status_mask']!=3:
            self._fail('Model did not account exactly one requested real GIC pulse')
        self._last_counts=dict(after)
        self.report['pulses'].append({'irq':irq,'stage':stage,'counts':after,
                                      'test_status_mask':3,'physical_timing_unverified':True})
        self.report['status']=stage
        self._deadline=time.monotonic()+self.stage_timeout

    def handle(self, pc, cpu):
        try:
            return self._handle(pc,cpu)
        except (RuntimeError,TimeoutError,OSError,KeyError,ValueError)as exc:
            if isinstance(exc,DisplayClockProbeError):
                raise
            self._fail('Cannot continue TEST diagnostic: '+str(exc))

    def _handle(self, pc, cpu):
        pc&=~1
        if pc not in self._breakpoints:
            return False
        self.check_timeout()
        if self.emu.selected_cpu!=cpu or cpu!=0:
            self._fail('Original TEST IRQ must be observed on its configured CPU0')
        regs=self.emu.registers()
        if regs['r15']!=pc:
            self._fail('Stopped PC does not match the owned observation')
        state=self._state()
        event={'pc':hex(pc),'cpu':cpu,'cpsr':hex(regs['cpsr']),
               'stage':self._stage,'original_state':state}
        if pc in (PRIMARY_ISR,SECONDARY_ISR):
            expected=PRIMARY_ISR if self._stage=='wait_original_state2'else SECONDARY_ISR
            if pc!=expected or self._seen_isr:
                self._fail('Unexpected/repeated original ISR entry')
            self._seen_isr=True
            event['kind']='original_gic_isr_entered'
            self.report['events'].append(event)
            self._remove(pc)
            return True
        if not self._seen_isr or state['selector']!=0:
            self._fail('Original ISR was not observed or selected completion source changed')
        if pc==PRIMARY_CHECK:
            expected={'lcd_state':2,'avio_completion':0,'callback':hex(CALLBACK)}
            if (self._stage!='wait_original_state2' or any(state[k]!=v for k,v in expected.items())
                    or state['bank0_dirty']!=0):
                self._fail('Original IRQ196 did not commit shadow config and advance state1->2')
            event['kind']='original_primary_state2'
            self.report['events'].append(event)
            self._remove(PRIMARY_CHECK)
            self._add(SECONDARY_CHECK)
            self._pulse(232,'wait_original_state3')
            return True
        wanted=3 if self._stage=='wait_original_state3'else 4
        if self._stage not in ('wait_original_state3','wait_original_state4'):
            self._fail('Unexpected secondary handler stage')
        expected={'lcd_state':wanted,'avio_completion':wanted-2,
                  'callback':hex(CALLBACK)if wanted==3 else '0x0'}
        if any(state[k]!=v for k,v in expected.items()):
            self._fail('Original secondary handler/callback did not produce the expected next state')
        event['kind']='original_secondary_state'+str(wanted)
        self.report['events'].append(event)
        self._remove(SECONDARY_CHECK)
        if wanted==3:
            # Step the actual instruction so the same code breakpoint can be
            # reused for the next IRQ, without modifying PC or guest context.
            try:
                self.emu.step(timeout=min(.5,self.timeout_hint()))
                self._add(SECONDARY_CHECK)
                self._pulse(232,'wait_original_state4')
            except (RuntimeError,TimeoutError,OSError)as exc:
                if isinstance(exc,DisplayClockProbeError):
                    raise
                self._fail('Cannot continue original secondary handler: '+str(exc))
        else:
            self.report['final_state']=state
            self.report['model_after']=self._json('display-test-state-json')
            if not self._cleanup():
                self._fail('Cannot restore TEST inputs off','cleanup_failed')
            self._completed=True
            self.report.update(status='original_lcd_callback_chain_observed_under_test_inputs',
                               original_callback9_observed_by_result=True)
        return True

    def close(self):
        """Restore owned TEST settings; leave real pending IRQ handling alone."""
        if self._started:
            self._cleanup()
        self.report['closed']=True
