#!/usr/bin/env python3
"""Audit the original time-zone prompt and the observed software-input lock."""
import hashlib
import json
import struct
from pathlib import Path
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
program = (ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base = 0x06280000
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
run = HERE.parent/'menu-smp/results/local-frame-query'
boot = json.loads((run/'result.json').read_text())
switch_index = 0x23c-0x1e7
switch_table = 0x0682d58c
switch_offset = struct.unpack_from('<I', program, switch_table-base+switch_index*4)[0]
assert switch_offset == 0x2da and switch_table+switch_offset == 0x0682d866
requests=[]
for request in boot['software_key_probe']['requests']:
    route = [event for event in request['events'] if event['kind']=='original_key_route']
    requests.append({'key':request['key'],'action':request['action'],'status':request['status'],
                     'route':route})
    assert route[-1]['route']=='secondary_transition_gate_return'
    assert route[-1]['r0_r2'][0]=='0x1'
    assert route[-1]['gate_values']['secondary_stage']==2
report={
    'scope':'Original code and prior execution evidence; no key call, state change, callback, or GPIO input in this analysis',
    'program_sha256':sha, 'run':str(run.resolve()),
    'presented_frame':str((run/'graphics/frame-0000.png').resolve()),
    'observed_text':'Please set the time zone; Set',
    'native_layout':boot['original_ui_state_at_stop'],
    'modal_id':'0x23c',
    'modal_dispatch':[
        '066beea6 calls0682de54 for an active original modal before the normalmode6 exclusion',
        '0682de54 receivesUIevent0f000002 and calls0682d0e0 with its originalUIkey parameter',
        'Original32-bit relative switch at0682d580..d588 uses table0682d58c',
        'For modal23c, index55, table entry0682d6e0=2da ->0682d866 ->0682e248',
    ],
    'original_confirmation_path':{
        'accepted_ui_key_codes':['0x7','0x12','0x48'],
        'center_source':'OriginalKEY_CENTER press ->bank0mask10 ->0663f02e ->066bddc8(7,0), when native input routing permits',
        'handler':'0682e268 calls066aa2ac(4), then068b728e(2)',
        'original_state_change':'068b7294 calls066bed42(0x20);068b72a2 stores its own kind2',
        'original_scene_change':'068b72a4 calls068b7b3c;068b7b42 calls066c1982(0x70de)',
        'validation_limit':'This downstream path is established statically, not yet observed for the blocked requests in this run',
    },
    'actual_blocked_requests':requests,
    'exact_input_gate':{
        'original_primary':'066bfd22 returns0 only whenpending_mode[08aa1124]==ff and stage[08aa1120] in{0,1}',
        'original_secondary':'066adcfc tailbranches0682e5d0; returns0 only whenstage[08aa090c] in{0,1}',
        'actual_secondary_stage':2,
        'meaning':'Both originalpress and release reachedthisgetter and returned1, so the rawroute returned before either normalkeyhandler',
        'not_proved':'Which original asynchronous display step will release the secondary lock must be observed; this analysis never sets it',
    },
    'probe_additions':{
        'wait_option':'KeyProbe.arm(...,wait_for_input_route_ready=True)',
        'default':False,
        'behavior':'At originaltask285 loop, reads both predicates, single-steps pending original loop, preserves the same bounded deadline; invokes no getter or key function while blocked',
        'report':'input_route_gate with actual primary_stage/pending_mode/secondary_stage, bothready predicates, samples andwait_steps',
        'timeout':'timeout_waiting_for_input_route_ready; no forcedstate orcontext restore',
        'read_only_consumption_observers':['0682d0e0','0682e248','068b728e','068b7b3c'],
    },
}
confirmed_run = HERE.parent/'menu-smp/results/local-shared-image-stride'
confirmed = json.loads((confirmed_run/'result.json').read_text())
confirmed_requests = confirmed['software_key_probe']['requests']
press = confirmed_requests[0]
assert press['action'] == 1 and press['consumed']
assert press['input_route_gate']['passed']
assert press['input_route_gate']['samples'] == 12
assert press['input_route_gate']['wait_steps'] == 11
assert press['input_route_gate']['secondary_stage'] == 1
routes = [event for event in press['events'] if event['kind'] == 'original_key_route']
by_name = {}
for event in routes:
    by_name.setdefault(event['route'], []).append(event)
assert any(event['r0_r2'][0] == '0x7'
           for event in by_name['original_timezone_prompt_key_handler'])
assert by_name['original_timezone_setup_entry'][0]['r0_r2'][0] == '0x2'
assert by_name['original_timezone_scene_setup'][0]['gate_values']['state'] == 0x20
assert confirmed['original_ui_state_at_stop']['source_scene_08cb10d0'] == '0x70de'
assert confirmed['original_ui_state_at_stop']['render_scene_0b392e98'] == '0x1001'
assert all(request['consumed'] and request['registers_restored']
           and request['fp_registers_restored'] for request in confirmed_requests)
report['later_actual_confirmation'] = {
    'run': str(confirmed_run.resolve()),
    'input_route_gate': press['input_route_gate'],
    'observed_original_route': [event for event in press['events']
                                if event.get('route', '').startswith('original_')
                                or event['kind'] in ('original_ui_key', 'original_ui_event')],
    'original_state_at_stop': confirmed['original_ui_state_at_stop'],
    'press_and_release_consumed': True,
    'all_core_and_fp_contexts_restored': True,
    'conclusion': 'Original time-zone confirmation executed, producing state0x20 and source scene0x70de.',
    'display_limit': 'Rendered scene remained0x1001 with modal0x23c. This proves the state transition, not presentation of the next scene.',
    'input_lock_limit': 'The native stage became1 naturally; the previously captured stage2 was a finite asynchronous wait, not proof of a missing peripheral.',
}
(HERE/'timezone-key-evidence.json').write_text(json.dumps(report,indent=2)+'\n')
cs=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB);cs.detail=True
ranges=[(0x0682de54,0x0682de98),(0x0682d54e,0x0682d558),(0x0682d580,0x0682d58a),
        (0x0682d866,0x0682d86e),(0x0682e248,0x0682e276),(0x068b728e,0x068b72b0),
        (0x068b7b3c,0x068b7b46),(0x066431de,0x06643248),(0x066bfd22,0x066bfd3a),
        (0x066adcfc,0x066add00),(0x0682e5d0,0x0682e5e2)]
lines=[f'Original program SHA256={sha}; selectedThumbblocks; no guest executed.']
for start,end in ranges:
    lines.append('\nRANGE '+hex(start))
    for ins in cs.disasm(program[start-base:end-base],start):
        suffix=''
        if ins.mnemonic.startswith('ldr') and len(ins.operands)>1:
            op=ins.operands[1]
            if op.type==capstone.arm.ARM_OP_MEM and op.mem.base==capstone.arm.ARM_REG_PC:
                literal=((ins.address+4)&~3)+op.mem.disp
                suffix=f'; [{literal:08x}]={struct.unpack_from("<I",program,literal-base)[0]:08x}'
        lines.append(f'{ins.address:08x} {ins.bytes.hex():10} {ins.mnemonic:9} {ins.op_str} {suffix}')
(HERE/'timezone-key.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Originalmodal23c contract and bothactualblockedkeyroutes verified; no injection.')
