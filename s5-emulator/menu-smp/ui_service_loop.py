#!/usr/bin/env python3
"""Bounded execution of the original task66 ARM receive/dispatch loop.

This is a local component experiment BEFORE DSP/post-init completion, not normal
boot. Start only at the real task66 PEER_WAIT after any inferior call has been
restored. A separate 16-byte frame is reserved below its suspended stack; only
SP, R6 and PC are changed. Original code receives, copies/releases messages,
dispatches them, and optionally sends its fallback response. This helper never
writes guest memory or manufactures messages, release results, flags or ACKs.

Forward owned stops to handle(pc,cpu) with that CPU selected. Keep the runner's
total deadline: an empty queue is supposed to block in the original RTOS. At
most 64 successfully received messages are processed. The limit stops at the
NEXT iteration boundary, after dispatch/fallback return, before receive #65.
The suspended pre-init context is never restored, including on close/error.
"""
import hashlib
import struct

PEER_WAIT = 0x065491ee
LOOP_ENTRY, ITERATION = 0x0675efba, 0x0675efbc
POSTINIT_ENTRY, POSTINIT_SECOND = 0x0675efb2, 0x0675efb6
POSTINIT_BYTES = bytes.fromhex('c1f72bfb5bf609f8')
RECEIVE_RETURN, DISPATCH_CALL, DISPATCH_RETURN = 0x0675efc6, 0x0675efce, 0x0675efd2
FALLBACK_CALL, FALLBACK_RETURN = 0x0675eff2, 0x0675eff6
OBSERVERS = (ITERATION, RECEIVE_RETURN, DISPATCH_CALL, DISPATCH_RETURN,
             FALLBACK_CALL, FALLBACK_RETURN)
LOOP_BYTES = bytes.fromhex(
    '8b4d3246844928688bf05afb0028f8d1814c204600f0c1f80028f2d1854920680968'
    '8842edd1a168002301228de80e0081492a680b688049faf75cfce1e7')
MESSAGE_BUFFER, QUEUE_ID, QUEUE_TABLE = 0x0a603680, 0x76, 0x08e1b94c
TASK_ID, TASK_ENTRY = 66, 0x0675ef85
GUARDS = (0x08ab4e80, 0x08ab4e7c, 0x08ab4e68)
ROUTES = {0x06f49a28: QUEUE_ID, 0x06f49c64: 0x115, 0x06f48f88: TASK_ID}


class UIServiceLoopProbeError(RuntimeError):
    """Stop this local experiment without repairing or restoring guest state."""


