#!/usr/bin/env python3
"""One original queued mode1 service call during a partial scheduling experiment.

This is explicitly function-level execution before DSP initialization completes,
not a DSP model or a completed camera boot. Construction does nothing. The caller
must stop at PEER_WAIT in original task66, after restoring its delay-call context,
then call start(cpu). Forward owned stops to handle(pc,cpu), retain an outer
process deadline, and stop on UIServiceProbeError. On completed, the original
wait registers are restored; the caller can schedule another original dly call.

Default accepts only the actual queued {115,11d,36,1,0}. Explicit command=0x35
accepts {115,11d,35,1,0} under additional snapshot-derived object/state guards;
it may return with an asynchronous ACK still pending. completed means the
original call returned and its wait context was restored, not mode completion.
Original receive performs the
20-byte copy and pool6 release; original dispatch emits the acknowledgement.
No DSP state, UI flags, message data, firmware bytes or TCB fields are written by
this module. The original task's working message buffer retains the API's copy.
"""
import hashlib
import re
import struct
import time
import xml.etree.ElementTree as ET

PEER_WAIT = 0x065491ee
RECEIVE_API = 0x067ea67a
DISPATCH_API = 0x0675f154
MESSAGE_BUFFER = 0x0a603680
QUEUE_ID = 0x76
EXPECTED = (0x115, 0x11d, 0x36, 1, 0)
ACK_EXPECTED = (0x76, 0x42, 0x36, 0, 1)
QUEUE_TABLE = 0x08e1b94c
TASK_ID, TASK_ENTRY = 66, 0x0675ef85
GUARDS = (0x08ab4e80, 0x08ab4e7c, 0x08ab4e68)
POOL_RELEASE_CALL = 0x067ea6cc
POOL_RELEASE_RETURN = 0x067ea6d0
MODE_HANDLER = 0x0653b7e6
MODE35_HANDLER, MODE35_START, MODE35_NEXT = 0x0653b86c, 0x066d04dc, 0x067361a8
ACK_ENTRY, ACK_SEND_RETURN = 0x06759710, 0x06759766
RECEIVE_OBSERVERS = (POOL_RELEASE_CALL, POOL_RELEASE_RETURN)
DISPATCH_OBSERVERS = (MODE_HANDLER, ACK_ENTRY, ACK_SEND_RETURN)


class UIServiceProbeError(RuntimeError):
    """Stop the partial experiment; never force-restore an unknown context."""


