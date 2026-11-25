#!/usr/bin/env python3
"""Read original frame-message/ACK control flow and saved RAM; no guest mutation."""
from pathlib import Path
import argparse, hashlib, json, mmap, struct
import capstone

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--snapshot',type=Path,default=HERE.parent/'menu-smp/results/local-ui-loop-postinit')
args=ap.parse_args()
b=(ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes();base=0x06280000
assert hashlib.sha256(b).hexdigest()=='fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
u32rom=lambda a:struct.unpack_from('<I',b,a-base)[0]
assert u32rom(0x06f49a2c)==0x77
assert u32rom(0x063b294c)==0x06705545
assert [0x06705276+2*b[0x06705276-base+i] for i in range(4)]==[0x067052ac,0x067052dc,0x067052c6,0x067052f0]
with (args.snapshot/'ram.bin').open('rb') as f:
    ram=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)
    u32=lambda a:struct.unpack_from('<I',ram,a)[0]
    u16=lambda a:struct.unpack_from('<H',ram,a)[0]
    task=0x08e03c18
    assert ram[task:task+4]==b'DRHT' and u32(task+0xc0)==67 and u32(task+0xd8)==0x06705545
    queue=u32(0x08e1b94c+(0x77-1)*4)
    frame=0x0a603fb8
    obj=u32(frame+0xbc+0x2c)
    report={
      'scope':'Read-only original bytes and completed snapshot. No IRQ, synthetic metadata, queue message, original poll call or ACK is injected.',
      'snapshot':str(args.snapshot.resolve()),'program_sha256':hashlib.sha256(b).hexdigest(),
      'actual':{
        'ui_step':hex(u32(0x08cb0ee0)),'ack_byte':hex(ram[0x08aa1103]),
        'lcd_state':u32(0x08ab488c),'avio_state':u32(0x08a9c7f8),
        'controller_state':u32(0x08aac2e4),'mode35_pending_byte14':ram[0x08aac2f8],
        'original_40001_received_byte15':ram[0x08aac2f9],
        'poll_counter_08aac26c':u16(0x08aac26c),'ack_fsm_08aaa780':hex(u32(0x08aaa780)),
        'postinit_flag_08aa02af':ram[0x08aa02af],
        'task67':{'tcb':hex(task),'entry':hex(u32(task+0xd8)),'state':u32(task+0xec),
                  'saved_sp':hex(u32(task+8)),'wait_object':hex(u32(task+0x108))},
        'queue77':{'object':hex(queue),'capacity':u32(queue+4),'count':u32(queue+0x18),
                   'read_index':u32(queue+0x14),'write_index':u32(queue+0x10)},
        'frame':{'address':hex(frame),'size_copied':'0x220','mode_bc':u32(frame+0xbc),
                 'child_e8':hex(obj),'child_byte19':ram[obj+0x19] if obj and obj+0x19<len(ram) else None},
      },
      'consumer':[
        'Original task67 entry06705544 receives queue77 via06705570 BL067ea67a, timeoutffffffff.',
        '0670558c calls06705762; for accepted frame codes and nonnull payload,0670577e BL0675eff8 copies0x220B payload into0a603fb8.',
        '06705786 checks frame+bc. Modes other than2/3 may wait at067057d0 original065b2480 before dispatch; a frame IRQ does not guarantee immediate poll.',
        '0670559e BL06705262 dispatches code0->067052ac->06786afe(r1=1); code1->067052dc->06786c30; code2->067052c6->06786afe(r1=0); code3->067052f0->06786cb0.',
        'These frame handlers call066d19c2 at06786c18/06786ca2/06786d10. Its controller-state2 branch066d147a returns1, continues original frame work, then tail-jumps066d179c at066d1a20.',
        '066d179c calls0649f6c2(frame+bc): if frame+e8 is nonnull and its byte19!=0, skip readiness poll; otherwise066d17b6 BL066d16fe.',
        'The same readiness function can run through067055dc->0670564e->066d179c when original08aa0178 gate is nonzero; this is also after a received frame-class message.',
      ],
      'readiness_and_ack':[
        '066d16fe requires byte08aac2f8(+14)!=0. If zero, resets halfword08aac26c and returns0.',
        'Byte08aac2f9(+15), set only by the observed original40001 handler, enables ready checks. Zero skips those checks but still increments the original poll counter.',
        'For controllerstate2:6718c9e reads08ab1758;676efba reads08aa0214;6718cac reads08ab16ef. Additional tests6728c64/6728cd2 and67365a2 exist; not named as proved hardware conditions.',
        '066d1774 increments halfword08aac26c. At old count0x5a it stores0x5b then tail-calls067361a8(1); larger counts also take that path. This is a count of function calls, not a verified time duration or frame rate.',
        'For actual ACK FSM08aaa780=1001,067361a8(1)->06736204 sets its original pending byte then067361f4 BL066d1a22(1).',
        '066d1a22 calls06759710(code35,arg1=1,arg2=0). Only successful send clears controller+14; when argument1 it sets controllerstate3. Original input task later ORs2 into08aa1103.',
      ],
      'producer_connection':[
        '065206b8(code,payload) constructs a static20B ring message {0,2001,code,payload,0} at08c4ffe0+index*20, index from08aa0234+2 modulo32, then sends queue77.',
        'Code0 sender0671edee is in0671e37a; code1/2 senders0671e0e8/0671e35e are in0671d72e; code3 sender0671eef2 is in0671edfa.',
        '0671edfa calls0671e37a/0671d72e and has caller065b9d52 in065b9c3a, plus064a7278 and064a74cc.',
        '064a7308 (contains064a74cc) also has caller065b9d40 in the same original service chain; this is not independent evidence for LCD196/232 as producer.',
        'Teammate original registration audit identifies IRQ192->06497252->065b9c3a and IRQ193->0649727c->065b9e1a. Source register/enable gates and metadata ownership are audited separately; no automatic clock is inferred here.',
      ],
      'next_evidence':'Use explicitly bounded source IRQ192/193 TEST input only after source gates are verified, observe original065206b8 and task67 copy/dispatch/poll/ACK. Preserve any new original wait. Do not directly call066d16fe or manufacture queue77 metadata/GUI ACK.',
    }
    ram.close()
