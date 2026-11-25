#!/usr/bin/env python3
"""Explicit bounded source-frame IRQ TEST; no pixels, MMIO or guest RAM supplied.

Default construction/close have no I/O. start() requires a stopped guest and
naturally established original initialization flags. Each pair requests IRQ192
then193 through the QEMU GIC input, observing the original ISR and its actual
LR return on CPU0. Timing, sensor operation, frame validity and menu completion
are not claimed. The caller resumes CPUs and retains an outer process timeout.
"""
import json
import struct
import time

ISR={192:0x06497252,193:0x0649727c}
RECEIVE_RETURN, CONSUMER_DONE, CONSUMER_LOOP = 0x06705574, 0x067055ba, 0x06705560
ACK_ENTRY, ACK_SENT = 0x06759710, 0x06759766

class FrameIRQProbeError(RuntimeError):
    """Stop the bounded TEST and preserve the original guest execution state."""

class FrameIRQProbe:
    def __init__(self,emu,*,pairs=1,stage_timeout=2.0):
        if type(pairs)is not int or not 1<=pairs<=128:
            raise ValueError('Use one through128 explicit TEST IRQ pairs')
        if not 0<stage_timeout<=10:
            raise ValueError('Use a bounded stage timeout <=10 seconds')
        self.emu,self.pairs,self.stage_timeout=emu,pairs,stage_timeout
        self._started=self._owns_inputs=self._completed=False
        self._stage=self._deadline=self._entry=None
        self._breakpoints={}
        self._sequence=[192,193]*pairs
        self._position=0
        self._counts=None
        self._inflight=self._ack_call=None
        self._pair_completions=0
        self._pair_poll=0
        self._ack_sent=False
        self._observed_task67=None
        self.report={'scope':'Explicit TEST source IRQ192/193 inputs; no physical period, sensor, RGB pixels or complete camera emulation',
            'status':'not_started','pairs':pairs,'automatic_timing':False,
            'guest_ram_or_registers_written_by_host':False,'metadata_or_queue_messages_constructed_by_host':False,
            'guest_callback_invoked_by_host':False,'ui_or_dsp_ready_written_by_host':False,
            'menu_rendered_verified':False,'pulses':[],'events':[],
            'consumer_backpressure':True,'consumer_messages':[],'pair_consumption':[],
            'original_mode35_ack_enqueued':False,
            'pending_irq_cleanup':'Disabling TEST does not retract already pending GIC edges; this helper never clears GIC pending or active bits'}

    @property
    def active(self):return self._stage is not None
    @property
    def completed(self):return self._completed
    @property
    def breakpoints(self):return frozenset(self._breakpoints)

    def _get(self,name):
        return self.emu.qmp.command('qom-get',{'path':'/machine','property':name},timeout=2)
    def _set(self,name,value):
        return self.emu.qmp.command('qom-set',{'path':'/machine','property':name,'value':value},timeout=2)
    def _json(self,name):
        value=self._get(name)
        return json.loads(value)if isinstance(value,str)else value
    def _u32(self,address):return struct.unpack('<I',self.emu.read_mem(address,4))[0]
    def _byte(self,address):return self.emu.read_mem(address,1)[0]
    def _state(self):
        q=self._u32(0x08e1b94c+(0x77-1)*4)
        if not 0x08000000<=q<0x1fffffc0:
            raise FrameIRQProbeError('Original queue77 object is outside validated guest RAM')
        return {'init_ready':self._byte(0x08aa7f19),'postinit_ready':self._byte(0x08aa02af),
            'source_latch':self._byte(0x08aa0289),'previous_source_latch':self._byte(0x08aa02ac),
            'mode_flag':self._byte(0x08a9da70),'source_inhibit':self._byte(0x08aa032a),
            'sampled_gate_vector':list(self.emu.read_mem(0x08aa02c8,5)),
            'sequence_index':struct.unpack('<H',self.emu.read_mem(0x08aa7f1e,2))[0],
            'poll_counter':struct.unpack('<H',self.emu.read_mem(0x08aac26c,2))[0],
            'mode35_ack':self._byte(0x08aa1103),'controller_state':self._u32(0x08aac2e4),
            'controller_reply_received':self._byte(0x08aac2f9),
            'queue77':{'object':hex(q),'count':self._u32(q+0x18),
                'read_index':self._u32(q+0x14),'write_index':self._u32(q+0x10)}}
    def _add(self,address,kind=2):
        if address not in self._breakpoints:
            self.emu.breakpoint_add(address,kind=kind);self._breakpoints[address]=kind
    def _remove(self,address):
        if address in self._breakpoints:
            self.emu.breakpoint_remove(address,kind=self._breakpoints[address]);del self._breakpoints[address]
    def _cleanup(self):
        errors=[]
        if self._owns_inputs:
            try:
                self._set('frame-test-mode',False)
                self.report['inputs_restored_off']=self._get('frame-test-mode')is False
                if not self.report['inputs_restored_off']:raise RuntimeError('TEST disable readback differs')
                self._owns_inputs=False
            except (RuntimeError,TimeoutError,OSError)as exc:errors.append(str(exc))
        for pc in tuple(self._breakpoints):
            try:self._remove(pc)
            except (RuntimeError,TimeoutError,OSError)as exc:errors.append(str(exc))
        self._stage=self._deadline=None
        if errors:self.report.setdefault('cleanup_errors',[]).extend(errors)
        return not errors
    def _fail(self,message,status='test_sequence_stopped'):
        self.report.update(status=status,error=message);self._cleanup()
        raise FrameIRQProbeError(message)
    def timeout_hint(self):
        return 30.0 if self._deadline is None else max(.001,self._deadline-time.monotonic())
    def check_timeout(self):
        if self._deadline is not None and time.monotonic()>=self._deadline:
            self._fail('Original ISR stage did not complete before deadline','timeout_original_isr')
    def _guard_source(self,state):
        # The first real IRQ may set source_latch and produce gate[4]=1, which
        # intentionally skips that first service pass. Never force that latch.
        if (not state['init_ready']or not state['postinit_ready']or state['mode_flag']
            or state['source_inhibit']or state['source_latch']not in (0,1)
            or state['previous_source_latch']not in (0,1)):
            self._fail('Original initialization/source gates are not ready','guard_rejected')
    def _gic(self,ids):
        g=self._json('gic-state-json');cpu=g['cpus'][0]
        entries={x['id']:x for x in g['enabled_or_asserted_irqs']}
        if not(g['distributor_control']&1 and cpu['control']&1):
            self._fail('Original GIC/CPU0 interface is disabled','guard_rejected')
        for irq in ids:
            v=entries.get(irq)
            if(not v or not v['enabled']&1 or v['targets']!=1
               or v['priority_cpu0']>=cpu['priority_mask']
               or any(v[k]for k in ('pending','active','level'))):
                self._fail('Original source IRQ route/mask/pending differs','guard_rejected')
        return {'cpu0':cpu,'irqs':{str(i):entries[i]for i in ids}}
    def start(self):
        try:self._start()
        except (RuntimeError,TimeoutError,OSError,KeyError,ValueError)as exc:
            if isinstance(exc,FrameIRQProbeError):raise
            self._fail('Cannot start source IRQ TEST: '+str(exc),'guard_rejected')
    def _start(self):
        if self._started:raise FrameIRQProbeError('Use a fresh probe for a new TEST')
        self._started=True
        if self.emu.qmp.command('query-status',timeout=2).get('running')is not False:
            self._fail('Stop all CPUs before enabling source IRQ TEST','guard_rejected')
        if self._get('frame-test-mode')is not False:
            self._fail('Source TEST is already enabled by another owner','guard_rejected')
        state=self._state();self.report['initial_state']=state;self._guard_source(state)
        if state['controller_reply_received']!=1:
            self._fail('Original controller40001 reply has not been consumed','guard_rejected')
        self.report['initial_gic']=self._gic((192,193))
        model=self._json('frame-test-state-json')
        if model['enabled']or model['automatic_timing']is not False or set(model['pulses'])!={'192','193'}:
            self._fail('Unexpected default-off source TEST model contract','guard_rejected')
        self._counts=dict(model['pulses']);self.report['model_before']=model
        self._owns_inputs=True;self._set('frame-test-mode',True)
        if self._get('frame-test-mode')is not True:self._fail('Source TEST enable readback differs')
        for pc in (RECEIVE_RETURN,CONSUMER_DONE,CONSUMER_LOOP,ACK_ENTRY,ACK_SENT):self._add(pc)
        self._pulse()
    def _pulse(self):
        irq=self._sequence[self._position]
        if irq==192:
            self._pair_completions=0
            self._pair_poll=self._state()['poll_counter']
        self._guard_source(self._state());self._gic((irq,))
        self._entry=None;self._add(ISR[irq]);self._stage='wait_isr_entry'
        self._set('frame-test-pulse',irq)
        model=self._json('frame-test-state-json')
        expected={k:v+(k==str(irq))for k,v in self._counts.items()}
        if model['pulses']!=expected or not model['enabled'] or model['automatic_timing']is not False:
            self._fail('Model did not record exactly one requested source GIC pulse')
        self._counts=dict(model['pulses'])
        self.report['pulses'].append({'irq':irq,'pair':self._position//2+1,'counts':dict(self._counts)})
        self.report['status']=self._stage;self._deadline=time.monotonic()+self.stage_timeout
    def _finish(self,status):
        self.report['final_state']=self._state();self.report['model_after']=self._json('frame-test-state-json')
        if not self._cleanup():self._fail('Could not disable source TEST','cleanup_failed')
        self._completed=True;self.report['status']=status
    def _step_owned(self,pc):
        kind=self._breakpoints[pc];self._remove(pc)
        try:self.emu.step(timeout=min(.5,self.timeout_hint()))
        finally:
            if self.active:self._add(pc,kind)
    def _task67(self,cpu,r):
        tcb=self._u32(0x20040+cpu*4)
        if not 0x08000000<=tcb<0x1ffffeb0:return None
        if (self.emu.read_mem(tcb,4)!=b'DRHT'or self._u32(tcb+0xc0)!=67
            or self._u32(tcb+0xd8)!=0x06705545):return None
        if (r['cpsr']&0x3f!=0x33 or r['cpsr']&0x0600fc00
            or not self._u32(tcb+0xc)<=r['r13']<=self._u32(tcb+0x10)-20):
            self._fail('Original frame consumer stack or task context differs')
        if self._observed_task67 not in (None,tcb):
            self._fail('Original frame task identity unexpectedly changed')
        self._observed_task67=tcb
        return tcb
    def _consumer(self,pc,cpu,r):
        tcb=self._task67(cpu,r)
        if tcb is None:
            self._step_owned(pc);return True
        state=self._state()
        if pc==RECEIVE_RETURN:
            if r['r0']==0:
                if self._inflight:self._fail('Original consumer received again before finishing previous message')
                words=struct.unpack('<5I',self.emu.read_mem(r['r13'],20))
                self._inflight={'cpu':cpu,'tcb':hex(tcb),'sp':r['r13'],
                    'pair':self._position//2+(self._stage!='wait_consumer_drain'),
                    'words':[hex(x)for x in words],
                    'poll_before':state['poll_counter']}
        elif pc==CONSUMER_DONE:
            item=self._inflight
            if not item or item['cpu']!=cpu or item['sp']!=r['r13']:
                self._fail('Original frame completion lacks matching receive/context')
            item.update(poll_after=state['poll_counter'],queue_count_after=state['queue77']['count'],
                        completed_pc=hex(pc))
            self.report['consumer_messages'].append(item)
            self._inflight=None;self._pair_completions+=1
        elif pc==CONSUMER_LOOP and self._stage=='wait_consumer_drain':
            if self._maybe_drain(state,cpu,hex(tcb),r['r13']):return True
        self._step_owned(pc);return True
    def _maybe_drain(self,state,cpu,tcb,sp):
        if self._inflight or not self._pair_completions or state['queue77']['count']:
            return False
        progressed=state['poll_counter']!=self._pair_poll
        ack_complete=state['mode35_ack']&15==15
        transitioned=(state['controller_state']==3 and self._byte(0x08aac2f8)==0)
        if not(progressed or ack_complete or self._ack_sent or transitioned):return False
        self.report['pair_consumption'].append({'pair':self._position//2,
            'cpu':cpu,'tcb':tcb,'sp':hex(sp),'completed_messages':self._pair_completions,
            'poll_before':self._pair_poll,'poll_after':state['poll_counter'],
            'queue77':state['queue77'],'mode35_ack':state['mode35_ack'],
            'at_original_receive_loop':True})
        if ack_complete:self._finish('original_mode35_ack_consumed_stop_test_inputs')
        elif self._ack_sent:self._finish('original_mode35_ack_enqueued_ui_pending')
        elif transitioned:self._finish('controller_transition_observed_stop_test_inputs')
        elif self._position>=len(self._sequence):self._finish('original_source_isrs_and_frame_consumers_completed')
        else:self._pulse()
        return True
    def _ack_observer(self,pc,cpu,r):
        tcb=self._task67(cpu,r)
        if tcb is not None and pc==ACK_ENTRY and tuple(r['r'+str(i)]for i in range(3))==(0x35,1,0):
            index=self._u32(0x08aab9d4)
            if index>=128:self._fail('Original ACK ring index is outside capacity')
            self._ack_call={'cpu':cpu,'tcb':tcb,'pointer':0x08e9f518+index*20}
            self.report['events'].append({'kind':'original_mode35_ack_entry','cpu':cpu,'pc':hex(pc),
                'arguments':[0x35,1,0],'ring_pointer':hex(self._ack_call['pointer'])})
        elif pc==ACK_SENT and self._ack_call and tcb==self._ack_call['tcb'] and cpu==self._ack_call['cpu']:
            ptr=self._ack_call['pointer'];words=struct.unpack('<5I',self.emu.read_mem(ptr,20))
            if r['r4']!=ptr or words!=(0x76,0x42,0x35,0,1)or r['r0']!=0:
                self._fail('Observed original mode35 ACK enqueue failed or packet differs')
            self._ack_sent=True;self.report['original_mode35_ack_enqueued']=True
            self.report['events'].append({'kind':'original_mode35_ack_enqueue_return','cpu':cpu,'pc':hex(pc),
                'return_r0':0,'packet':[hex(x)for x in words]})
            self._ack_call=None
        self._step_owned(pc);return True
    def _return_site(self,lr):
        pc=lr&~1;kind=2 if lr&1 else 4
        if not 0x06280004<=pc<0x07267000 or pc&(kind-1):
            self._fail('Original ISR LR is outside aligned original program code')
        # Accept only a real preceding BLX(register), not an arbitrary return
        # pointer. IRQ dispatch is indirect and the instruction never changes.
        raw=self.emu.read_mem(pc-kind,kind);ins=int.from_bytes(raw,'little')
        valid=(ins&0xff87==0x4780)if kind==2 else(ins&0x0ffffff0==0x012fff30)
        if not valid:self._fail('ISR LR is not immediately after an original BLX(register)')
        return pc,kind,raw.hex()
    def handle(self,pc,cpu):
        try:return self._handle(pc,cpu)
        except (RuntimeError,TimeoutError,OSError,KeyError,ValueError)as exc:
            if isinstance(exc,FrameIRQProbeError):raise
            self._fail('Cannot observe source IRQ TEST: '+str(exc))
    def _foreign_return(self,pc,cpu,regs):
        # The dispatcher return site is shared by IRQs and CPUs. Execute its
        # actual instruction on a foreign stop and re-arm without changing PC.
        count=self.report.get('foreign_return_stops',0)+1
        self.report['foreign_return_stops']=count
        self.report['last_foreign_return']={'cpu':cpu,'sp':hex(regs['r13']),
                                           'cpsr':hex(regs['cpsr'])}
        if count>max(256,self.pairs*32):self._fail('Shared return-site observation limit exceeded')
        kind=self._breakpoints[pc];self._remove(pc)
        try:self.emu.step(timeout=min(.5,self.timeout_hint()))
        finally:
            if self.active:self._add(pc,kind)
        return True
    def _handle(self,pc,cpu):
        pc&=~1
        if pc not in self._breakpoints:return False
        self.check_timeout()
        if self.emu.selected_cpu!=cpu:self._fail('Caller must select the stopped CPU')
        r=self.emu.registers()
        if r['r15']!=pc:self._fail('Stopped PC differs from owned observer')
        if pc in (RECEIVE_RETURN,CONSUMER_DONE,CONSUMER_LOOP):return self._consumer(pc,cpu,r)
        if pc in (ACK_ENTRY,ACK_SENT):return self._ack_observer(pc,cpu,r)
        if self._stage=='wait_isr_return' and pc==self._entry['return_pc']:
            e=self._entry
            if(cpu!=0 or r['r13']!=e['sp']or r['cpsr']&0x1f!=0x12
               or bool(r['cpsr']&0x20)!=(e['return_kind']==2)):
                return self._foreign_return(pc,cpu,r)
        if cpu!=0:self._fail('Source ISR entry observed on unexpected CPU')
        if r['r15']!=pc or r['cpsr']&0x1f!=0x12 or r['cpsr']&0x0600fc00:
            self._fail('Expected original IRQ context with matching PC and no IT block')
        irq=self._sequence[self._position]
        if self._stage=='wait_isr_entry':
            if pc!=ISR[irq]or not r['cpsr']&0x20:self._fail('Unexpected original source ISR entry')
            ret,kind,opcode=self._return_site(r['r14'])
            self._entry={'return_pc':ret,'return_kind':kind,'sp':r['r13'],'lr':r['r14']}
            self.report['events'].append({'kind':'original_isr_entry','irq':irq,'pc':hex(pc),
                'cpu':cpu,'sp':hex(r['r13']),'lr':hex(r['r14']),'cpsr':hex(r['cpsr']),
                'caller_blx_bytes':opcode,'state':self._state()})
            self._remove(pc);self._add(ret,kind)
            self._stage='wait_isr_return';self.report['status']=self._stage
            self._deadline=time.monotonic()+self.stage_timeout
            return True
        e=self._entry
        if(self._stage!='wait_isr_return'or pc!=e['return_pc']or r['r13']!=e['sp']
           or bool(r['cpsr']&0x20)!=(e['return_kind']==2)):
            self._fail('Original ISR return CPU/stack/mode does not match entry')
        self.report['events'].append({'kind':'original_isr_return','irq':irq,'pc':hex(pc),
            'cpu':cpu,'sp':hex(r['r13']),'cpsr':hex(r['cpsr']),'state':self._state()})
        self._remove(pc);self._position+=1
        if irq==193 and self._position>=4:
            self._stage='wait_consumer_drain';self.report['status']=self._stage
            self._deadline=time.monotonic()+self.stage_timeout
            # A consumer on another CPU may already have completed and blocked
            # again before the source ISR returns. Require both observed work
            # and actual queue wait, rather than waiting for a nonexistent stop.
            tcb=self._observed_task67;state=self._state()
            if(tcb is not None and self._u32(tcb+0xc0)==67 and self._u32(tcb+0xd8)==0x06705545
               and self._u32(tcb+0xec)==4 and self._u32(tcb+0x108)==int(state['queue77']['object'],16)):
                self._maybe_drain(state,None,hex(tcb),self._u32(tcb+8))
        elif self._position<len(self._sequence):self._pulse()
        else:self._finish('original_source_isrs_returned_under_test_inputs')
        return True
    def close(self):
        if self._started:self._cleanup()
        self.report['closed']=True