class UIServiceProbe:
    def __init__(self, emu, *, return_address=0x0001bd00, stage_timeout=2.0, command=0x36):
        if (return_address & 1 or not 0x10000 <= return_address < 0x20000
                or return_address in (0x10800, 0x1be00, 0x1bf00)):
            raise ValueError('Reserve an even, distinct return trap in helper RAM')
        if not 0 < stage_timeout <= 10:
            raise ValueError('Each original call needs a bounded deadline <=10s')
        if type(command) is not int or command not in (0x36,0x35):
            raise ValueError('Only exact command36 service or explicit command35 diagnostic')
        self.emu, self.return_address = emu, return_address
        self.command = command
        self.expected = (0x115,0x11d,command,1,0)
        self.ack_expected = (0x76,0x42,command,0,1)
        self.mode_handler = MODE_HANDLER if command == 0x36 else MODE35_HANDLER
        self.dispatch_observers = ((self.mode_handler,ACK_ENTRY,ACK_SEND_RETURN) if command == 0x36
                                   else (MODE35_HANDLER,MODE35_START,MODE35_NEXT,ACK_ENTRY,ACK_SEND_RETURN))
        self.stage_timeout = stage_timeout
        self._breakpoints, self._saved = set(), None
        self._deadline = self._stage = self._fp_layout = None
        self._started = self._completed = False
        self.report = {
            'scope':'One original queued mode1 service call before DSP init completes; function-level local execution experiment',
            'status':'not_started', 'original_instructions_modified':False,
            'dsp_ready_or_magic_written':False, 'ui_or_tcb_fields_written':False,
            'message_fields_synthesized':False, 'original_wait_restored':False,
            'dsp_initialization_completed':False, 'menu_opened_verified':False,
            'events':[],
            'command':hex(command),
            'completed_meaning':'Original handler returned and wait context restored; not asynchronous mode completion',
        }
        if command == 0x35:
            self.report['scope'] = ('Explicit original command35 mode1 control-handler diagnostic before DSP init completes; '
                                    'may stop inside unsupported initialization or return with ACK pending')

    @property
    def active(self):
        return self._saved is not None

    @property
    def completed(self):
        return self._completed

    @property
    def breakpoints(self):
        return frozenset(self._breakpoints)

    def _u32(self, address):
        return struct.unpack('<I', self.emu.read_mem(address,4))[0]

    def _ram(self, address, size=4):
        return address % 4 == 0 and 0x08000000 <= address <= 0x20000000-size

    def _task(self, cpu):
        address = self._u32(0x20040+cpu*4)
        if not self._ram(address,0x150):
            self._fail('Current task pointer outside expected RAM')
        return address

    def _add(self, address):
        if address not in self._breakpoints:
            self.emu.breakpoint_add(address)
            self._breakpoints.add(address)

    def _remove(self, address):
        if address in self._breakpoints:
            self.emu.breakpoint_remove(address)
            self._breakpoints.remove(address)

    def _clear(self):
        for address in tuple(self._breakpoints):
            self._remove(address)

    def _fail(self, message, status=None):
        self.report.update(status=status or ('call_stopped' if self.active else 'guard_rejected'),
                           error=message, unsafe_to_force_context_restore=self.active)
        raise UIServiceProbeError(message)

    def timeout_hint(self):
        return 30.0 if self._deadline is None else max(.001,self._deadline-time.monotonic())

    def check_timeout(self):
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self._fail('Original '+str(self._stage)+' call did not return by its deadline',
                       'timeout_call_incomplete')

    def _snapshot_fp(self):
        target = self.emu.rsp.read_feature('target.xml')
        includes = re.findall(r'<xi:include\s+href="([^"]+)"', target)
        if includes[:3] != ['arm-core.xml','arm-neon.xml','arm-vfp-sysregs.xml']:
            self._fail('Unexpected XML register layout; no guessed VFP numbers')
        mapping, number, hashes = {}, 0, {}
        for filename in includes[:3]:
            xml = self.emu.rsp.read_feature(filename)
            hashes[filename] = hashlib.sha256(xml.encode()).hexdigest()
            for reg in ET.fromstring(xml).iter('reg'):
                number = int(reg.get('regnum',number))
                mapping[reg.get('name')] = (number,int(reg.get('bitsize')))
                number += 1
        names = [f'd{i}'for i in range(32)]+['fpscr','fpexc','fpsid']
        if any(name not in mapping or mapping[name][1] != (64 if name.startswith('d')else 32)
               for name in names):
            self._fail('Complete D0..D31/FPSCR/FPEXC/FPSID unavailable')
        self._fp_layout = {name:mapping[name]for name in names}
        self.report['register_xml_sha256'] = hashes
        self.report['fp_register_numbers'] = {name:mapping[name][0]for name in names}
        values = {}
        for name,(number,bits) in self._fp_layout.items():
            value = self.emu.rsp.request(f'p{number:x}')
            if len(value) != bits//4 or not re.fullmatch('[0-9a-fA-F]+',value):
                self._fail('Cannot preserve complete '+name)
            values[name] = value.lower()
        return values

    def _restore_fp(self, values):
        for name,value in values.items():
            if name != 'fpsid':
                number = self._fp_layout[name][0]
                if self.emu.rsp.request(f'P{number:x}={value}') != 'OK':
                    self._fail('Cannot restore '+name)
        for name,value in values.items():
            number = self._fp_layout[name][0]
            if self.emu.rsp.request(f'p{number:x}').lower() != value:
                self._fail('Restored '+name+' differs')

    def _peek(self):
        obj = self._u32(QUEUE_TABLE+(QUEUE_ID-1)*4)
        if not self._ram(obj,0x24):
            self._fail('Queue76 object missing or outside expected RAM')
        capacity, array = self._u32(obj+4), self._u32(obj+0xc)
        write_index, read_index, count, senders = [self._u32(obj+x)for x in (0x10,0x14,0x18,0x20)]
        if (not 1 <= capacity <= 0x10000 or not 1 <= count <= capacity
                or not 0 <= read_index < capacity or not 0 <= write_index < capacity
                or senders or not self._ram(array,capacity*4)):
            self._fail('Queue76 is empty, invalid, or has waiting-sender priority path')
        slot = array+read_index*4
        envelope = self._u32(slot)
        if not self._ram(envelope,20):
            self._fail('Queue76 envelope outside expected RAM')
        words = struct.unpack('<5I',self.emu.read_mem(envelope,20))
        pool6 = self._u32(0x08e1e1a4+5*4)
        pool_prefix = self._u32(envelope-4)
        self.report['queue_before'] = {'object':hex(obj),'capacity':capacity,'count':count,
            'read_index':read_index,'write_index':write_index,'slot':hex(slot),
            'envelope':hex(envelope),'words':[hex(x)for x in words],'waiting_senders':senders,
            'pool6_object':hex(pool6),'envelope_pool_prefix':hex(pool_prefix)}
        if words != self.expected:
            self._fail('Actual queue76 head is not the exact original mode1 request')
        if not self._ram(pool6,4) or pool_prefix != pool6:
            self._fail('Actual envelope does not have the original pool6 ownership prefix')
        return obj,envelope

    def _guard_mode35(self):
        """Permit only the actual observed flag0/prevff object-ready branch."""
        flag = self.emu.read_mem(0x08a9da70,1)[0]
        previous = self._u32(0x08aa0e10)
        ready = self.emu.read_mem(0x08ab09e0,2)
        objects = []
        self.report['command35_guards'] = {'flag_08a9da70':flag,'previous_08aa0e10':hex(previous),
                                          'ready_08ab09e0_hex':ready.hex(),'objects':objects}
        if flag != 0 or previous != 0xff or ready != b'\x01\x01':
            self._fail('Command35 diagnostic requires observed flag0/prevff and both existing objects ready')
        for address in (0x09003cf8,0x09003d8c):
            vtable,child = self._u32(address),self._u32(address+8)
            if vtable != 0x06fb4034 or not self._ram(child,0x180):
                self._fail('Command35 existing object/vtable differs')
            child_vtable = self._u32(child)
            get_child,reset_method = self._u32(vtable+0xc),self._u32(child_vtable+0x48) if child_vtable == 0x06fb4078 else 0
            objects.append({'address':hex(address),'vtable':hex(vtable),'child':hex(child),
                            'child_vtable':hex(child_vtable),'get_child':hex(get_child),
                            'child_method_48':hex(reset_method)})
            if get_child != 0x0679f7e1 or child_vtable != 0x06fb4078 or reset_method != 0x067a0fd1:
                self._fail('Command35 existing virtual-call targets differ')

    def start(self, cpu):
        if self._started:
            raise UIServiceProbeError('This probe handles exactly one request')
        self._started = True
        if type(cpu) is not int or not 0 <= cpu < 4 or self.emu.selected_cpu != cpu:
            self._fail('Select the stopped CPU before start')
        regs = self.emu.registers()
        tcb, cpsr, sp = self._task(cpu), regs['cpsr'], regs['r13']
        checks = {hex(a):self._u32(a)for a in GUARDS+(0x209c0+cpu*4,)}
        start,end = self._u32(tcb+0xc),self._u32(tcb+0x10)
        call_sp = sp & ~7
        self.report['guards'] = {'cpu':cpu,'tcb':hex(tcb),'task_id':self._u32(tcb+0xc0),
            'cpsr':hex(cpsr),'sp':hex(sp),'call_sp':hex(call_sp),
            'stack_start':hex(start),'stack_end':hex(end),'globals':checks}
        if (regs['r15'] != PEER_WAIT or self.emu.read_mem(tcb,4) != b'DRHT'
                or self._u32(tcb+0xc0) != TASK_ID or self._u32(tcb+0xd8) != TASK_ENTRY
                or self._u32(tcb+0xec) != 2):
            self._fail('Require original task66 ready/running at its peer wait')
        if (cpsr & 0x3f) != 0x33 or cpsr & (0x80|0x0600fc00) or any(checks.values()):
            self._fail('Require Thumb SVC, IRQ enabled, no IT/nesting/dispatch disable')
        if (sp & 3 or not 0x08000000 <= start <= call_sp-0x1000
                or not sp+64 <= end+1 <= 0x20000000):
            self._fail('Require original stack with4KiB free and safe aligned callSP')
        globals_expected = {0x06f49a28:QUEUE_ID,0x06f492b0:0x11d,
                            0x06f49c64:0x115,0x06f48f88:TASK_ID}
        values = {a:self._u32(a)for a in globals_expected}
        self.report['original_route_globals'] = {hex(a):hex(v)for a,v in values.items()}
        if values != globals_expected or self._u32(0x08aab9d4) >= 128:
            self._fail('Original service route globals or ACK ring index differ')
        if self.command == 0x35:
            self._guard_mode35()
        obj,envelope = self._peek()
        raw = self.emu.rsp.request('g')
        if not re.fullmatch('[0-9a-fA-F]+',raw) or len(raw)%2 or len(raw)//2 < 68:
            self._fail('Complete core GDB packet unavailable')
        fp = self._snapshot_fp()
        # Neither this alignment gap nor the suspended live frame is scratch.
        live = self.emu.read_mem(call_sp,sp+64-call_sp)
        self._saved = {'cpu':cpu,'tcb':tcb,'core':regs,'raw':raw,'fp':fp,
                       'sp':call_sp,'live_frame':live,'queue':obj,'envelope':envelope}
        self.report.update(register_packet_bytes=len(raw)//2, independent_fp_registers=len(fp),
                           message_buffer=hex(MESSAGE_BUFFER), timeout_argument=0,
                           stack_alignment_adjustment=sp-call_sp,
                           message_buffer_before_hex=self.emu.read_mem(MESSAGE_BUFFER,20).hex())
        self._add(self.return_address)
        self._launch('receive',RECEIVE_API,{0:QUEUE_ID,1:MESSAGE_BUFFER,2:0},RECEIVE_OBSERVERS)

    def _launch(self, stage, address, arguments, observers):
        self._stage = stage
        self.report['status'] = 'calling_original_'+stage
        for observer in observers:
            self._add(observer)
        for number,value in arguments.items():
            self.emu.write_reg('r'+str(number),value)
        self.emu.write_reg('sp',self._saved['sp'])
        self.emu.write_reg('lr',self.return_address|1)
        self.emu.write_reg('pc',address)
        self._deadline = time.monotonic()+self.stage_timeout

    def _validate_return(self, cpu, regs):
        saved = self._saved
        if (not saved or cpu != saved['cpu'] or self._task(cpu) != saved['tcb']
                or self._u32(saved['tcb']+0xc0) != TASK_ID
                or self._u32(saved['tcb']+0xd8) != TASK_ENTRY
                or self._u32(saved['tcb']+0xec) != 2):
            self._fail('Return CPU/task identity changed; no restore attempted')
        if (regs['r15'] != self.return_address or regs['r13'] != saved['sp']
                or (regs['cpsr']&0x3f) != 0x33 or regs['cpsr']&(0x80|0x0600fc00)
                or any(self._u32(a)for a in GUARDS+(0x209c0+cpu*4,))):
            self._fail('Return stack/mode/dispatch context differs; no restore attempted')
        if self.emu.read_mem(saved['sp'],len(saved['live_frame'])) != saved['live_frame']:
            self._fail('Original suspended stack frame changed; no forced repair')

    def _restore(self):
        saved = self._saved
        if self.emu.rsp.request('G'+saved['raw']) != 'OK':
            self._fail('Cannot restore core register packet')
        self._restore_fp(saved['fp'])
        if self.emu.registers() != saved['core']:
            self._fail('Restored core registers differ')
        self.report.update(original_wait_restored=True, fp_registers_restored=True,
                           live_stack_unchanged=True)
        self._saved = self._deadline = self._stage = None
        self._clear()

    def _returned(self, cpu, regs):
        self._validate_return(cpu,regs)
        result,stage = regs['r0'],self._stage
        self.report[stage+'_return'] = hex(result)
        if stage == 'receive':
            for address in RECEIVE_OBSERVERS:
                self._remove(address)
            if result != 0:
                self._restore()
                self._fail('Original nonblocking receive failed','original_receive_rejected')
            words = struct.unpack('<5I',self.emu.read_mem(MESSAGE_BUFFER,20))
            self.report['received_words'] = [hex(x)for x in words]
            if words != self.expected or not self.report.get('original_pool_release_returned'):
                self._restore()
                self._fail('Original receive evidence does not match guarded request','receive_evidence_mismatch')
            self.report['queue_count_after_receive'] = self._u32(self._saved['queue']+0x18)
            self._launch('dispatch',DISPATCH_API,{0:MESSAGE_BUFFER},self.dispatch_observers)
        else:
            ack_sent = self.report.get('ack_send_return') == '0x0'
            route_verified = (self.command == 0x36 or
                              all(self.report.get(name)for name in ('command35_handler_observed',
                                  'command35_control_start_observed','command35_next_stage_observed')))
            successful = result == 1 and route_verified and (ack_sent or self.command == 0x35)
            self._restore()
            if not successful:
                self._fail('Original dispatch did not satisfy the selected handler/ACK contract','dispatch_not_completed')
            self._completed = True
            self.report.update(status='original_request_serviced_wait_restored' if ack_sent else 'handler_returned_ack_pending',
                               original_request_consumed=True, original_ack_enqueued=ack_sent,
                               asynchronous_mode_completion_verified=False)

    def _step(self, pc):
        self._remove(pc)
        try:
            self.emu.step(timeout=min(.5,self.timeout_hint()))
        finally:
            if self.active:
                self._add(pc)

    def handle(self, pc, cpu):
        pc &= ~1
        if pc not in self._breakpoints:
            return False
        self.check_timeout()
        if self.emu.selected_cpu != cpu:
            self._fail('Select the stopped CPU before handle')
        regs = self.emu.registers()
        if pc == self.return_address:
            self._returned(cpu,regs)
            return True
        if not self._saved:
            self._fail('Observer without active call')
        if cpu != self._saved['cpu'] or self._task(cpu) != self._saved['tcb']:
            self._step(pc)
            return True
        event = {'pc':hex(pc),'cpu':cpu,'r0_r2':[hex(regs['r'+str(i)])for i in range(3)]}
        if pc == POOL_RELEASE_CALL:
            if regs['r0'] != 6 or regs['r1'] != self._saved['envelope']:
                self._fail('Original receive released an unexpected pool/envelope')
            self.report['original_pool_release_call'] = True
            event['kind'] = 'original_pool6_release'
        elif pc == POOL_RELEASE_RETURN:
            if not self.report.get('original_pool_release_call'):
                self._fail('Missing original pool release entry evidence')
            self.report.update(original_pool_release_returned=True,pool_release_return=hex(regs['r0']))
            if regs['r0'] != 0:
                self._fail('Original pool6 release failed; do not dispatch or release twice')
            event['kind'] = 'original_pool6_release_return'
        elif pc == self.mode_handler:
            if regs['r0'] != MESSAGE_BUFFER or struct.unpack('<5I',self.emu.read_mem(MESSAGE_BUFFER,20)) != self.expected:
                self._fail('Original mode handler did not receive exact mode1 request')
            event['kind'] = 'original_mode1_handler'
            if self.command == 0x35:
                self.report['command35_handler_observed'] = True
        elif pc == MODE35_START and self.command == 0x35:
            if regs['r0'] != 0xff:
                self._fail('Command35 control-init argument differs from observed previous mode')
            self.report['command35_control_start_observed'] = True
            event['kind'] = 'original_command35_control_start'
        elif pc == MODE35_NEXT and self.command == 0x35:
            if regs['r0'] != 0:
                self._fail('Command35 next-stage argument differs')
            self.report['command35_next_stage_observed'] = True
            event['kind'] = 'original_command35_next_stage'
        elif pc == ACK_ENTRY:
            if tuple(regs['r'+str(i)]for i in range(3)) != (self.command,1,0):
                self._fail('Original ACK arguments differ')
            index = self._u32(0x08aab9d4)
            if index >= 128:
                self._fail('Original ACK ring index outside capacity')
            self.report['ack_ring_pointer'] = hex(0x08e9f518+index*20)
            event['kind'] = 'original_ack_helper'
        elif pc == ACK_SEND_RETURN:
            pointer = int(self.report.get('ack_ring_pointer','0'),16)
            if not pointer or regs['r4'] != pointer:
                self._fail('Original ACK ring pointer differs')
            words = struct.unpack('<5I',self.emu.read_mem(pointer,20))
            self.report.update(ack_words=[hex(x)for x in words],ack_send_return=hex(regs['r0']))
            if words != self.ack_expected or regs['r0'] != 0:
                self._fail('Original ACK enqueue failed or its message differs; no restore in nested call')
            event['kind'] = 'original_ack_enqueue_return'
        self.report['events'].append(event)
        if len(self.report['events']) > 16:
            self._fail('Unexpected repeated service observers')
        self._step(pc)
        return True

    def close(self):
        """Remove only owned Z0 breakpoints; never restore an incomplete call."""
        self._clear()
        self.report.update(closed=True, unsafe_to_force_context_restore=self.active)
        if self.active:
            self.report['close_state'] = 'closed_with_incomplete_call'
