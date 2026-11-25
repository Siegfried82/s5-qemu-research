#!/usr/bin/env python3
"""Explain a saved original guard-write abort; never repair pointers or run QEMU."""
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
parser.add_argument('--snapshot',type=Path,default=HERE.parent/'menu-smp/results/local-ui-mode35')
parser.add_argument('--fault-report',type=Path,
                    default=HERE.parent/'menu-smp/results/local-ui-mode35-fault-regs/result.json')
args=parser.parse_args()
program=(ROOT/'s5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base=0x06280000
word=lambda a:struct.unpack_from('<I',program,a-base)[0]
boot=json.loads((args.snapshot/'result.json').read_text())
fault_run=json.loads(args.fault_report.read_text())
fault_event=next(x for x in reversed(fault_run['events'])if 'fault_system_registers'in x)
system=fault_event['fault_system_registers']
regs={k:int(v,16)for k,v in boot['final_registers'][2].items()}
fault_pc=regs['r14']-8
assert fault_pc==0x0646bcc6
cs=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
insn=next(cs.disasm(program[fault_pc-base:fault_pc-base+2],fault_pc))
assert (insn.mnemonic,insn.op_str)==('str','r6, [r7]')
with (args.snapshot/'ram.bin').open('rb')as stream:
    ram=mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
    u32=lambda a:struct.unpack_from('<I',ram,a)[0]
    tcb=u32(0x20048)
    assert ram[tcb:tcb+4]==b'DRHT'and u32(tcb+0xc0)==285
    desc=regs['r5']
    allocation_base,size,parent=u32(desc+4),u32(desc+8),u32(desc+12)
    guard=regs['r4']
    assert size>=guard
    table=word(0x066808d8)
    regions=[list(struct.unpack_from('<4I',ram,table+i*16))for i in range(4)]
    matches=[(i,row)for i,row in enumerate(regions)
             if row[0]and row[1]!=0xdead and row[3]!=0xdead
             and row[1]<=allocation_base<row[1]+row[0]]
    assert len(matches)==1
    region_index,region=matches[0]
    translated=allocation_base-region[1]+region[3]
    destination=(translated+size-guard)&0xffffffff
    assert destination==regs['r7']==int(system['DFAR']['value'],16)
    assert translated==regs['r2']
    arenas=[]
    count=u32(0x08ab1044)
    assert count==39
    for index in range(count):
        address=0x09049268+index*32
        arenas.append({'index':index,'address':hex(address),'id':ram[address],
                       'base':hex(u32(address+4)),'size':hex(u32(address+8)),
                       'end_exclusive':hex(u32(address+4)+u32(address+8)),
                       'class_byte':ram[address+12]})
    ttbr=int(system['TTBR0']['value'],16)&~0x3fff
    sections=[]
    for virtual in (fault_pc,allocation_base,translated,destination):
        address=ttbr+(virtual>>20)*4
        descriptor=u32(address)
        assert descriptor&3==2
        sections.append({'virtual':hex(virtual),'l1_entry_address':hex(address),
                         'descriptor':hex(descriptor),
                         'physical':hex((descriptor&0xfff00000)|(virtual&0xfffff)),
                         'source':'First run saved RAM; second run confirms same TTBR0 and fault address'})
    end=u32(tcb+16)
    candidates=[]
    for address in range((end-0x600)&~3,(end+1)&~3,4):
        value=u32(address)
        if 0x06280000<=value<0x07fe7000 and value&1:
            candidates.append({'address':hex(address),'value':hex(value)})
    stack_edges=[]
    for stack_address,call_address,target in (
            (0x0a03db1c,0x0646a710,0x0646bca2),
            (0x0a03db84,0x0646a76e,0x0646a6aa),
            (0x0a03dba4,0x067e3334,0x0646a72c),
            (0x0a03dbbc,0x06769e76,0x067e3314)):
        saved=u32(stack_address)
        decoded=next(cs.disasm(program[call_address-base:call_address-base+4],call_address))
        assert saved==(call_address+4)|1
        assert decoded.mnemonic=='bl'and decoded.op_str==f'#{hex(target)}'
        stack_edges.append({'saved_lr_address':hex(stack_address),'saved_lr':hex(saved),
                            'call_instruction':hex(call_address),'callee':hex(target)})
    report={
        'scope':'Read-only original instruction and saved execution analysis; no pointer patch, alias folding, fault skip, or QEMU execution',
        'snapshot':str(args.snapshot.resolve()),'fault_register_run':str(args.fault_report.resolve()),
        'program_sha256':hashlib.sha256(program).hexdigest(),
        'fault':{'cpu':2,'registers':boot['final_registers'][2],
                 'pc_from_abort_lr_minus8':hex(fault_pc),'instruction':insn.mnemonic+' '+insn.op_str,
                 'bytes':insn.bytes.hex(),'write_address':hex(destination),'write_value':hex(allocation_base),
                 'system_registers':system,
                 'dfsr_interpretation':'0x808: WnR=1, short-descriptor fault status8, synchronous external abort; not translation or permission fault'},
        'task':{'tcb':hex(tcb),'id':u32(tcb+0xc0),'entry':hex(u32(tcb+0xd8)),
                'state':u32(tcb+0xec),'cpu':u32(tcb+0x94),'name_scope':'Original input/UI task285, not the injected task66 call',
                'saved_tcb_sp':hex(u32(tcb+8)),'stack_start':hex(u32(tcb+12)),'stack_end':hex(end),
                'abort_sp':'r13=0 is the banked ABT stack at vector entry; it is not evidence the interrupted task SP was0',
                'inferred_svc_sp':'0x0a03db08, from original646bca2 six-register frame and savedLR646a715; not a direct banked-SP read',
                'confirmed_saved_call_edges':stack_edges,
                'other_stack_code_candidates_not_a_verified_backtrace':candidates},
        'allocation':{'descriptor':hex(desc),'words':[hex(u32(desc+x))for x in range(0,32,4)],
                      'base':hex(allocation_base),'size':hex(size),'guard_size':hex(guard),'parent_arena':hex(parent),
                      'guard_offset':hex(size-guard),'translated_base':hex(translated),
                      'guard_destination':hex(destination),
                      'subtraction_underflow':False,
                      'caller_requested_bytes':'0x02938000; allocator adds128-byte guard and aligns to128',
                      'origin':'Original646a6aa ->646b2fc selects arena ->646b5aa allocates ->646bca2 writes tail guard',
                      'selection':'Type0x11 maps to pool registry entry0904820c; policy field+0xc=1 causes646b368 to start from finalarena(first+0x4c0)'},
        'arena_initialization':{
            'function':'0x0646adf0','initializer_call':'0x0646ae36 ->0x0646ad2a',
            'source_instructions':['646ae2e LDR r2, literal646b104=703e0000','646ae30 MOV r1,0fc00000','646ae34 LDR r0,[08ab103c+8]'],
            'hardcoded_start':'0x0fc00000','hardcoded_size':'0x703e0000','hardcoded_end_exclusive':'0x7ffe0000',
            'block_size_constant':hex(word(0x0646b0f8)),
            'count':count,'descriptor_base':'0x09049268','descriptor_stride':32,
            'arenas':arenas,
            'interpretation':'Range is deliberately initialized by original firmware, not created by the observed guard subtraction. It does not alone prove physical DRAM capacity.'},
        'original_address_translation':{
            'table':hex(table),'row_fields':['size','field1','field2','field3'],
            'rows':[[hex(x)for x in row]for row in regions],
            'call':'646bcba ->66c1b4c ->66808c6 ->6680838',
            'conversion':'66808c6 chooses sourcefield1 and destinationfield3',
            'matching_row':region_index,'arithmetic':f'{hex(allocation_base)} - {hex(region[1])} + {hex(region[3])} = {hex(translated)}',
            'second_bank_note':'Row1 maps field1 40000000..7fffffff to field3 c0000000..ffffffff; field2 is DEAD, so this is not evidence to back low40000000..7fffffff as cached RAM.'},
        'saved_page_table_sections':sections,
        'conclusion':'The original tail-guard store uses a validly computed high-bank address. The actual page table maps that VA identically, and the observed write external abort is explained by the existing512MiB high RAM window not covering it. This fault is not evidence of guard-size underflow or an arbitrary uninitialized pointer.',
        'bounded_followup':'An explicit2GiB high-address RAM backing experiment is grounded in the original allocator and translation table. Preserve Device/MMIO low mappings and do not fold addresses into512MiB. Observe whether the same original store proceeds; this tests the mapping gap, not real-camera capacity or DSP readiness.',
        'limits':'Actual physical DRAM topology and whether later initialization constrains this address pool remain independently unverified; later exceptions or hardware waits must be retained.',
    }
    ram.close()
(HERE/'mode35-fault-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
cs.detail=True
ranges=[(0x0646bca2,0x0646bd14),(0x0646a6aa,0x0646a72c),(0x0646a72c,0x0646a7a8),
        (0x067e3314,0x067e334c),(0x06769e6e,0x06769e80),
        (0x0646adf0,0x0646ae3e),(0x0646ad2a,0x0646adf0),
        (0x0646b34e,0x0646b398),(0x0646b5aa,0x0646b62e),(0x0646b64c,0x0646b704),
        (0x066c1b4c,0x066c1b7e),(0x06680838,0x066808ce)]
lines=[]
for start,end in ranges:
    lines.append('\nOriginal bytes at '+hex(start))
    for instruction in cs.disasm(program[start-base:end-base],start):
        text=f'{instruction.address:08x} {instruction.bytes.hex():10} {instruction.mnemonic:8} {instruction.op_str}'
        operands=instruction.operands
        if (instruction.mnemonic.startswith('ldr')and len(operands)==2 and operands[1].type==capstone.CS_OP_MEM
                and instruction.reg_name(operands[1].mem.base)=='pc'):
            literal=((instruction.address+4)&~3)+operands[1].mem.disp
            text+=f' ; [{literal:08x}]={word(literal):08x}'
        lines.append(text)
(HERE/'mode35-fault-evidence.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps({'fault_pc':hex(fault_pc),'task':report['task']['id'],'destination':hex(destination),
                  'arenas':count,'guard_underflow':False,'source_bank':region_index}))