(HERE/'mode35-frame-wait-evidence.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
cs=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB);cs.detail=True
ranges=[(0x06705544,0x067055b6),(0x06705762,0x067057e0),(0x06705262,0x06705276),
 (0x067052ac,0x06705304),(0x0675eff8,0x0675f014),(0x066d19c2,0x066d1a22),
 (0x066d147a,0x066d149a),(0x066d14a4,0x066d14a8),(0x066d179c,0x066d17ba),
 (0x0649f6c2,0x0649f6d6),(0x066d16fe,0x066d171c),(0x066d1730,0x066d179c),
 (0x067361a8,0x06736216),(0x066d1a22,0x066d1a4c),(0x065206b8,0x06520714),
 (0x0671edda,0x0671edf8),(0x065b9d34,0x065b9d58),(0x064a7272,0x064a7298)]
lines=['Selected known ARM Thumb code ranges; literal pools and TBB bytes are not executed code.']
for start,end in ranges:
    lines.append(f'\n[{start:08x}..{end:08x})')
    for ins in cs.disasm(b[start-base:end-base],start):
        note=''
        if ins.mnemonic.startswith('ldr') and len(ins.operands)>1 and ins.operands[1].type==capstone.arm.ARM_OP_MEM and ins.operands[1].mem.base==capstone.arm.ARM_REG_PC:
            lit=((ins.address+4)&~3)+ins.operands[1].mem.disp
            note=f' ; literal[{lit:08x}]={u32rom(lit):08x}'
        lines.append(f'{ins.address:08x}: {ins.mnemonic:8} {ins.op_str}{note}')
(HERE/'mode35-frame-wait.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps(report['actual'],indent=2))