class UIServiceLoopProbe:
    def __init__(self, emu, *, max_messages=64, run_postinit=False):
        if type(max_messages) is not int or not 1 <= max_messages <= 64:
            raise ValueError('The diagnostic message limit must be between 1 and 64')
        self.emu, self.max_messages = emu, max_messages
        if type(run_postinit) is not bool:
            raise ValueError('run_postinit must be an explicit boolean')
        self.run_postinit = run_postinit
        self._breakpoints, self._started, self._active = set(), False, False
        self._context, self._stage, self._message = None, None, None
        self.report = {
            'scope': 'Original ARM receive/dispatch loop entered before unfinished DSP/post-init; local component execution only',
            'status': 'not_started', 'max_messages': max_messages,
            'run_postinit_requested': run_postinit,
            'limit_semantics': 'Stop at next original iteration boundary after dispatch and any fallback return, before another receive',
            'original_instructions_modified': False, 'guest_memory_written_by_helper': False,
            'queue_or_message_fields_synthesized': False, 'acknowledgements_synthesized': False,
            'dsp_ready_or_magic_written': False, 'ui_or_tcb_fields_written': False,
            'dsp_initialization_completed': False, 'postinit_672060c_executed_by_helper': False,
            'postinit_65b9fcc_executed_by_helper': False,
            'skipped_initialization': ['Remainder of 0676ede0 after current DSP wait',
                                       '0672060c -> 0675e6ec', '065b9fcc writes 08aa02af=1'],
            'original_wait_restored': False, 'menu_opened_verified': False,
            'messages_received': 0, 'messages_completed': 0, 'receive_errors': 0,
            'events': [], 'messages': [],
        }

    @property
    def active(self):
        return self._active

    @property
    def breakpoints(self):
        return frozenset(self._breakpoints)

    def _u32(self, address):
        return struct.unpack('<I', self.emu.read_mem(address, 4))[0]

    @staticmethod
    def _ram(address, size=4):
        return address % 4 == 0 and 0x08000000 <= address <= 0x20000000-size

    def _fail(self, message, status=None):
        self.report.update(status=status or ('loop_stopped' if self.active else 'guard_rejected'),
                           error=message, unsafe_to_force_context_restore=self.active)
        raise UIServiceLoopProbeError(message)

    def _task(self, cpu):
        tcb = self._u32(0x20040+cpu*4)
        if not self._ram(tcb, 0x150):
            self._fail('Current task pointer is outside expected task RAM')
        return tcb

    def _add(self, pc):
        if pc not in self._breakpoints:
            self.emu.breakpoint_add(pc)
            self._breakpoints.add(pc)

    def _remove(self, pc):
        if pc in self._breakpoints:
            self.emu.breakpoint_remove(pc)
            self._breakpoints.remove(pc)

    def _queue(self):
        obj = self._u32(QUEUE_TABLE+(QUEUE_ID-1)*4)
        if not self._ram(obj, 0x24):
            self._fail('Original queue76 object is unavailable')
        capacity, array, write, read, count, senders = [self._u32(obj+x)
            for x in (4, 0xc, 0x10, 0x14, 0x18, 0x20)]
        if (not 1 <= capacity <= 0x10000 or not 0 <= count <= capacity
                or not 0 <= read < capacity or not 0 <= write < capacity
                or not self._ram(array, capacity*4)):
            self._fail('Original queue76 metadata is inconsistent')
        # An empty queue, waiting senders and static envelopes are valid here.
        # Only the original receive implementation decides ownership/release.
        return {'object': hex(obj), 'capacity': capacity, 'array': hex(array),
                'read_index': read, 'write_index': write, 'count': count,
                'waiting_senders': hex(senders)}

    def start(self, cpu):
        if self._started:
            raise UIServiceLoopProbeError('A loop diagnostic can only be started once')
        self._started = True
        if type(cpu) is not int or not 0 <= cpu < 4 or self.emu.selected_cpu != cpu:
            self._fail('Select the stopped CPU before start')
        regs, tcb = self.emu.registers(), self._task(cpu)
        cpsr, sp = regs['cpsr'], regs['r13']
        frame_sp = (sp & ~7)-16
        start, end = self._u32(tcb+0xc), self._u32(tcb+0x10)
        guards = {hex(a): self._u32(a) for a in GUARDS+(0x209c0+cpu*4,)}
        self.report['guards'] = {'cpu': cpu, 'tcb': hex(tcb), 'cpsr': hex(cpsr),
            'sp': hex(sp), 'loop_sp': hex(frame_sp), 'stack_start': hex(start),
            'stack_end': hex(end), 'globals': guards}
        if (regs['r15'] != PEER_WAIT or self.emu.read_mem(tcb, 4) != b'DRHT'
                or self._u32(tcb+0xc0) != TASK_ID or self._u32(tcb+0xd8) != TASK_ENTRY
                or self._u32(tcb+0xec) != 2):
            self._fail('Require original running task66 at its peer wait')
        if (cpsr & 0x3f) != 0x33 or cpsr & (0x80|0x0600fc00) or any(guards.values()):
            self._fail('Require Thumb SVC, IRQ enabled, no IT/nesting/dispatch disable')
        if (sp & 3 or not 0x08000000 <= start <= frame_sp-0x1000
                or not sp+64 <= end+1 <= 0x20000000):
            self._fail('Require 16-byte separate frame, 4KiB call headroom and intact original frame')
        if self.emu.read_mem(LOOP_ENTRY, len(LOOP_BYTES)) != LOOP_BYTES:
            self._fail('Original receive/dispatch loop bytes differ from verified S5 v2.9')
        if self.run_postinit and self.emu.read_mem(POSTINIT_ENTRY, len(POSTINIT_BYTES)) != POSTINIT_BYTES:
            self._fail('Original post-init call instructions differ from verified S5 v2.9')
        route_values = {a: self._u32(a) for a in ROUTES}
        self.report['route_globals'] = {hex(a): hex(v) for a,v in route_values.items()}
        if route_values != ROUTES or self._u32(0x08aab9d4) >= 128:
            self._fail('Original routing globals or response-ring index differ')
        self.report['queue_before'] = self._queue()
        live_start = sp & ~7
        live = self.emu.read_mem(live_start, sp+64-live_start)
        self._context = {'tcb': tcb, 'sp': frame_sp, 'live_start': live_start, 'live': live}
        self.report.update(original_core={k: hex(v) for k,v in regs.items()},
                           original_live_frame_sha256=hashlib.sha256(live).hexdigest(),
                           loop_bytes_sha256=hashlib.sha256(LOOP_BYTES).hexdigest(),
                           postinit_flag_08aa02af_before=self.emu.read_mem(0x08aa02af, 1)[0],
                           message_buffer_before_hex=self.emu.read_mem(MESSAGE_BUFFER, 20).hex(),
                           register_writes=['sp', 'r6', 'pc'], timeout_argument='0xffffffff')
        try:
            for pc in OBSERVERS+((POSTINIT_ENTRY, POSTINIT_SECOND, LOOP_ENTRY) if self.run_postinit else ()):
                self._add(pc)
        except Exception:
            self.close()
            raise
        # Mark active before the first register mutation: any transport failure
        # thereafter leaves a stopped context that must not be force-restored.
        self._active = True
        self._stage = 'postinit_entry' if self.run_postinit else 'entry'
        self.report['status'] = 'entering_original_postinit' if self.run_postinit else 'entering_original_loop'
        self.emu.write_reg('sp', frame_sp)
        self.emu.write_reg('r6', 0xffffffff)
        self.emu.write_reg('pc', POSTINIT_ENTRY if self.run_postinit else LOOP_ENTRY)

    def _validate(self, cpu, regs, pc):
        c = self._context
        if (self._task(cpu) != c['tcb'] or self._u32(c['tcb']+0xc0) != TASK_ID
                or self._u32(c['tcb']+0xd8) != TASK_ENTRY or self._u32(c['tcb']+0xec) != 2):
            self._fail('Observer is not executing in the original task66 context')
        if (regs['r15'] != pc or regs['r13'] != c['sp']
                or (regs['cpsr'] & 0x3f) != 0x33 or regs['cpsr'] & (0x80|0x0600fc00)
                or any(self._u32(a) for a in GUARDS+(0x209c0+cpu*4,))
                or regs['r6'] != 0xffffffff
                or (pc not in (POSTINIT_ENTRY, POSTINIT_SECOND, LOOP_ENTRY) and regs['r5'] != 0x06f49a28)):
            self._fail('Original loop register/stack/IRQ contract changed')
        if self.emu.read_mem(c['live_start'], len(c['live'])) != c['live']:
            self._fail('Suspended initialization frame changed; no repair attempted')

    def _step(self, pc):
        self._remove(pc)
        try:
            self.emu.step(timeout=.5)
        finally:
            if self.active:
                self._add(pc)

    def handle(self, pc, cpu):
        pc &= ~1
        if pc not in self._breakpoints:
            return False
        if not self.active or self.emu.selected_cpu != cpu:
            self._fail('Owned stop without active loop or selected stopped CPU')
        regs = self.emu.registers()
        self._validate(cpu, regs, pc)
        event = {'pc': hex(pc), 'cpu': cpu}
        if pc == POSTINIT_ENTRY:
            if self._stage != 'postinit_entry':
                self._fail('Unexpected repeated original post-init entry')
            self._stage = 'postinit_first_running'
            event.update(kind='original_postinit_call', target='0x0672060c')
        elif pc == POSTINIT_SECOND:
            if self._stage != 'postinit_first_running':
                self._fail('First original post-init return without observed call')
            self._stage = 'postinit_second_running'
            self.report['postinit_672060c_executed_by_helper'] = True
            self.report['postinit_672060c_return'] = hex(regs['r0'])
            event.update(kind='original_postinit_return_and_next_call', result=hex(regs['r0']), target='0x065b9fcc')
        elif pc == LOOP_ENTRY:
            if self._stage != 'postinit_second_running':
                self._fail('Second original post-init return without observed call')
            self._stage = 'entry'
            self.report['postinit_65b9fcc_executed_by_helper'] = True
            self.report['postinit_flag_08aa02af_after'] = self.emu.read_mem(0x08aa02af, 1)[0]
            self.report['skipped_initialization'] = ['Remainder of 0676ede0 after current DSP wait']
            event.update(kind='original_postinit_return', result=hex(regs['r0']))
        elif pc == ITERATION:
            if self._stage not in ('entry', 'receive_error', 'dispatch_return', 'fallback_return'):
                self._fail('Unexpected original loop boundary before the observed operation returned')
            if self._message is not None:
                if (self._message['dispatch_return'] == '0x0'
                        and self._message['words_after_dispatch'][0] == '0x115'
                        and self._stage != 'fallback_return'):
                    self._fail('Expected original fallback-send return was not observed')
                self._message['iteration_completed'] = True
                self.report['messages_completed'] += 1
                self._message = None
            event['kind'] = 'original_iteration_boundary'
            event['messages_completed'] = self.report['messages_completed']
            self.report['events'].append(event)
            if self.report['messages_completed'] >= self.max_messages:
                self._fail('Bounded original loop reached its message limit before another receive',
                           'message_limit_reached')
            self._stage = 'receiving'
            self.report['status'] = 'original_receive_pending'
        elif pc == RECEIVE_RETURN:
            if self._stage != 'receiving':
                self._fail('Receive return without an original receive iteration')
            event.update(kind='original_receive_return', result=hex(regs['r0']))
            if regs['r0'] != 0:
                self.report['receive_errors'] += 1
                self._stage = 'receive_error'
                if self.report['receive_errors'] >= 64:
                    self.report['events'].append(event)
                    self._fail('64 original receive errors; stopping diagnostic error loop', 'receive_error_limit')
            else:
                words = struct.unpack('<5I', self.emu.read_mem(MESSAGE_BUFFER, 20))
                self._message = {'number': self.report['messages_received']+1,
                                 'words': [hex(x) for x in words], 'iteration_completed': False}
                self.report['messages'].append(self._message)
                self.report['messages_received'] += 1
                event['words'] = self._message['words']
                self._stage = 'received'
        elif pc == DISPATCH_CALL:
            if self._stage != 'received' or regs['r0'] != MESSAGE_BUFFER or regs['r4'] != MESSAGE_BUFFER:
                self._fail('Original dispatch does not follow the observed receive into its work buffer')
            self._stage = 'dispatching'
            self.report['status'] = 'original_dispatch_pending'
            event['kind'] = 'original_dispatch_call'
        elif pc == DISPATCH_RETURN:
            if self._stage != 'dispatching':
                self._fail('Dispatch return without the observed original call')
            self._message['dispatch_return'] = hex(regs['r0'])
            self._message['words_after_dispatch'] = [hex(x) for x in struct.unpack(
                '<5I', self.emu.read_mem(MESSAGE_BUFFER, 20))]
            self._stage = 'dispatch_return'
            event.update(kind='original_dispatch_return', result=hex(regs['r0']))
        elif pc == FALLBACK_CALL:
            if (self._stage != 'dispatch_return' or self._message['dispatch_return'] != '0x0'
                    or self._message['words_after_dispatch'][0] != '0x115'):
                self._fail('Unexpected original fallback response path')
            self._stage = 'fallback'
            event.update(kind='original_fallback_call', r0_r3=[hex(regs['r'+str(i)]) for i in range(4)],
                         stack_words=[hex(x) for x in struct.unpack('<3I', self.emu.read_mem(regs['r13'], 12))])
        elif pc == FALLBACK_RETURN:
            if self._stage != 'fallback':
                self._fail('Fallback return without the observed original call')
            self._message['fallback_send_return'] = hex(regs['r0'])
            self._stage = 'fallback_return'
            event.update(kind='original_fallback_return', result=hex(regs['r0']))
        if pc != ITERATION:
            self.report['events'].append(event)
        self._step(pc)
        return True

    def close(self):
        """Remove owned breakpoints only; never restore the pre-init context."""
        for pc in tuple(self._breakpoints):
            self._remove(pc)
        self.report.update(closed=True, unsafe_to_force_context_restore=self.active,
                           close_state='stopped_without_context_restore' if self.active else 'not_running')
        self._active = False
