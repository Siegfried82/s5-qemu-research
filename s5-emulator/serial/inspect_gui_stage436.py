#!/usr/bin/env python3
"""Read actual post-service GUI wait and command35 branch; never run a guest."""
import argparse
import hashlib
import json
import mmap
import struct
from pathlib import Path
import capstone

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--snapshot',type=Path,default=HERE.parent/'menu-smp/results/local-ui-service-first')
args=parser.parse_args()
program=(ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base=0x06280000
boot=json.loads((args.snapshot/'result.json').read_text())
word=lambda address:struct.unpack_from('<I',program,address-base)[0]
with (args.snapshot/'ram.bin').open('rb')as stream:
    ram=mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
    u32=lambda address:struct.unpack_from('<I',ram,address)[0]
    queue=u32(0x08e1b94c+(0x76-1)*4)
    capacity,array,read_index,count=[u32(queue+x)for x in (4,12,20,24)]
    messages=[]
    assert 0<count<=capacity
    for index in range(count):
        slot=array+((read_index+index)%capacity)*4
        pointer=u32(slot)
        messages.append({'slot':hex(slot),'envelope':hex(pointer),
                         'words':[hex(u32(pointer+x))for x in range(0,20,4)]})
    objects=[]
    for address in (0x09003cf8,0x09003d8c):
        vtable,child=u32(address),u32(address+8)
        child_vtable=u32(child)
        objects.append({'address':hex(address),'vtable':hex(vtable),'get_child':hex(u32(vtable+12)),
                        'child':hex(child),'child_vtable':hex(child_vtable),
                        'child_method_48':hex(u32(child_vtable+0x48))})
    report={
        'scope':'Original bytes and saved RAM only; no queue receive, ACK, flag, display signal or firmware mutation in this analysis',
        'snapshot':str(args.snapshot.resolve()),
        'program_sha256':hashlib.sha256(program).hexdigest(),
        'previous_actual_service':boot['original_ui_service_probe'],
        'ui':{'state':u32(0x08a928b4),'mode':u32(0x08a928bc),'step':hex(u32(0x08cb0ee0)),
              'transition_stage':u32(0x08aa1120),
              'control_base':'0x08aa10fc','control_hex':ram[0x08aa10fc:0x08aa1140].hex(),
              'previous_ack_08aa1104':hex(ram[0x08aa1104]),
              'current_ack_08aa1103':hex(ram[0x08aa1103]),
              'required_current_ack':'0x0f','missing_current_bits':hex(0xf&~ram[0x08aa1103])},
        'exact_wait_chain':[
            '066c0788 compares(step-0x3c2) to0x74; step0x436 jumps066c085e ->066c0a54',
            '066c0a54 LDRB r0,[r5,#7],r5=08aa10fc;066c0a56 CMP r0,#0xf',
            'Mismatch ->066c0a4e storesstep0x436 via066c07e8,returns0',
            'The tested byte is08aa1103,not previous-stage08aa1104',
        ],
        'ack_producer_and_consumer':{
            'request_producer':'066c03c8..066c03e0 calls original67ea590(queue76,reply115,source11d,code35,mode1,size0)',
            'original_ack_consumer':'064758ae compares messagecode35 ->064759b2 loads2 ->064759b4 BL066c1436',
            'writer':'066c1436 ORs r0 into byte[08aa10fc+7]',
            'other_bit':'066be25e calls066c1436(1); observedmask0xd already contains that bit',
        },
        'queue76':{'object':hex(queue),'capacity':capacity,'array':hex(array),'read_index':read_index,
                   'count':count,'waiting_senders':u32(queue+0x20),'messages':messages},
        'command35':{
            'tbb_byte_address':hex(0x0675eba8+0x35),
            'tbb_byte':program[0x0675eba8+0x35-base],
            'tbb_target':hex(0x0675eba8+program[0x0675eba8+0x35-base]*2),
            'handler':'0x0653b86c',
            'flag_08a9da70':ram[0x08a9da70],
            'previous_mode_08aa0e10':hex(u32(0x08aa0e10)),
            'actual_selected_path':'flag0,mode1,previousff ->0653b8ae BL066d04dc(ff); then0653b8b4 BL067361a8(0); then original STR mode1 to08aa0e10',
            'direct_ack_condition':'flag!=0 OR mode!=1 OR previousmode==1; none holds in this saved snapshot',
            'does_not_equal_command36':'Unlike command36, this selected path starts a substantial control sequence; no unconditional immediateACK contract',
            'ready_bytes_08ab09e0':ram[0x08ab09e0:0x08ab09e2].hex(),
            'objects':objects,
            'control_state_08aac2e4':[hex(u32(0x08aac2e4+x))for x in range(0,0x30,4)],
            'next_state_08aaa780':hex(u32(0x08aaa780)),
            'control_dependencies':[
                '066d04dc obtains two original objects via6608846/66088c0; calls child vtable+48 ->067a0fd0',
                '066d0548/0554 register original callback0648d793 through648b08e for pipelines0/1',
                '066d05ac sends originalcode40001 through6759778;066d05de sets its own controllerstate2',
                'Original further calls include676a65c,6730b7a,671f846,674b9bc,673626c,6500c42,65c2a34,67866fe,66d1d1e,671fbb2',
                'These are not a proved dependency-free ACK stub; downstream hardware/peer dependencies remain to be observed',
            ],
            'diagnostic_contract':'ui_service_probe.py command35 verifies the exact head and these original object/state guards, executes originalreceive/dispatch under2s stage deadlines, and only reports handler_returned_ack_pending if originaldispatchreturns1 without synchronousACK',
        },
        'independent_lcd_wait':{'state_08ab488c':u32(0x08ab488c),'avio_08a9c7f8':u32(0x08a9c7f8),
                                'evidence':'display-clock-evidence.json'},
        'conclusion':'The direct step0x436 condition is a second software acknowledgement byte missingbit2, with the actualcode35 request still queued for task66. This is not evidence that the step directly reads or waits for the LCD completion word. LCD synchronization is independently incomplete; later code35 processing may introduce display/hardware dependencies.',
    }
    assert report['ui']['step']=='0x436'
    assert report['ui']['current_ack_08aa1103']=='0xd'
    assert report['ui']['previous_ack_08aa1104']=='0xff'
    assert report['command35']['tbb_target']=='0x675ec80'
    assert messages[0]['words']==['0x115','0x11d','0x35','0x1','0x0']
    ram.close()
(HERE/'gui-stage436-evidence.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
cs=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB);cs.detail=True
ranges=[(0x066c0788,0x066c078e),(0x066c085e,0x066c0860),
        (0x066c0a3c,0x066c0a66),(0x066c03b2,0x066c03ec),
        (0x064758ac,0x064758b4),(0x064759aa,0x064759ba),(0x066c142c,0x066c1440),
        (0x066be258,0x066be264),(0x0675ec7a,0x0675ec86),
        (0x0653b86c,0x0653b8bc),(0x0653efea,0x0653eff0),
        (0x066d04dc,0x066d0632),(0x067361a8,0x0673626c),
        (0x06608846,0x06608868),(0x066088c0,0x066088e2),
        (0x0679f7e0,0x0679f7f4),(0x067a0fd0,0x067a1058)]
lines=[]
for start,end in ranges:
    lines.append('\nOriginal bytes at '+hex(start))
    for instruction in cs.disasm(program[start-base:end-base],start):
        text=f'{instruction.address:08x} {instruction.bytes.hex():10} {instruction.mnemonic:8} {instruction.op_str}'
        operands=instruction.operands
        if (instruction.mnemonic=='ldr'and len(operands)==2 and operands[1].type==capstone.CS_OP_MEM
                and instruction.reg_name(operands[1].mem.base)=='pc'):
            literal=((instruction.address+4)&~3)+operands[1].mem.disp
            text+=f' ; [{literal:08x}]={word(literal):08x}'
        lines.append(text)
(HERE/'gui-stage436.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps({'step':report['ui']['step'],'ack_current':report['ui']['current_ack_08aa1103'],
                  'queue_head':messages[0]['words'],'command35_direct_ack':False}))
