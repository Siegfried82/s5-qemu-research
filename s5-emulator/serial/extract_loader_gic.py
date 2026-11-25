#!/usr/bin/env python3
"""Extract exact loader GIC routes and original IRQ vector chain, without execution."""
from pathlib import Path
import hashlib,json,struct
import capstone
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
paths={n:ROOT/'s5-v29-offline/unpacked-v29'/n for n in ['01_loader2.bin','02_loader3.bin','03_program.bin']}
blobs={n:p.read_bytes() for n,p in paths.items()}
l3=blobs['02_loader3.bin'];program=blobs['03_program.bin']
assert hashlib.sha256(l3).hexdigest()=='67d44d7c2bb2091b7a56ca5118345e0ee604e6b2c6c31769cd6de06ae752d838'
rows=[]
for i in range(416):
 off=0x6978+i*4;irq,targets,config=struct.unpack_from('<HBB',l3,off);assert irq==i+32
 rows.append({'gic_id':irq,'loader3_table_file_offset':hex(off),'target_mask':hex(targets),'config_2bits':config,'itargetsr_address':hex(0x70001820+i),'icfgr_byte_address':hex(0x70001c08+i//4),'icfgr_shift':(i%4)*2})
report={'sources':{n:{'path':str(paths[n]),'sha256':hashlib.sha256(v).hexdigest()} for n,v in blobs.items()},'gic_distributor':{'loader2_store_file_offset':'0x1998','register':'0x70001000','value':1},'routes':{'loader3_function_file_offset':'0x10e8','loader3_call_file_offset':'0x19c','table_file_offset':'0x6978','count':416,'rows':rows},'cpu_interface':{'loader3_instructions_file_range':['0x1a0','0x1d0'],'base':'0x70002000','per_cpu_stride':'0x8000','control':1,'priority_mask':'0xf0','note':'Loader3 call path at these offsets runs CPU0. Replaying these values separately for all QEMU CPU interfaces is an explicit handoff reconstruction, not proof all cores use this exact loader call path.'},'irq_vectors':{'vbar':'0x0','preload':'Original complete 02_loader3.bin at 0 (48KiB); do not execute its startup for this handoff test.','chain':[{'address':'0x18','source':'loader3+0x18','instruction':'b 0x4c'},{'address':'0x4c','source':'loader3+0x4c','instruction':'ldr pc,[pc,#0x3f4]','literal_address':'0x448','literal_value':'0x06280000'},{'address':'0x06280000','source':'program+0','instruction':'ldr pc,[pc,#0x70]','literal_address':'0x06280078','literal_value':'0x068f9a0c'},{'address':'0x068f9a0c','source':'program','instruction':'b 0x068f9c1c'},{'address':'0x068f9c1c','source':'program','meaning':'Original RTOS IRQ context save, then 0x068f9a10 dispatch'}],'vbar_note':'No VBAR write was found in the examined loader/entry routines. VBAR0 plus the resident loader3 vector chain is supported by direct instruction evidence; high vectors remain unproven.'},'original_runtime_mmio':{'iar_function':'0x068e7ec4','iar_address':'0x7000200c + (MPIDR & 3) * 0x8000','eoir_function':'0x068e7ee8','eoir_address':'0x70002010 + (MPIDR & 3) * 0x8000','requirement':'Map CPU1/2 fixed GIC interface views at 0x7000a000 / 0x70012000; banked CPU0-only mapping is insufficient.'},'isolated_runtime_verification':'results-loader-gic-guest-readback/result.json','limitations':['Full IRQ delivery and menu rendering not proven by the isolated routing test.','Do not bulk-read GIC with GDB memory requests; use guest instructions or safe QOM state.']}
(HERE/'loader-gic.json').write_text(json.dumps(report,indent=2)+'\n')
ranges=[('01_loader2.bin',0x1974,0x40,0,0),('02_loader3.bin',0,0x50,0,0),('02_loader3.bin',0x19c,0x38,0,0),('02_loader3.bin',0x10e8,0x60,16,0),('03_program.bin',0,0x88,0,0x6280000),('03_program.bin',0x68f9a0c-0x6280000,0x78,0,0x6280000),('03_program.bin',0x68f9c1c-0x6280000,0x78,0,0x6280000),('03_program.bin',0x68e7ec4-0x6280000,0x48,0,0x6280000)]
lines=[]
for name,off,size,mode,base in ranges:
 lines.append(f'\nSOURCE {name} sha256={report["sources"][name]["sha256"]} file_offset={hex(off)} mode={"Thumb" if mode else "ARM"}')
 for i in capstone.Cs(capstone.CS_ARCH_ARM,mode).disasm(blobs[name][off:off+size],base+off):lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}')
for name,offs in [('01_loader2.bin',[0x1aa8]),('02_loader3.bin',[0x448,0x47c,0x480,0x113c,0x1140,0x1144])]:
 for off in offs:lines.append(f'LITERAL {name}+{off:x}: {struct.unpack_from("<I",blobs[name],off)[0]:08x}')
(HERE/'loader-gic.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Wrote loader-gic.json and loader-gic.disasm.txt; 416 routes validated.')
