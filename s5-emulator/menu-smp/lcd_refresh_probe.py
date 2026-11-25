#!/usr/bin/env python3
"""Bounded, request-gated LCD232 TEST input, not a physical display clock.

start() arms the original event86-set return boundary. With event86bit0 clear,
active is false: no TEST input is enabled and no polling deadline is needed.
After an original request, one GIC232 pulse is observed through its real ISR,
its original LR return, and task18's actual GUI callback/send/return. No guest
memory, register, queue or callback is written/called by the host. The caller
owns all CPU resume and the experiment's overall deadline.
"""
import json
import struct
import time

REQUEST = 0x0727ca5e
ISR = 0x06497338
CALLBACK = 0x0727cf58
SEND_RETURN = 0x0727cf8e
CALLBACK_RETURN = 0x0727cf9a
DRAIN = 0x0647e85e  # Original task18 receive-loop boundary, before MOV r1,sp.


class LCDRefreshProbeError(RuntimeError):
    pass


class LCDRefreshProbe:
    def __init__(self, emu, *, max_pulses=8, stage_timeout=10.0):
        if type(max_pulses) is not int or not 1 <= max_pulses <= 64:
            raise ValueError('Use one through64 bounded request-gated pulses')
        if not 0 < stage_timeout <= 10:
            raise ValueError('Use a stage timeout <=10seconds')
        self.emu, self.max_pulses, self.stage_timeout = emu, max_pulses, stage_timeout
        self._bps = {}
        self._started = self._armed = self._owns = self._completed = False
        self._stage = self._deadline = self._old_mask = self._entry = self._callback = None
        self._irq_done = self._callback_done = False
        self.report = dict(scope='Request-gated explicit LCD232 TEST; no physical period, hardware clock, scanout or menu proof',
                           status='not_started', max_pulses=max_pulses, automatic_timing=False,
                           guest_ram_or_registers_written_by_host=False,
                           guest_callback_invoked_by_host=False, queue_messages_created_by_host=False,
                           pulses=[], events=[], completed_requests=0,
                           pending_irq_cleanup='Disabling TEST does not retract pending GIC edges; no GIC pending/active bits are manually cleared')

    @property
    def active(self):
        return self._stage is not None

    @property
    def armed(self):
        return self._armed

    @property
    def completed(self):
        return self._completed

    @property
    def breakpoints(self):
        return frozenset(self._bps)

    def _get(self, name):
        return self.emu.qmp.command('qom-get', {'path':'/machine','property':name}, timeout=2)

    def _set(self, name, value):
        return self.emu.qmp.command('qom-set', {'path':'/machine','property':name,'value':value}, timeout=2)

    def _json(self, name):
        v = self._get(name)
        return json.loads(v) if isinstance(v, str) else v

    def _u32(self, address):
        return struct.unpack('<I', self.emu.read_mem(address, 4))[0]

    def _state(self):
        eid = self._u32(0x06f498d0)
        if eid != 0x86:
            self._fail('Original GUI event ID differs')
        obj = self._u32(0x08e18d80+(eid-1)*4)
        q = self._u32(0x08e1b94c+(15-1)*4)
        if not all(0x08000000 <= p < 0x20000000-0x40 for p in (obj, q)):
            self._fail('Original event/queue object outside verified RAM')
        return dict(event_object=hex(obj), event_flags=self._u32(obj+8),
                    callback=hex(self._u32(0x08a9d610)), lcd=self._u32(0x08ab488c),
                    avio=self._u32(0x08a9c7f8), selector=self.emu.read_mem(0x08ab1bba,1)[0],
                    q15_count=self._u32(q+0x18),
                    queued_type3=struct.unpack('<H',self.emu.read_mem(0x08a9d606,2))[0])

    def _add(self, pc, kind=2):
        if pc not in self._bps:
            self.emu.breakpoint_add(pc, kind=kind)
            self._bps[pc] = kind

    def _remove(self, pc):
        if pc in self._bps:
            self.emu.breakpoint_remove(pc, kind=self._bps[pc])
            del self._bps[pc]

    def _inputs_off(self):
        if self._owns:
            self._set('display-test-mode', False)
            self._set('display-test-status-mask', self._old_mask)
            if self._get('display-test-mode') is not False or self._get('display-test-status-mask') != self._old_mask:
                raise LCDRefreshProbeError('TEST input restoration readback differs')
            self._owns = False

    def _cleanup(self):
        errors = []
        try:
            self._inputs_off()
        except (RuntimeError, TimeoutError, OSError) as exc:
            errors.append(str(exc))
        for pc in tuple(self._bps):
            try:
                self._remove(pc)
            except (RuntimeError, TimeoutError, OSError) as exc:
                errors.append(str(exc))
        self._stage = self._deadline = None
        self._armed = False
        if errors:
            self.report.setdefault('cleanup_errors', []).extend(errors)
        return not errors

    def _fail(self, message):
        self.report.update(status='diagnostic_stopped', error=message)
        self._cleanup()
        raise LCDRefreshProbeError(message)

    def timeout_hint(self):
        return 30.0 if self._deadline is None else max(.001, self._deadline-time.monotonic())

    def check_timeout(self):
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._fail('Original LCD ISR/callback did not finish before bounded deadline')

    def start(self):
        if self._started:
            raise LCDRefreshProbeError('Use a new helper for another experiment')
        self._started = True
        try:
            if self.emu.qmp.command('query-status', timeout=2).get('running') is not False:
                self._fail('Stop all CPUs before arming LCD refresh TEST')
            # Exact original request boundary and callback prologue, no patching.
            if self.emu.read_mem(REQUEST,2) != bytes.fromhex('45e7') or self.emu.read_mem(CALLBACK,2) != bytes.fromhex('7cb5'):
                self._fail('Original GUI request/callback opcodes differ')
            state = self._state()
            self._guard(state)
            self.report['initial_state'] = state
            self._armed = True
            self._add(REQUEST)
            self.report['status'] = 'armed_waiting_original_event86_request'
            if state['event_flags'] & 1:
                self._request(state)
        except (RuntimeError, TimeoutError, OSError, KeyError, ValueError) as exc:
            if isinstance(exc, LCDRefreshProbeError):
                raise
            self._fail('Cannot arm LCD refresh TEST: '+str(exc))

    def _guard(self, state):
        if state['callback'] != hex(CALLBACK|1) or state['lcd'] != 4 or state['avio'] != 2 or state['selector'] != 0:
            self._fail('Require original LCD4/AVIO2, selector232 and registered GUI callback')

    def _request(self, state):
        # Existing original queue work may already deliver the requested
        # callback. Observe its natural drain; do not fabricate a second IRQ
        # or stop a valid run merely because its queue is not empty yet.
        if state['q15_count'] or state['queued_type3']:
            if self.emu.read_mem(DRAIN,2) != bytes.fromhex('6946'):
                self._fail('Original task18 receive-loop opcode differs')
            self._remove(REQUEST)
            self._add(DRAIN)
            self.report['status'] = 'passive_waiting_existing_display_work'
            self.report['events'].append(dict(kind='deferred_for_original_queue_work',state=state))
            return
        self._remove(DRAIN)
        self._pulse(state)

    def _step_drain(self):
        # Execute exactly the original MOV r1,sp before reinstalling its
        # observer. This only avoids retrapping the same untouched opcode.
        count=self.report.get('passive_receive_observations',0)+1
        self.report['passive_receive_observations']=count
        if count>256:
            self._fail('Bounded observation of existing display work exhausted')
        self._remove(DRAIN)
        try:self.emu.step(timeout=.5)
        finally:
            if self.armed and not self.active:self._add(DRAIN)

    def _pulse(self, state):
        self._guard(state)
        if not state['event_flags'] & 1:
            self._fail('Refusing pulse without original event86bit0 request')
        if state['q15_count'] or state['queued_type3']:
            self._fail('Existing display queue work prevents attributing a new TEST callback')
        if len(self.report['pulses']) >= self.max_pulses:
            self._fail('Request pulse bound exceeded')
        if self._get('display-test-mode') is not False:
            self._fail('Display TEST already owned by another diagnostic')
        g = self._json('gic-state-json')
        cpu = g['cpus'][0]
        irq = {v['id']:v for v in g['enabled_or_asserted_irqs']}.get(232)
        if (not(g['distributor_control']&1 and cpu['control']&1) or not irq
                or not irq['enabled']&1 or irq['targets'] != 1
                or irq['priority_cpu0'] >= cpu['priority_mask']
                or any(irq[k] for k in ('pending','active','level'))):
            self._fail('Original GIC232 routing/masking/pending state differs')
        model = self._json('display-test-state-json')
        if model['enabled'] or model['automatic_timing'] or model['hardware_clock_verified']:
            self._fail('Expected default-off explicit TEST model')
        self._old_mask = self._get('display-test-status-mask')
        if type(self._old_mask) is not int or self._old_mask not in range(4):
            self._fail('Unsupported TEST mask property')
        self._entry = self._callback = None
        self._irq_done = self._callback_done = False
        self._remove(REQUEST)
        for pc in (ISR,CALLBACK,SEND_RETURN,CALLBACK_RETURN):
            self._add(pc)
        self._stage = 'waiting_original_isr_and_callback'
        self._deadline = time.monotonic()+self.stage_timeout
        self._owns = True
        self._set('display-test-status-mask', 3)
        self._set('display-test-mode', True)
        self._set('display-test-pulse', 232)
        after = self._json('display-test-state-json')
        expected = {k:v+(k=='232') for k,v in model['pulses'].items()}
        if after['pulses'] != expected or not after['enabled'] or after['status_mask'] != 3:
            self._fail('Model did not record exactly one requested GIC232 pulse')
        self.report['pulses'].append(dict(irq=232, request_state=state, counts=after['pulses'], test_status_mask=3))
        self.report['status'] = self._stage

    def _task18(self, cpu, regs):
        tcb = self._u32(0x20040+cpu*4)
        if not 0x08000000 <= tcb < 0x20000000-0x150:
            self._fail('GUI callback is outside original task RAM')
        if (self.emu.read_mem(tcb,4) != b'DRHT' or self._u32(tcb+0xc0) != 18
                or self._u32(tcb+0xd8) != 0x0647e83f
                or regs['cpsr']&0x3f != 0x33 or regs['cpsr']&0x0600fc00
                or not self._u32(tcb+0xc) <= regs['r13'] <= self._u32(tcb+0x10)):
            self._fail('GUI callback must run in the actual original task18 context')
        return tcb

    def _step_shared(self, pc):
        n = self.report.get('foreign_isr_return_stops',0)+1
        self.report['foreign_isr_return_stops'] = n
        if n > self.max_pulses*128:
            self._fail('Shared IRQ-return observation bound exceeded')
        kind = self._bps[pc]
        self._remove(pc)
        try:
            self.emu.step(timeout=min(.5,self.timeout_hint()))
        finally:
            if self.active:
                self._add(pc,kind)

    def handle(self, pc, cpu):
        pc &= ~1
        if pc not in self._bps:
            return False
        try:
            self.check_timeout()
            if self.emu.selected_cpu != cpu:
                self._fail('Caller must select the stopped CPU')
            regs = self.emu.registers()
            if regs['r15'] != pc:
                self._fail('Stopped PC differs from owned observer')
            state = self._state()
            if pc == DRAIN:
                self._task18(cpu,regs)
                self._guard(state)
                if not state['event_flags']&1:
                    self.report['events'].append(dict(kind='original_request_cleared_while_draining',cpu=cpu,state=state))
                    self._remove(DRAIN)
                    self._add(REQUEST)
                    self.report['status']='armed_waiting_original_event86_request'
                elif state['q15_count'] or state['queued_type3']:
                    self._step_drain()
                else:
                    self.report['events'].append(dict(kind='original_queue_drained_request_still_pending',cpu=cpu,state=state))
                    self._remove(DRAIN)
                    self._pulse(state)
                return True
            if pc == REQUEST:
                self.report['events'].append(dict(kind='original_event86_set_return',cpu=cpu,r0=regs['r0'],state=state))
                if regs['r0'] != 0:
                    self._fail('Original event86 set returned an error')
                self._request(state)
                return True
            if pc == ISR:
                if cpu != 0 or regs['cpsr']&0x3f != 0x32 or regs['cpsr']&0x0600fc00:
                    self._fail('Expected original Thumb IRQ232 on CPU0')
                ret = regs['r14'] & ~1
                kind = 2 if regs['r14']&1 else 4
                if not 0x06280004 <= ret < 0x07267000 or ret&(kind-1):
                    self._fail('Original ISR LR outside aligned original code')
                ins = int.from_bytes(self.emu.read_mem(ret-kind,kind),'little')
                if not ((ins&0xff87 == 0x4780) if kind==2 else (ins&0x0ffffff0 == 0x012fff30)):
                    self._fail('Original ISR LR does not follow BLX(register)')
                self._entry = dict(pc=ret,kind=kind,sp=regs['r13'])
                self.report['events'].append(dict(kind='original_isr_entry',cpu=cpu,lr=hex(regs['r14']),sp=hex(regs['r13'])))
                self._remove(pc)
                self._add(ret,kind)
            elif self._entry and pc == self._entry['pc']:
                e = self._entry
                if cpu != 0 or regs['r13'] != e['sp'] or regs['cpsr']&0x1f != 0x12 or bool(regs['cpsr']&0x20) != (e['kind']==2):
                    self._step_shared(pc)
                    return True
                self._irq_done = True
                self.report['events'].append(dict(kind='original_isr_return',cpu=cpu,pc=hex(pc),sp=hex(regs['r13'])))
                self._remove(pc)
            else:
                tcb = self._task18(cpu,regs)
                if pc == CALLBACK:
                    if not state['event_flags']&1:
                        self._fail('Original callback no longer sees the requested event86bit0')
                    self._callback = dict(tcb=tcb,sp=regs['r13'],send_success=False)
                    self.report['events'].append(dict(kind='original_task18_callback_entry',cpu=cpu,tcb=hex(tcb),state=state))
                else:
                    call = self._callback
                    if not call or tcb != call['tcb'] or regs['r13'] != call['sp']-24:
                        self._fail('Original callback stack/TCB differs from observed entry')
                    if pc == SEND_RETURN:
                        msg = struct.unpack('<5I',self.emu.read_mem(0x0b392e78,20))
                        if regs['r0'] != 0 or msg != (0,0x10e,0x11f438,0,0):
                            self._fail('Original GUI callback did not enqueue its expected notification successfully')
                        call['send_success'] = True
                        self.report['events'].append(dict(kind='original_callback_queue115_send_success',cpu=cpu,message=[hex(x)for x in msg]))
                    elif pc == CALLBACK_RETURN:
                        if not call['send_success'] or state['event_flags']&1:
                            self._fail('Original callback did not clear its completed event86 request')
                        self._callback_done = True
                        self.report['events'].append(dict(kind='original_callback_return_boundary',cpu=cpu,state=state))
                self._remove(pc)
            if self._irq_done and self._callback_done:
                self._inputs_off()
                self.report['completed_requests'] += 1
                self._stage = self._deadline = None
                if len(self.report['pulses']) >= self.max_pulses:
                    self._completed = True
                    self._cleanup()
                    self.report['status'] = 'bounded_original_callbacks_completed'
                else:
                    self._add(REQUEST)
                    self.report['status'] = 'armed_waiting_original_event86_request'
            return True
        except (RuntimeError, TimeoutError, OSError, KeyError, ValueError) as exc:
            if isinstance(exc,LCDRefreshProbeError):
                raise
            self._fail('Cannot observe LCD refresh TEST: '+str(exc))

    def close(self):
        if self._started:
            self._cleanup()
        self.report['closed'] = True
