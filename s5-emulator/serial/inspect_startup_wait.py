#!/usr/bin/env python3
"""Read-only original-firmware task/GPIO evidence from a saved flat RAM image."""
import argparse, hashlib, json, mmap, struct
from pathlib import Path
import capstone
ROOT=Path(__file__).resolve().parents[2]
FIRMWARE=ROOT/'s5-v29-offline/unpacked-v29/03_program.bin'
BASE=0x06280000

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--ram',type=Path,default=ROOT/'s5-emulator/menu-smp/results/loader-timer-boot2/ram.bin');ap.add_argument('--output',type=Path,default=Path(__file__).parent/'startup-wait.json');a=ap.parse_args()
    firmware=FIRMWARE.read_bytes()
    assert hashlib.sha256(firmware).hexdigest()=='fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
    with a.ram.open('rb') as f:
        ram=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ)
        u32=lambda p:struct.unpack_from('<I',ram,p)[0]
        def task(p):
            return dict(address=hex(p),name=ram[p+0xc4:p+0xd4].split(b'\0')[0].decode('ascii','replace'),run_count=u32(p+4),saved_sp=hex(u32(p+8)),stack_start=hex(u32(p+12)),stack_end=hex(u32(p+16)),state=u32(p+0x30),entry=hex(u32(p+0xd8)),task_id=u32(p+0xc0),wait_type=u32(p+0x100),wait_object_id=u32(p+0x104),wait_object=hex(u32(p+0x108)),wait_args=hex(u32(p+0x114)))
        tasks=[];pos=0x08d00000
        while True:
            pos=ram.find(b'DRHT',pos,0x09000000)
            if pos<0:break
            if ram[pos+0xc4:pos+0xc9]==b'uiTSK':tasks.append(task(pos))
            pos+=4
        p=0x08dfe890; t=task(p);obj=u32(p+0x108);args=u32(p+0x114)
        data={
            'kind':'Original firmware RAM and static disassembly; no injected task/event mutation',
            'firmware':str(FIRMWARE),'firmware_sha256':hashlib.sha256(firmware).hexdigest(),
            'ram':str(a.ram.resolve()),'ram_base':'0x0','ram_size':len(ram),
            'current_thread_pointers':[hex(u32(0x20040+i*4)) for i in range(3)],
            'main_task':t,'tasks':tasks,
            'main_tcb_words':{hex(p+i):hex(u32(p+i)) for i in range(0,0x144,4)},
            'main_saved_stack_words':{hex(i):hex(u32(i)) for i in range(u32(p+8),u32(p+16)+1,4)},
            'event_wait':{'event_id':u32(p+0x104),'object':hex(obj),'object_words':[hex(u32(obj+i)) for i in range(0,32,4)],'current_bits':hex(u32(obj+8)),'mask':hex(u32(args)),'mode':u32(args+4),'mode_evidence':'0 means all requested bits; 0x065b231c / 0x06575fb4','timeout_ticks':hex(u32(p+0x4c)),'forever':u32(p+0x4c)==0xffffffff,'saved_return_chain':['0x063c8a5d','0x063c8c1d','0x06469e39','0x065b24df','0x065b2419']},
            'different_notification_event':{'set_wrapper':'0x06469eaa','set_implementation':'0x06575f58','set_semantics':'OR mask into another event','set_id_global':'0x06f498b4','set_id':u32(0x6f498b4),'wait_id_global':'0x06f498b8','wait_id':u32(0x6f498b8),'note':'Do not describe this as clearing the waited event.'},
            'gate':{'function':'0x063c8be2','observed_return_after_wait':'0x063c8c1d','prior_predicates':['0x065007a0','0x065451f2','0x06548ed2'],'gpio_test_function':'0x06548ed2','sampling':'10 LDRH reads of bit4 per register; returns nonzero if either input never sampled high','inputs':[{'address':'0x7f080410','gpio_id':54},{'address':'0x7f080094','gpio_id':37}],'physical_names':'Not proven. SD driver slot-indexed debounce at 0x064b201c suggests dual card-detect inputs; this is inference, not verified pin naming.','inactive_profile':'Explicit hypothetical external input values bit4=1 at these two registers only. No task event or original code patch.','register_note':'Generic GPIO config 0x06831bba writes higher config fields to these word addresses; bit4 is sampled input, not the whole register RO.','other_predicates':{'0x065007a0':'1 if 0x7f081000 bit4 low','0x06545536':'bit15 if 0x7f082814 bit4 low; bit13 if 0x7f080420 bit4 low','0x06543e74':'1 if 0x7f080050 bit4 low','0x065439ea':'Classifies ADC-like scalar into bins; input0 maps to3. Physical mode meaning unproven.','scalar_ram':'0x08e21132','scalar_value':struct.unpack_from('<H',ram,0x8e21132)[0]}},
            'limitations':['No physical pin names or true board reset levels verified.','Stack code addresses are candidate saved return addresses checked against original call sites, not DWARF unwinding.','GPIO inactive profile removes an artificial active-low condition; it is not evidence that the menu has rendered.']}
        a.output.write_text(json.dumps(data,indent=2)+'\n')
    md=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
    ranges=[(0x63c8be2,0x8a),(0x65007a0,0x20),(0x65451f2,0x32),(0x6545536,0x1e),(0x65439ea,0x3a),(0x6543e74,0x10),(0x6548ed2,0x42),(0x6469e08,0x4a),(0x6469eaa,0x14),(0x6575f58,0x30),(0x65b231c,0x164),(0x64b201c,0xac),(0x64b1b14,0x98),(0x6831bba,0xf8)]
    lines=['Original 03_program.bin SHA256 '+hashlib.sha256(firmware).hexdigest(),'Static Capstone Thumb disassembly. Literal pools may decode as instructions; literal values listed separately.']
    for start,size in ranges:
        lines.append('\nRANGE '+hex(start))
        for i in md.disasm(firmware[start-BASE:start-BASE+size],start):lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}')
    for addr in [0x65007bc,0x6544184,0x65452b4,0x65456a4,0x65456b0,0x6548f0c,0x6548f10,0x6469f20,0x6469f24,0x64b20bc,0x64b20c0,0x64b20c4,0x6ee5f08,0x6ee5f0c,0x6ee5f58,0x6ee5f5c]:
        lines.append(f'LITERAL {addr:08x} = {struct.unpack_from("<I",firmware,addr-BASE)[0]:08x}')
    a.output.with_suffix('.disasm.txt').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'output':str(a.output),'task_count':len(tasks),'main':t,'wait':data['event_wait']},indent=2))
if __name__=='__main__':main()
