#!/usr/bin/env python3
"""Read saved RAM and original instructions; never inject completion messages."""
import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--snapshot', type=Path, default=HERE.parent/'menu-smp/results/isolated-display-gradient')
args = parser.parse_args()
program = (ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
post = (ROOT/'s5-v29-offline/unpacked-v29/05_postboot1.bin').read_bytes()
boot = json.loads((args.snapshot/'result.json').read_text())
with (args.snapshot/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda a: struct.unpack_from('<I', ram, a)[0]
    def task(a):
        assert ram[a:a+4] == b'DRHT'
        offsets = {'id':0xc0,'run_count':4,'saved_sp':8,'priority':0x2c,
                   'cpu':0x94,'entry':0xd8,'state':0xec,'wait_type':0x100,
                   'wait_object_id':0x104,'wait_object':0x108,'timeout':0x4c}
        result = {'address':hex(a), **{k:hex(u32(a+o)) for k,o in offsets.items()}}
        sp, end = u32(a+8), u32(a+0x10)
        result['stack_original_code_candidates'] = [
            {'stack_address':hex(x),'value':hex(u32(x))}
            for x in range(sp, min(end,sp+0x400),4)
            if 0x06280000 <= u32(x) < 0x07fe7000 and u32(x)&1]
        return result
    queue_address = 0x08e1a08c
    queue_buffer = u32(queue_address+8)
    envelope = u32(queue_buffer)
    words = struct.unpack_from('<5I',ram,envelope)
    assert words == (0x115,0x11d,0x36,1,0)
    assert u32(0x06f49a28) == 0x76
    control = 0x08aa10fc
    report = {
        'scope':'Observed original GUI startup wait during explicit local scheduling experiment; no flags, messages, firmware or TCB patched',
        'snapshot':str(args.snapshot/'ram.bin'),
        'program_sha256':hashlib.sha256(program).hexdigest(),
        'postboot1_sha256':hashlib.sha256(post).hexdigest(),
        'graphics':{'swaps':boot['graphics_swaps'],'draws':boot['graphics_draws'],
                    'normal_menu_verified':False,
                    'warmup':'Original7290030 creates and draws a4x4 path at72901ca..7290234; first swap alone is not a menu'},
        'ui_state':{'base':'0x08a928b0','words':[hex(u32(0x08a928b0+i)) for i in range(0,24,4)],
                    'event_gate':'66beeec reads+0xc; when6,66beef0 branches to66bef1c, skipping normal state handler',
                    'state_zero_handler':'Table06a1aff4 entry0 points66bef53(BX LR); actual mode6 already skips that handler'},
        'mode_transition':{'base':hex(control),'block_hex':ram[control:control+0x44].hex(),
                           'stage':u32(control+0x24),'requested_mode':u32(control+0x18),
                           'progress_address':'0x08cb0ee0','progress':hex(u32(0x08cb0ee0)),
                           'ack_address':hex(control+8),'ack_value':hex(ram[control+8]),'required':'0x0f','missing_bits':hex(0xf & ~ram[control+8]),
                           'poll_chain':['6475e5a BL66c138a','stage2 ->66c13b0 ->66c0718','step3c2 ->66c082e ->66c0964','66c0966 CMP ack,#0xf','not equal ->66c095a storesstep3c2,returns0'],
                           'original_ack_handler':'64758d2 compares messagecode0x36;64759aa calls66c142c(2),which ORs byte[08aa1104]'},
        'pending_original_request':{'queue_id':'0x76','object':hex(queue_address),
                                    'object_words':[hex(u32(queue_address+i))for i in range(0,36,4)],
                                    'buffer':hex(queue_buffer),'envelope':hex(envelope),
                                    'message_words':[hex(x)for x in words],
                                    'envelope_fields':['replyqueue0x115','source0x11d','code0x36','mode1','payloadsize0'],
                                    'producer':'66c1778(mode1) ->66c17d0 BL67ea590; targetqueue0x76,replyqueue0x115,code0x36',
                                    'consumer':'task66 entry675ef84 calls676ede0 before675efc2 queue_receive; queueID global06f49a28 is0x76'},
        'tasks':[task(a)for a in [0x08e03ad4,0x08dffe14,0x08e13d04,0x08e15000]],
        'display_isolation':boot['display_isolation'],
        'conclusion':'The unsupported coprocessor initialization in task66 prevents the queued original mode request from being consumed. Current GUI progress remains3c2 and lacks originalackbit2. Localdelay does not complete that dependency.',
        'linux_causality':'No Linux dependency is needed to explain this observed gating. CPU3 was explicitly parked in this snapshot; this report does not establish that later menu features are independent of Linux.',
        'next_evidence':'Complete/execute the real peer or retain this as a partial experiment; observe original675efc2 consumption,64759aa acknowledgement,66c096a progress and a normal rendered frame. Do not synthesize their completion.'
    }
    assert report['mode_transition']['progress']=='0x3c2'
    assert report['mode_transition']['ack_value']=='0xd'
    assert report['ui_state']['words'][3]=='0x6'

(HERE/'gui-start-wait-evidence.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
cs = capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
ranges = [(0x675ef84,0x675eff8),(0x66c1778,0x66c17d4),
          (0x66c138a,0x66c139e),(0x66c13a4,0x66c13cc),
          (0x66c0718,0x66c0738),(0x66c0944,0x66c0992),
          (0x64758c8,0x64758de),(0x64759aa,0x64759ba),
          (0x66c142c,0x66c1436),(0x66beea6,0x66bef3c),
          (0x66bef46,0x66bef54),(0x72901ca,0x729023a),
          (0x729036a,0x729038c)]
lines=[]
for start,end in ranges:
    base,data=(0x7267000,post)if start>=0x7267000 else(0x6280000,program)
    lines.append('\nOriginal bytes at '+hex(start))
    lines.extend(f'{i.address:08x} {i.bytes.hex():10} {i.mnemonic:8} {i.op_str}'
                 for i in cs.disasm(data[start-base:end-base],start))
(HERE/'gui-start-wait.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps({'progress':report['mode_transition']['progress'],'ack':report['mode_transition']['ack_value'],
                  'pending_message':report['pending_original_request']['message_words'],
                  'consumer':'task66','swaps':report['graphics']['swaps']}))
